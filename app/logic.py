"""Règles de gestion : crédits, séances, réservations, annulations, paiements, relevés."""
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

from sqlalchemy import func, select

from .core import (Booking, ClassSession, CreditEntry, Payment, Series, Studio, User, now_local, settings)

ACTIVE = ("booked", "attended")


class RuleError(Exception):
    """Refus métier. `code` est la clé du message affiché par le site."""

    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


# ---------------------------------------------------------------- crédits
def credits_for(reference_price):
    """RG : crédits = tarif de référence / valeur du crédit, arrondi, minimum 1."""
    return max(1, round(reference_price / settings.credit_value))


def move_credits(db, user, delta, reason, actor_id=None):
    user.credits += delta
    db.add(CreditEntry(user_id=user.id, delta=delta, reason=reason, actor_id=actor_id))


def expire_plan(db, user):
    """Les crédits non utilisés expirent à la fin de la période payée."""
    if user.plan_expires_at and user.plan_expires_at < now_local():
        if user.credits > 0:
            move_credits(db, user, -user.credits, "Expiration de la formule")
        user.plan_id, user.plan_expires_at = None, None
        db.commit()


# ---------------------------------------------------------------- séances
def js_weekday(d):
    return (d.weekday() + 1) % 7  # 0 = dimanche, comme dans le site


def ensure_sessions(db):
    """Crée les séances manquantes des cours récurrents sur la fenêtre de réservation."""
    now = now_local()
    series = db.scalars(select(Series).join(Studio, Studio.id == Series.studio_id)
                        .where(Series.is_active, Studio.is_active)).all()
    if not series:
        return
    existing = set(db.execute(
        select(ClassSession.series_id, ClassSession.starts_at)
        .where(ClassSession.series_id.is_not(None), ClassSession.starts_at >= now.replace(hour=0, minute=0, second=0, microsecond=0))
    ).all())
    created = False
    for s in series:
        days = {int(x) for x in s.days.split(",") if x != ""}
        hh, mm = (int(x) for x in s.time.split(":"))
        for offset in range(settings.window_days):
            day = (now + timedelta(days=offset)).date()
            if js_weekday(day) not in days:
                continue
            start = datetime(day.year, day.month, day.day, hh, mm)
            if start <= now or (s.id, start) in existing:
                continue
            db.add(ClassSession(studio_id=s.studio_id, series_id=s.id, discipline=s.discipline, title=s.title,
                                starts_at=start, duration=s.duration, capacity=s.capacity,
                                gopass_spots=s.gopass_spots, reference_price=s.reference_price, credits=s.credits))
            created = True
    if created:
        db.commit()


def taken_map(db, session_ids):
    if not session_ids:
        return {}
    rows = db.execute(select(Booking.session_id, func.count()).where(
        Booking.session_id.in_(session_ids), Booking.status.in_(ACTIVE)).group_by(Booking.session_id)).all()
    return dict(rows)


# ---------------------------------------------------------------- réservations
def new_code(db):
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    while True:
        code = "GP-" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not db.scalar(select(Booking.id).where(Booking.code == code)):
            return code


def book(db, user_id, session_id):
    # Verrous sur la séance puis le membre : deux réservations simultanées ne peuvent pas
    # prendre la dernière place ni dépenser deux fois les mêmes crédits.
    sess = db.scalar(select(ClassSession).where(ClassSession.id == session_id).with_for_update())
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not sess or sess.status != "open" or sess.starts_at <= now_local():
        raise RuleError("eGone", 404)
    if db.scalar(select(Booking.id).where(Booking.user_id == user.id, Booking.session_id == sess.id,
                                          Booking.status.in_(ACTIVE))):
        raise RuleError("eAlready")
    taken = db.scalar(select(func.count()).select_from(Booking).where(
        Booking.session_id == sess.id, Booking.status.in_(ACTIVE)))
    if taken >= sess.gopass_spots:
        raise RuleError("eFull")
    if user.credits < sess.credits:
        raise RuleError("needCredits", 402)
    booking = Booking(user_id=user.id, session_id=sess.id, credits=sess.credits, code=new_code(db))
    db.add(booking)
    move_credits(db, user, -sess.credits, f"Réservation : {sess.title}")
    db.commit()
    return booking


def refundable(sess):
    return sess.starts_at - now_local() > timedelta(hours=settings.cancel_hours)


def cancel_booking(db, user_id, booking_id):
    booking = db.scalar(select(Booking).where(Booking.id == booking_id, Booking.user_id == user_id).with_for_update())
    if not booking or booking.status != "booked" or booking.session.starts_at <= now_local():
        raise RuleError("eGone", 404)
    booking.status = "cancelled"
    if refundable(booking.session):
        booking.refunded = True
        user = db.scalar(select(User).where(User.id == user_id).with_for_update())
        move_credits(db, user, booking.credits, f"Annulation : {booking.session.title}")
    db.commit()
    return booking


def cancel_session(db, sess):
    """Annulation par le studio : toutes les réservations sont remboursées. Renvoie les membres touchés."""
    sess.status = "cancelled"
    affected = []
    for b in db.scalars(select(Booking).where(Booking.session_id == sess.id, Booking.status == "booked")).all():
        b.status, b.refunded = "studio_cancelled", True
        user = db.scalar(select(User).where(User.id == b.user_id).with_for_update())
        move_credits(db, user, b.credits, f"Séance annulée par le studio : {sess.title}")
        affected.append((user, sess))
    return affected


def check_in(db, studio_id, code):
    code = code.strip().upper().replace(" ", "")
    if code and not code.startswith("GP-"):
        code = "GP-" + code
    booking = db.scalar(select(Booking).join(ClassSession, ClassSession.id == Booking.session_id)
                        .where(Booking.code == code, ClassSession.studio_id == studio_id).with_for_update())
    if not booking:
        raise RuleError("checkNo", 404)
    if booking.status != "booked":
        raise RuleError("checkDone")
    if booking.session.starts_at.date() != now_local().date():
        raise RuleError("checkDay")
    booking.status, booking.checked_in_at = "attended", now_local()
    db.commit()
    return booking


# ---------------------------------------------------------------- paiements
def apply_payment(db, payment, actor_id=None):
    """Active la formule payée. Sans effet si le paiement est déjà enregistré (webhook reçu deux fois)."""
    if payment.status == "paid":
        return False
    now = now_local()
    user = db.scalar(select(User).where(User.id == payment.user_id).with_for_update())
    if user.plan_expires_at and user.plan_expires_at < now and user.credits > 0:
        move_credits(db, user, -user.credits, "Expiration de la formule")
    payment.status, payment.paid_at, payment.recorded_by = "paid", now, actor_id
    move_credits(db, user, payment.credits, f"Formule {payment.plan.name_fr}", actor_id)
    user.plan_id = payment.plan_id
    user.plan_expires_at = now + timedelta(days=settings.plan_days)
    db.commit()
    return True


# ---------------------------------------------------------------- relevés
def statement(db, studio, month):
    """Réservations dues au studio sur un mois : présences, absences et annulations tardives."""
    start = datetime.strptime(month + "-01", "%Y-%m-%d")
    end = (start + timedelta(days=32)).replace(day=1)
    now = now_local()
    rows = db.scalars(select(Booking).join(ClassSession, ClassSession.id == Booking.session_id).where(
        ClassSession.studio_id == studio.id, ClassSession.starts_at >= start, ClassSession.starts_at < end)).all()
    lines, total = [], 0
    for b in rows:
        s = b.session
        if b.status == "attended":
            kind = "attended"
        elif b.status == "booked" and s.starts_at < now:
            kind = "no_show"
        elif b.status == "cancelled" and not b.refunded:
            kind = "late_cancel"
        else:
            continue
        amount = round(s.reference_price * studio.payout_rate)
        total += amount
        lines.append({"date": s.starts_at.isoformat(), "title": s.title, "kind": kind, "amount": amount})
    lines.sort(key=lambda x: x["date"])
    return {"month": month, "studio": studio.name, "lines": lines, "total": total, "payout_rate": studio.payout_rate}


# ---------------------------------------------------------------- e-mails
MAIL = {
    "booked": {"fr": ("Réservation confirmée", "Votre réservation est confirmée : {title}, {when}, {studio}.\nCode à présenter à l'accueil : {code}"),
               "en": ("Booking confirmed", "Your booking is confirmed: {title}, {when}, {studio}.\nCode to show at the front desk: {code}"),
               "ar": ("تم تأكيد الحجز", "تم تأكيد حجزك: {title}، {when}، {studio}.\nالرمز الذي تُظهره عند الاستقبال: {code}")},
    "studio_cancelled": {"fr": ("Séance annulée par le studio", "La séance {title} du {when} est annulée par {studio}. Vos crédits ont été rendus."),
                         "en": ("Class cancelled by the studio", "The class {title} on {when} was cancelled by {studio}. Your credits have been refunded."),
                         "ar": ("ألغى الاستوديو الحصة", "ألغى {studio} حصة {title} بتاريخ {when}. تمت إعادة أرصدتك.")},
    "paid": {"fr": ("Formule activée", "Votre paiement est enregistré. Formule {plan} activée : {credits} crédits ajoutés."),
             "en": ("Plan activated", "Your payment is recorded. Plan {plan} activated: {credits} credits added."),
             "ar": ("تم تفعيل الاشتراك", "تم تسجيل دفعتك. تم تفعيل اشتراك {plan}: أُضيف {credits} رصيدًا.")},
}


def send_mail(to, kind, lang, **values):
    """Envoie un e-mail si SMTP est configuré ; sinon ne fait rien. Ne bloque jamais une réservation."""
    if not (settings.smtp_host and settings.mail_from):
        return
    subject, body = MAIL[kind].get(lang, MAIL[kind]["fr"])
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "Gopass · " + subject, settings.mail_from, to
    msg.set_content(body.format(**values))
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except Exception as exc:  # l'e-mail est un plus : on journalise sans faire échouer l'action
        print("mail error:", exc)
