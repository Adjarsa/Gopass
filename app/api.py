"""Routes de l'API Gopass."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import logic
from .core import (COOKIE, DISCIPLINES, IS_SQLITE, TOKEN_DAYS, AdminLog, Booking, ClassSession, ContactMessage,
                   CreditEntry, Payment, Plan, Series, Studio, StudioApplication, User, check_password, get_db,
                   hash_password, make_token, now_local, read_token, settings)

router = APIRouter(prefix="/api")
LANGS = ("fr", "en", "ar")


# ---------------------------------------------------------------- accès
def current_user(request: Request, db: Session = Depends(get_db)):
    uid = read_token(request.cookies.get(COOKIE, ""))
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active:
        raise HTTPException(401, "auth")
    logic.expire_plan(db, user)
    return user


def optional_user(request: Request, db: Session = Depends(get_db)):
    uid = read_token(request.cookies.get(COOKIE, ""))
    user = db.get(User, uid) if uid else None
    return user if user and user.is_active else None


def studio_user(user: User = Depends(current_user)):
    if user.role != "studio" or not user.studio_id:
        raise HTTPException(403, "forbidden")
    return user


def admin_user(user: User = Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(403, "forbidden")
    return user


def rule(fn, *args):
    try:
        return fn(*args)
    except logic.RuleError as exc:
        raise HTTPException(exc.status, exc.code)


def log(db, actor, action, detail=""):
    db.add(AdminLog(actor_id=actor.id, action=action, detail=detail))


def set_cookie(response, user):
    response.set_cookie(COOKIE, make_token(user.id), max_age=TOKEN_DAYS * 86400, httponly=True,
                        samesite="lax", secure=not IS_SQLITE, path="/")


# ---------------------------------------------------------------- formats de sortie
def out_plan(p):
    return {"id": p.id, "name": [p.name_fr, p.name_en, p.name_ar], "desc": [p.desc_fr, p.desc_en, p.desc_ar],
            "credits": p.credits, "price": p.price_dzd, "active": p.is_active, "sort": p.sort}


def out_studio(s, disciplines=()):
    return {"id": s.id, "name": s.name, "city": s.city, "area": s.area, "desc": s.description,
            "disciplines": sorted(disciplines), "active": s.is_active, "payout_rate": s.payout_rate}


def out_session(s, taken=0, mine=False, full=False):
    data = {"id": s.id, "studio_id": s.studio_id, "studio": s.studio.name, "city": s.studio.city,
            "area": s.studio.area, "discipline": s.discipline, "title": s.title, "start": s.starts_at.isoformat(),
            "duration": s.duration, "credits": s.credits, "spots": s.gopass_spots,
            "left": max(0, s.gopass_spots - taken), "mine": mine}
    if full:
        data.update({"taken": taken, "capacity": s.capacity, "status": s.status, "recurring": s.series_id is not None})
    return data


def out_booking(b):
    s, now = b.session, now_local()
    status = b.status
    if status == "booked" and s.starts_at < now:
        status = "past"
    return {"id": b.id, "code": b.code, "status": status, "credits": b.credits, "refunded": b.refunded,
            "upcoming": b.status == "booked" and s.starts_at > now, "refundable": logic.refundable(s),
            "title": s.title, "discipline": s.discipline, "start": s.starts_at.isoformat(),
            "duration": s.duration, "studio": s.studio.name, "studio_id": s.studio_id}


def out_me(db, user):
    bookings = db.scalars(select(Booking).where(Booking.user_id == user.id).order_by(Booking.id.desc()).limit(100)).all()
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role, "lang": user.lang,
            "credits": user.credits, "plan": out_plan(user.plan) if user.plan else None,
            "plan_expires": user.plan_expires_at.isoformat() if user.plan_expires_at else None,
            "studio_id": user.studio_id, "bookings": [out_booking(b) for b in bookings]}


# ---------------------------------------------------------------- public
@router.get("/config")
def config():
    return {"cancel_hours": settings.cancel_hours, "online_payment": bool(settings.chargily_secret_key),
            "window_days": settings.window_days, "disciplines": DISCIPLINES}


@router.get("/plans")
def plans(db: Session = Depends(get_db)):
    return [out_plan(p) for p in db.scalars(select(Plan).where(Plan.is_active).order_by(Plan.sort, Plan.id)).all()]


@router.get("/studios")
def studios(db: Session = Depends(get_db)):
    discs = {}
    for sid, d in db.execute(select(Series.studio_id, Series.discipline).where(Series.is_active)).all():
        discs.setdefault(sid, set()).add(d)
    rows = db.scalars(select(Studio).where(Studio.is_active).order_by(Studio.city, Studio.name)).all()
    return [out_studio(s, discs.get(s.id, ())) for s in rows]


@router.get("/sessions")
def sessions(db: Session = Depends(get_db), user: User | None = Depends(optional_user)):
    logic.ensure_sessions(db)
    now = now_local()
    rows = db.scalars(select(ClassSession).join(Studio, Studio.id == ClassSession.studio_id).where(
        ClassSession.status == "open", Studio.is_active, ClassSession.starts_at > now,
        ClassSession.starts_at < now + timedelta(days=settings.window_days)).order_by(ClassSession.starts_at)).all()
    taken = logic.taken_map(db, [s.id for s in rows])
    mine = set()
    if user:
        mine = set(db.scalars(select(Booking.session_id).where(
            Booking.user_id == user.id, Booking.status.in_(logic.ACTIVE))).all())
    return [out_session(s, taken.get(s.id, 0), s.id in mine) for s in rows]


class ApplicationIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    city: str = Field(min_length=2, max_length=80)
    email: EmailStr
    disciplines: str = Field(min_length=2, max_length=300)


@router.post("/applications")
def apply(data: ApplicationIn, db: Session = Depends(get_db)):
    db.add(StudioApplication(name=data.name.strip(), city=data.city.strip(), email=data.email.lower(),
                             disciplines=data.disciplines.strip()))
    db.commit()
    return {"ok": True}


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    message: str = Field(min_length=2, max_length=4000)


@router.post("/contact")
def contact(data: ContactIn, db: Session = Depends(get_db)):
    db.add(ContactMessage(name=data.name.strip(), email=data.email.lower(), message=data.message.strip()))
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- comptes
class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    lang: str = "fr"


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


@router.post("/auth/register")
def register(data: RegisterIn, response: Response, db: Session = Depends(get_db)):
    email = data.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "eEmailUsed")
    user = User(name=data.name.strip(), email=email, password_hash=hash_password(data.password),
                lang=data.lang if data.lang in LANGS else "fr")
    db.add(user)
    db.commit()
    set_cookie(response, user)
    return out_me(db, user)


@router.post("/auth/login")
def login(data: LoginIn, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email.lower()))
    if not user or not user.is_active or not check_password(data.password, user.password_hash):
        raise HTTPException(401, "eLogin")
    set_cookie(response, user)
    logic.expire_plan(db, user)
    return out_me(db, user)


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return out_me(db, user)


class LangIn(BaseModel):
    lang: str


@router.patch("/me")
def set_lang(data: LangIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if data.lang in LANGS:
        user.lang = data.lang
        db.commit()
    return {"ok": True}


@router.delete("/me")
def delete_me(response: Response, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Suppression du compte : réservations à venir annulées, données personnelles effacées."""
    if user.role != "member":
        raise HTTPException(403, "forbidden")
    now = now_local()
    for b in db.scalars(select(Booking).where(Booking.user_id == user.id, Booking.status == "booked")).all():
        if b.session.starts_at > now:
            b.status = "cancelled"
    user.name, user.email = "Compte supprimé", f"deleted-{user.id}@invalid"
    user.password_hash, user.is_active, user.credits, user.plan_id = "", False, 0, None
    db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


# ---------------------------------------------------------------- réservations
class BookIn(BaseModel):
    session_id: int


@router.post("/bookings")
def create_booking(data: BookIn, tasks: BackgroundTasks, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    booking = rule(logic.book, db, user.id, data.session_id)
    s = booking.session
    tasks.add_task(logic.send_mail, user.email, "booked", user.lang, title=s.title,
                   when=s.starts_at.strftime("%d/%m/%Y %H:%M"), studio=s.studio.name, code=booking.code)
    db.refresh(user)
    return out_me(db, user)


@router.post("/bookings/{booking_id}/cancel")
def cancel(booking_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rule(logic.cancel_booking, db, user.id, booking_id)
    db.refresh(user)
    return out_me(db, user)


# ---------------------------------------------------------------- paiement en ligne (Chargily Pay)
class CheckoutIn(BaseModel):
    plan_id: int


@router.post("/payments/checkout")
def checkout(data: CheckoutIn, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    plan = db.get(Plan, data.plan_id)
    if not plan or not plan.is_active:
        raise HTTPException(404, "eGone")
    if not settings.chargily_secret_key:
        raise HTTPException(503, "ePayOff")
    base = settings.public_url or str(request.base_url).rstrip("/")
    payment = Payment(user_id=user.id, plan_id=plan.id, amount=plan.price_dzd, credits=plan.credits, method="online")
    db.add(payment)
    db.commit()
    try:
        r = httpx.post(settings.chargily_base_url + "/checkouts", timeout=20,
                       headers={"Authorization": "Bearer " + settings.chargily_secret_key},
                       json={"amount": plan.price_dzd, "currency": "dzd",
                             "success_url": base + "/?paiement=ok#compte",
                             "failure_url": base + "/?paiement=echec#formules",
                             "description": f"Gopass - {plan.name_fr}",
                             "locale": user.lang if user.lang in ("ar", "en", "fr") else "fr"})
        r.raise_for_status()
        body = r.json()
        payment.provider_ref = body["id"]
        db.commit()
        return {"url": body["checkout_url"]}
    except Exception as exc:
        payment.status = "failed"
        db.commit()
        print("chargily error:", exc)
        raise HTTPException(502, "ePayFail")


@router.post("/webhooks/chargily")
async def chargily_webhook(request: Request, db: Session = Depends(get_db)):
    """Chargily signe chaque notification : HMAC-SHA256 du corps brut avec la clé secrète."""
    raw = await request.body()
    signature = request.headers.get("signature", "")
    if not signature or not settings.chargily_secret_key:
        raise HTTPException(400, "signature")
    expected = hmac.new(settings.chargily_secret_key.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(403, "signature")
    event = json.loads(raw)
    ref = (event.get("data") or {}).get("id")
    payment = db.scalar(select(Payment).where(Payment.provider_ref == ref)) if ref else None
    if payment:
        if event.get("type") == "checkout.paid":
            if logic.apply_payment(db, payment):
                u = payment.user
                logic.send_mail(u.email, "paid", u.lang, plan=payment.plan.name_fr, credits=payment.credits)
        elif event.get("type") in ("checkout.failed", "checkout.canceled", "checkout.expired") and payment.status == "pending":
            payment.status = "failed"
            db.commit()
    return {}


# ---------------------------------------------------------------- espace studio
def own_session(db, user, session_id, lock=False):
    q = select(ClassSession).where(ClassSession.id == session_id, ClassSession.studio_id == user.studio_id)
    sess = db.scalar(q.with_for_update() if lock else q)
    if not sess:
        raise HTTPException(404, "eGone")
    return sess


def notify_cancelled(tasks, affected):
    for u, s in affected:
        tasks.add_task(logic.send_mail, u.email, "studio_cancelled", u.lang, title=s.title,
                       when=s.starts_at.strftime("%d/%m/%Y %H:%M"), studio=s.studio.name)


@router.get("/studio/dashboard")
def dashboard(user: User = Depends(studio_user), db: Session = Depends(get_db)):
    logic.ensure_sessions(db)
    now = now_local()
    studio = db.get(Studio, user.studio_id)
    rows = db.scalars(select(ClassSession).where(ClassSession.studio_id == studio.id, ClassSession.starts_at > now)
                      .order_by(ClassSession.starts_at)).all()
    taken = logic.taken_map(db, [s.id for s in rows])
    series = db.scalars(select(Series).where(Series.studio_id == studio.id, Series.is_active).order_by(Series.time)).all()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    counts = dict(db.execute(select(Booking.status, func.count()).join(ClassSession, ClassSession.id == Booking.session_id)
                             .where(ClassSession.studio_id == studio.id, ClassSession.starts_at >= month_start)
                             .group_by(Booking.status)).all())
    return {"studio": out_studio(studio, {s.discipline for s in series}),
            "sessions": [out_session(s, taken.get(s.id, 0), full=True) for s in rows],
            "series": [{"id": s.id, "discipline": s.discipline, "title": s.title,
                        "days": [int(x) for x in s.days.split(",") if x != ""], "time": s.time, "duration": s.duration,
                        "capacity": s.capacity, "spots": s.gopass_spots, "credits": s.credits,
                        "reference_price": s.reference_price} for s in series],
            "stats": {"upcoming": sum(1 for s in rows if s.status == "open"),
                      "bookings": counts.get("booked", 0) + counts.get("attended", 0),
                      "attended": counts.get("attended", 0)}}


class ClassIn(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    discipline: str
    time: str
    duration: int = Field(ge=15, le=180)
    capacity: int = Field(ge=1, le=60)
    spots: int = Field(ge=1, le=60)
    reference_price: int = Field(ge=100, le=50000)
    days: list[int] = []
    date: str | None = None


def check_class(data):
    if data.discipline not in DISCIPLINES or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", data.time):
        raise HTTPException(422, "badClass")
    if data.spots > data.capacity:
        raise HTTPException(422, "badCap")


@router.post("/studio/series")
def create_series(data: ClassIn, user: User = Depends(studio_user), db: Session = Depends(get_db)):
    check_class(data)
    days = sorted({d for d in data.days if 0 <= d <= 6})
    if not days:
        raise HTTPException(422, "badDays")
    db.add(Series(studio_id=user.studio_id, discipline=data.discipline, title=data.title.strip(),
                  days=",".join(str(d) for d in days), time=data.time, duration=data.duration,
                  capacity=data.capacity, gopass_spots=data.spots, reference_price=data.reference_price,
                  credits=logic.credits_for(data.reference_price)))
    db.commit()
    logic.ensure_sessions(db)
    return {"ok": True}


@router.delete("/studio/series/{series_id}")
def delete_series(series_id: int, tasks: BackgroundTasks, user: User = Depends(studio_user),
                  db: Session = Depends(get_db)):
    series = db.scalar(select(Series).where(Series.id == series_id, Series.studio_id == user.studio_id))
    if not series:
        raise HTTPException(404, "eGone")
    series.is_active = False
    affected = []
    for sess in db.scalars(select(ClassSession).where(ClassSession.series_id == series.id, ClassSession.status == "open",
                                                      ClassSession.starts_at > now_local())).all():
        affected += logic.cancel_session(db, sess)
    db.commit()
    notify_cancelled(tasks, affected)
    return {"ok": True}


@router.post("/studio/sessions")
def create_session(data: ClassIn, user: User = Depends(studio_user), db: Session = Depends(get_db)):
    check_class(data)
    try:
        start = datetime.strptime(f"{data.date} {data.time}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        raise HTTPException(422, "badClass")
    if start <= now_local() or start > now_local() + timedelta(days=60):
        raise HTTPException(422, "badClass")
    db.add(ClassSession(studio_id=user.studio_id, discipline=data.discipline, title=data.title.strip(), starts_at=start,
                        duration=data.duration, capacity=data.capacity, gopass_spots=data.spots,
                        reference_price=data.reference_price, credits=logic.credits_for(data.reference_price)))
    db.commit()
    return {"ok": True}


class SpotsIn(BaseModel):
    spots: int = Field(ge=1, le=60)


@router.patch("/studio/sessions/{session_id}")
def edit_session(session_id: int, data: SpotsIn, user: User = Depends(studio_user), db: Session = Depends(get_db)):
    sess = own_session(db, user, session_id, lock=True)
    taken = logic.taken_map(db, [sess.id]).get(sess.id, 0)
    if data.spots < taken or data.spots > sess.capacity:
        raise HTTPException(422, "badSlot")
    sess.gopass_spots = data.spots
    db.commit()
    return {"ok": True}


@router.post("/studio/sessions/{session_id}/cancel")
def cancel_session(session_id: int, tasks: BackgroundTasks, user: User = Depends(studio_user),
                   db: Session = Depends(get_db)):
    sess = own_session(db, user, session_id, lock=True)
    if sess.status != "open" or sess.starts_at <= now_local():
        raise HTTPException(404, "eGone")
    affected = logic.cancel_session(db, sess)
    db.commit()
    notify_cancelled(tasks, affected)
    return {"ok": True}


@router.post("/studio/sessions/{session_id}/restore")
def restore_session(session_id: int, user: User = Depends(studio_user), db: Session = Depends(get_db)):
    sess = own_session(db, user, session_id)
    if sess.status != "cancelled" or sess.starts_at <= now_local():
        raise HTTPException(404, "eGone")
    sess.status = "open"
    db.commit()
    return {"ok": True}


class CodeIn(BaseModel):
    code: str = Field(min_length=3, max_length=20)


@router.post("/studio/checkin")
def checkin(data: CodeIn, user: User = Depends(studio_user), db: Session = Depends(get_db)):
    booking = rule(logic.check_in, db, user.studio_id, data.code)
    return {"name": booking.user.name, "title": booking.session.title}


@router.get("/studio/statement")
def studio_statement(month: str, user: User = Depends(studio_user), db: Session = Depends(get_db)):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month")
    return logic.statement(db, db.get(Studio, user.studio_id), month)


# ---------------------------------------------------------------- administration
@router.get("/admin/overview")
def overview(admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    def count(model, *where):
        return db.scalar(select(func.count()).select_from(model).where(*where))
    studios_ = db.scalars(select(Studio).order_by(Studio.city, Studio.name)).all()
    series = db.scalars(select(Series).where(Series.is_active).order_by(Series.studio_id, Series.time)).all()
    names = {s.id: s.name for s in studios_}
    return {
        "counts": {"members": count(User, User.role == "member", User.is_active), "studios": count(Studio, Studio.is_active),
                   "bookings": count(Booking, Booking.status.in_(logic.ACTIVE)), "paid": count(Payment, Payment.status == "paid")},
        "studios": [out_studio(s) for s in studios_],
        "plans": [out_plan(p) for p in db.scalars(select(Plan).order_by(Plan.sort, Plan.id)).all()],
        "series": [{"id": s.id, "studio": names.get(s.studio_id, ""), "title": s.title, "discipline": s.discipline,
                    "reference_price": s.reference_price, "credits": s.credits} for s in series],
        "applications": [{"id": a.id, "name": a.name, "city": a.city, "email": a.email, "disciplines": a.disciplines,
                          "handled": a.handled, "at": a.created_at.isoformat()}
                         for a in db.scalars(select(StudioApplication).order_by(StudioApplication.id.desc()).limit(100)).all()],
        "messages": [{"id": m.id, "name": m.name, "email": m.email, "message": m.message, "handled": m.handled,
                      "at": m.created_at.isoformat()}
                     for m in db.scalars(select(ContactMessage).order_by(ContactMessage.id.desc()).limit(100)).all()],
        "payments": [{"id": p.id, "email": p.user.email, "plan": p.plan.name_fr, "amount": p.amount, "method": p.method,
                      "status": p.status, "at": p.created_at.isoformat()}
                     for p in db.scalars(select(Payment).order_by(Payment.id.desc()).limit(100)).all()],
    }


class StudioIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    city: str = Field(min_length=2, max_length=80)
    area: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=2000)
    manager_name: str = Field(min_length=1, max_length=80)
    manager_email: EmailStr
    manager_password: str = Field(min_length=8, max_length=128)


@router.post("/admin/studios")
def add_studio(data: StudioIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    email = data.manager_email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "eEmailUsed")
    studio = Studio(name=data.name.strip(), city=data.city.strip(), area=data.area.strip(),
                    description=data.description.strip(), payout_rate=settings.payout_rate)
    db.add(studio)
    db.flush()
    db.add(User(name=data.manager_name.strip(), email=email, password_hash=hash_password(data.manager_password),
                role="studio", studio_id=studio.id))
    log(db, admin, "studio.create", studio.name)
    db.commit()
    return {"ok": True}


class StudioPatch(BaseModel):
    active: bool | None = None
    payout_rate: float | None = Field(default=None, ge=0, le=1)


@router.patch("/admin/studios/{studio_id}")
def edit_studio(studio_id: int, data: StudioPatch, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    studio = db.get(Studio, studio_id)
    if not studio:
        raise HTTPException(404, "eGone")
    if data.active is not None:
        studio.is_active = data.active
    if data.payout_rate is not None:
        studio.payout_rate = data.payout_rate
    log(db, admin, "studio.edit", f"{studio.name} {data.model_dump(exclude_none=True)}")
    db.commit()
    return {"ok": True}


class CreditsIn(BaseModel):
    credits: int = Field(ge=1, le=100)


@router.patch("/admin/series/{series_id}")
def edit_series_credits(series_id: int, data: CreditsIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    """Gopass fixe le prix en crédits. Les réservations déjà faites gardent leur prix."""
    series = db.get(Series, series_id)
    if not series:
        raise HTTPException(404, "eGone")
    series.credits = data.credits
    for sess in db.scalars(select(ClassSession).where(ClassSession.series_id == series.id,
                                                      ClassSession.starts_at > now_local())).all():
        sess.credits = data.credits
    log(db, admin, "series.credits", f"{series.title} -> {data.credits}")
    db.commit()
    return {"ok": True}


class PlanIn(BaseModel):
    name_fr: str = Field(min_length=1, max_length=60)
    name_en: str = Field(min_length=1, max_length=60)
    name_ar: str = Field(min_length=1, max_length=60)
    desc_fr: str = Field(default="", max_length=200)
    desc_en: str = Field(default="", max_length=200)
    desc_ar: str = Field(default="", max_length=200)
    credits: int = Field(ge=1, le=1000)
    price_dzd: int = Field(ge=100, le=1000000)
    is_active: bool = True
    sort: int = 0


@router.post("/admin/plans")
def add_plan(data: PlanIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    db.add(Plan(**data.model_dump()))
    log(db, admin, "plan.create", data.name_fr)
    db.commit()
    return {"ok": True}


@router.put("/admin/plans/{plan_id}")
def edit_plan(plan_id: int, data: PlanIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    plan = db.get(Plan, plan_id)
    if not plan:
        raise HTTPException(404, "eGone")
    for key, value in data.model_dump().items():
        setattr(plan, key, value)
    log(db, admin, "plan.edit", f"{plan.name_fr} {plan.credits} cr / {plan.price_dzd} DA")
    db.commit()
    return {"ok": True}


@router.get("/admin/members")
def members(q: str = "", admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    query = select(User).where(User.role == "member", User.is_active)
    if q.strip():
        like = f"%{q.strip().lower()}%"
        query = query.where(or_(func.lower(User.email).like(like), func.lower(User.name).like(like)))
    rows = db.scalars(query.order_by(User.id.desc()).limit(50)).all()
    return [{"id": u.id, "name": u.name, "email": u.email, "credits": u.credits,
             "plan": u.plan.name_fr if u.plan else None} for u in rows]


class AdjustIn(BaseModel):
    delta: int = Field(ge=-1000, le=1000)
    reason: str = Field(min_length=3, max_length=200)


@router.post("/admin/members/{user_id}/credits")
def adjust(user_id: int, data: AdjustIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.id == user_id, User.role == "member").with_for_update())
    if not user or data.delta == 0 or user.credits + data.delta < 0:
        raise HTTPException(422, "badAdjust")
    logic.move_credits(db, user, data.delta, "Ajustement : " + data.reason.strip(), admin.id)
    log(db, admin, "credits.adjust", f"{user.email} {data.delta:+d} : {data.reason.strip()}")
    db.commit()
    return {"ok": True}


class CashIn(BaseModel):
    email: EmailStr
    plan_id: int


@router.post("/admin/payments/cash")
def cash(data: CashIn, tasks: BackgroundTasks, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == data.email.lower(), User.role == "member", User.is_active))
    plan = db.get(Plan, data.plan_id)
    if not user or not plan:
        raise HTTPException(404, "eNoMember")
    payment = Payment(user_id=user.id, plan_id=plan.id, amount=plan.price_dzd, credits=plan.credits, method="cash")
    db.add(payment)
    db.flush()
    logic.apply_payment(db, payment, admin.id)
    log(db, admin, "payment.cash", f"{user.email} {plan.name_fr} {plan.price_dzd} DA")
    db.commit()
    tasks.add_task(logic.send_mail, user.email, "paid", user.lang, plan=plan.name_fr, credits=plan.credits)
    return {"ok": True}


@router.get("/admin/statements")
def statements(month: str, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month")
    out = []
    for studio in db.scalars(select(Studio).order_by(Studio.name)).all():
        st = logic.statement(db, studio, month)
        out.append({"studio": studio.name, "count": len(st["lines"]), "total": st["total"]})
    return out


class HandledIn(BaseModel):
    kind: str
    id: int


@router.post("/admin/handled")
def handled(data: HandledIn, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    model = {"application": StudioApplication, "message": ContactMessage}.get(data.kind)
    row = db.get(model, data.id) if model else None
    if not row:
        raise HTTPException(404, "eGone")
    row.handled = True
    db.commit()
    return {"ok": True}


@router.get("/admin/ledger/{user_id}")
def ledger(user_id: int, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(CreditEntry).where(CreditEntry.user_id == user_id).order_by(CreditEntry.id.desc()).limit(100)).all()
    return [{"delta": e.delta, "reason": e.reason, "at": e.created_at.isoformat()} for e in rows]
