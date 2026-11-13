"""Configuration, base de données, modèles et sécurité."""
import os
import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import bcrypt
import jwt
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


# ---------------------------------------------------------------- configuration
def _env(name, default=""):
    return os.environ.get(name, default).strip()


class Settings:
    database_url = _env("DATABASE_URL", "sqlite:///./gopass.db")
    secret_key = _env("SECRET_KEY")
    public_url = _env("PUBLIC_URL").rstrip("/")
    admin_email = _env("ADMIN_EMAIL").lower()
    admin_password = _env("ADMIN_PASSWORD")
    chargily_secret_key = _env("CHARGILY_SECRET_KEY")
    chargily_base_url = _env("CHARGILY_BASE_URL", "https://pay.chargily.net/test/api/v2").rstrip("/")
    credit_value = int(_env("CREDIT_VALUE_DZD", "400"))
    cancel_hours = int(_env("CANCEL_HOURS", "12"))
    payout_rate = float(_env("PAYOUT_RATE", "0.75"))
    window_days = int(_env("BOOKING_WINDOW_DAYS", "7"))
    plan_days = int(_env("PLAN_DURATION_DAYS", "30"))
    seed_demo = _env("SEED_DEMO") == "1"
    smtp_host = _env("SMTP_HOST")
    smtp_port = int(_env("SMTP_PORT", "587"))
    smtp_user = _env("SMTP_USER")
    smtp_password = _env("SMTP_PASSWORD")
    mail_from = _env("MAIL_FROM")
    timezone = _env("TIMEZONE", "Africa/Algiers")


settings = Settings()

IS_SQLITE = settings.database_url.startswith("sqlite")
if not settings.secret_key:
    if not IS_SQLITE:
        raise RuntimeError("SECRET_KEY est obligatoire en production : ajoutez-la dans les variables Railway.")
    settings.secret_key = secrets.token_hex(32)  # développement local uniquement


def _db_url(url):
    # Railway fournit postgres:// ou postgresql:// ; SQLAlchemy attend le pilote psycopg (v3).
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


engine = create_engine(
    _db_url(settings.database_url),
    pool_pre_ping=True,
    connect_args={"check_same_thread": False} if IS_SQLITE else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def now_local():
    """Heure locale d'Algérie, sans fuseau : toutes les dates de cours sont stockées ainsi."""
    return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None)


# ---------------------------------------------------------------- modèles
class Base(DeclarativeBase):
    pass


DISCIPLINES = ["pilates", "yoga", "barre", "boxe", "danse", "natation", "cross", "stretching"]


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    name_fr: Mapped[str] = mapped_column(String(60))
    name_en: Mapped[str] = mapped_column(String(60))
    name_ar: Mapped[str] = mapped_column(String(60))
    desc_fr: Mapped[str] = mapped_column(String(200), default="")
    desc_en: Mapped[str] = mapped_column(String(200), default="")
    desc_ar: Mapped[str] = mapped_column(String(200), default="")
    credits: Mapped[int] = mapped_column(Integer)
    price_dzd: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class Studio(Base):
    __tablename__ = "studios"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(80))
    area: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    payout_rate: Mapped[float] = mapped_column(Float, default=0.75)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(10), default="member")  # member | studio | admin
    lang: Mapped[str] = mapped_column(String(2), default="fr")
    credits: Mapped[int] = mapped_column(Integer, default=0)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    studio_id: Mapped[int | None] = mapped_column(ForeignKey("studios.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    plan: Mapped[Plan | None] = relationship()


class Series(Base):
    """Cours récurrent : republié chaque semaine aux jours indiqués (0 = dimanche … 6 = samedi)."""
    __tablename__ = "series"
    id: Mapped[int] = mapped_column(primary_key=True)
    studio_id: Mapped[int] = mapped_column(ForeignKey("studios.id"), index=True)
    discipline: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(120))
    days: Mapped[str] = mapped_column(String(20))  # "0,2,4"
    time: Mapped[str] = mapped_column(String(5))  # "18:30"
    duration: Mapped[int] = mapped_column(Integer)
    capacity: Mapped[int] = mapped_column(Integer)
    gopass_spots: Mapped[int] = mapped_column(Integer)
    reference_price: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class ClassSession(Base):
    """Une séance datée. Les séances d'une série sont créées automatiquement pour la fenêtre de réservation."""
    __tablename__ = "sessions"
    __table_args__ = (UniqueConstraint("series_id", "starts_at", name="uq_series_start"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    studio_id: Mapped[int] = mapped_column(ForeignKey("studios.id"), index=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id"), nullable=True)
    discipline: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(120))
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    duration: Mapped[int] = mapped_column(Integer)
    capacity: Mapped[int] = mapped_column(Integer)
    gopass_spots: Mapped[int] = mapped_column(Integer)
    reference_price: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | cancelled
    studio: Mapped[Studio] = relationship()


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"), index=True)
    credits: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    # booked | cancelled | studio_cancelled | attended
    status: Mapped[str] = mapped_column(String(20), default="booked")
    refunded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    session: Mapped[ClassSession] = relationship()
    user: Mapped[User] = relationship()


class CreditEntry(Base):
    """Journal des mouvements de crédits : chaque débit ou crédit laisse une trace."""
    __tablename__ = "credit_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(200))
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plans.id"))
    amount: Mapped[int] = mapped_column(Integer)
    credits: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(10))  # online | cash
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending | paid | failed
    provider_ref: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    recorded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user: Mapped[User] = relationship()
    plan: Mapped[Plan] = relationship()


class StudioApplication(Base):
    __tablename__ = "studio_applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(200))
    disciplines: Mapped[str] = mapped_column(String(300))
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class ContactMessage(Base):
    __tablename__ = "contact_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


class AdminLog(Base):
    __tablename__ = "admin_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_local)


# ---------------------------------------------------------------- sécurité
COOKIE = "gp_session"
TOKEN_DAYS = 30


def hash_password(password):
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def check_password(password, hashed):
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


def make_token(user_id):
    payload = {"sub": str(user_id), "exp": datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def read_token(token):
    try:
        return int(jwt.decode(token, settings.secret_key, algorithms=["HS256"])["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
