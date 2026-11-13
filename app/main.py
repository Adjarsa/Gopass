"""Application Gopass : API + site servis par le même service."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import logic
from .api import router
from .core import Base, Plan, Series, SessionLocal, Studio, User, engine, hash_password, settings

WEB = Path(__file__).resolve().parent.parent / "web"

DEMO_STUDIOS = [
    ("Atelier Reformer", "Alger", "Hydra", "Petit studio de Pilates sur machines, groupes de six.", [
        ("pilates", "Pilates Reformer débutant", "0,2,4", "18:30", 50, 6, 3, 3200),
        ("barre", "Barre sculpt", "1,3,6", "09:00", 45, 12, 5, 1200)]),
    ("Yoga Lumière", "Alger", "El Biar", "Yoga doux, vinyasa et étirements, du matin au soir.", [
        ("yoga", "Yoga doux", "0,1,3", "10:00", 60, 16, 6, 800),
        ("stretching", "Stretching du soir", "2,4", "19:00", 45, 14, 6, 800),
        ("yoga", "Vinyasa flow", "5,6", "11:00", 60, 16, 6, 1200)]),
    ("Ring Club", "Alger", "Bab Ezzouar", "Boxe cardio et cross-training en petits groupes.", [
        ("boxe", "Boxe cardio", "1,3,6", "18:00", 60, 20, 8, 400),
        ("cross", "Cross-training", "0,2,4", "07:00", 45, 14, 6, 800)]),
    ("Studio Cadence", "Oran", "Bir El Djir", "Danse moderne et barre au sol, tous niveaux.", [
        ("danse", "Danse moderne", "1,4,6", "17:30", 60, 18, 8, 800),
        ("barre", "Barre au sol", "0,3", "12:30", 45, 12, 5, 1200)]),
    ("Bassin Bleu", "Oran", "Centre-ville", "Natation adultes et aquagym en bassin couvert.", [
        ("natation", "Natation adultes", "0,2,4,6", "08:00", 45, 10, 4, 800),
        ("natation", "Aquagym", "1,3,5", "10:30", 45, 14, 6, 400)]),
    ("Maison Souffle", "Annaba", "Centre-ville", "Pilates Reformer et yoga dans un cadre calme.", [
        ("pilates", "Pilates Reformer", "0,1,3,4", "18:00", 50, 5, 2, 2800),
        ("yoga", "Yoga du matin", "2,5,6", "08:30", 60, 12, 5, 800)]),
]


def seed():
    with SessionLocal() as db:
        if not db.scalar(select(Plan.id).limit(1)):
            db.add_all([
                Plan(name_fr="Découverte", name_en="Starter", name_ar="اكتشاف", credits=10, price_dzd=4000, sort=1,
                     desc_fr="Pour bouger de temps en temps.", desc_en="For moving every now and then.",
                     desc_ar="لمن يتحرّك من حين لآخر."),
                Plan(name_fr="Régulier", name_en="Regular", name_ar="منتظم", credits=25, price_dzd=9500, sort=2,
                     desc_fr="Pour une à deux séances par semaine.", desc_en="For one or two classes a week.",
                     desc_ar="لحصة أو حصتين في الأسبوع."),
                Plan(name_fr="Intensif", name_en="Intensive", name_ar="مكثّف", credits=45, price_dzd=16000, sort=3,
                     desc_fr="Pour s'entraîner presque tous les jours.", desc_en="For training almost every day.",
                     desc_ar="لمن يتمرّن تقريبًا كل يوم."),
            ])
        if settings.admin_email and settings.admin_password and \
                not db.scalar(select(User.id).where(User.email == settings.admin_email)):
            db.add(User(name="Admin", email=settings.admin_email, role="admin",
                        password_hash=hash_password(settings.admin_password)))
        if settings.seed_demo and not db.scalar(select(Studio.id).limit(1)):
            demo_password = os.environ.get("DEMO_PASSWORD", "").strip()
            for i, (name, city, area, desc, classes) in enumerate(DEMO_STUDIOS, start=1):
                studio = Studio(name=name, city=city, area=area, description=desc, payout_rate=settings.payout_rate)
                db.add(studio)
                db.flush()
                for d, title, days, time, dur, cap, spots, ref in classes:
                    db.add(Series(studio_id=studio.id, discipline=d, title=title, days=days, time=time, duration=dur,
                                  capacity=cap, gopass_spots=spots, reference_price=ref, credits=logic.credits_for(ref)))
                if len(demo_password) >= 8:
                    db.add(User(name=name, email=f"studio{i}@demo.gopass", role="studio", studio_id=studio.id,
                                password_hash=hash_password(demo_password)))
        db.commit()


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    seed()
    yield


app = FastAPI(title="Gopass", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)


@app.get("/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
