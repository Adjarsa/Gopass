"""Tests des règles de gestion (base SQLite temporaire)."""
import hashlib, hmac, json, os, tempfile
from datetime import datetime, timedelta

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ.update(ADMIN_EMAIL="admin@test.dz", ADMIN_PASSWORD="motdepasse1", CHARGILY_SECRET_KEY="test_sk", CANCEL_HOURS="12")

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import core, logic
from app.main import app


def client():
    return TestClient(app)


def login(c, email, password="motdepasse1"):
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_full_flow():
    with client() as admin, client() as member, client() as studio, client() as other:
        login(admin, "admin@test.dz")
        # --- studio créé par l'administrateur
        r = admin.post("/api/admin/studios", json={"name": "Studio Test", "city": "Alger", "area": "Hydra",
                       "manager_name": "Gérante", "manager_email": "studio@test.dz", "manager_password": "motdepasse1"})
        assert r.status_code == 200, r.text
        login(studio, "studio@test.dz")
        # --- cours récurrent tous les jours : 2 places Gopass sur 6, tarif 3 200 DA -> 8 crédits
        body = {"title": "Reformer", "discipline": "pilates", "time": "23:55", "duration": 50, "capacity": 6,
                "spots": 2, "reference_price": 3200, "days": [0, 1, 2, 3, 4, 5, 6]}
        assert studio.post("/api/studio/series", json=dict(body, spots=9)).status_code == 422   # > capacité
        assert studio.post("/api/studio/series", json=dict(body, days=[])).status_code == 422
        assert studio.post("/api/studio/series", json=body).status_code == 200
        dash = studio.get("/api/studio/dashboard").json()
        assert dash["series"][0]["credits"] == 8 and len(dash["sessions"]) >= 6
        sessions = member.get("/api/sessions").json()
        far, near = sessions[-1], sessions[0]                   # la plus lointaine et la plus proche
        assert far["credits"] == 8 and far["left"] == 2
        # --- compte membre
        assert member.post("/api/bookings", json={"session_id": far["id"]}).status_code == 401
        assert member.post("/api/auth/register", json={"name": "Lina", "email": "bad", "password": "motdepasse1"}).status_code == 422
        assert member.post("/api/auth/register", json={"name": "Lina", "email": "lina@test.dz", "password": "court"}).status_code == 422
        r = member.post("/api/auth/register", json={"name": "Lina", "email": "Lina@Test.dz", "password": "motdepasse1"})
        assert r.status_code == 200 and r.json()["credits"] == 0
        assert other.post("/api/auth/register", json={"name": "X", "email": "lina@test.dz", "password": "motdepasse1"}).status_code == 409
        assert member.post("/api/bookings", json={"session_id": far["id"]}).status_code == 402   # pas de crédits
        # --- paiement en espèces enregistré par l'administrateur (formule 25 crédits)
        plans = member.get("/api/plans").json()
        regular = [p for p in plans if p["credits"] == 25][0]
        assert member.post("/api/admin/payments/cash", json={"email": "lina@test.dz", "plan_id": regular["id"]}).status_code == 403
        assert admin.post("/api/admin/payments/cash", json={"email": "lina@test.dz", "plan_id": regular["id"]}).status_code == 200
        assert member.get("/api/me").json()["credits"] == 25
        # --- réservation, doublon, crédits débités
        me = member.post("/api/bookings", json={"session_id": far["id"]}).json()
        assert me["credits"] == 17 and me["bookings"][0]["code"].startswith("GP-")
        assert member.post("/api/bookings", json={"session_id": far["id"]}).status_code == 400   # déjà réservé
        # --- dernière place : un second membre la prend, un troisième est refusé
        other.post("/api/auth/register", json={"name": "Sara", "email": "sara@test.dz", "password": "motdepasse1"})
        admin.post("/api/admin/payments/cash", json={"email": "sara@test.dz", "plan_id": regular["id"]})
        assert other.post("/api/bookings", json={"session_id": far["id"]}).status_code == 200
        with client() as third:
            third.post("/api/auth/register", json={"name": "Ines", "email": "ines@test.dz", "password": "motdepasse1"})
            admin.post("/api/admin/payments/cash", json={"email": "ines@test.dz", "plan_id": regular["id"]})
            r = third.post("/api/bookings", json={"session_id": far["id"]})
            assert r.status_code == 400 and r.json()["detail"] == "eFull"
        # --- le studio ne peut pas descendre sous les places prises ni dépasser la capacité
        assert studio.patch(f"/api/studio/sessions/{far['id']}", json={"spots": 1}).status_code == 422
        assert studio.patch(f"/api/studio/sessions/{far['id']}", json={"spots": 7}).status_code == 422
        assert studio.patch(f"/api/studio/sessions/{far['id']}", json={"spots": 3}).status_code == 200
        # --- annulation à plus de 12 h : crédits rendus
        bid = me["bookings"][0]["id"]
        assert other.post(f"/api/bookings/{bid}/cancel").status_code == 404                     # pas sa réservation
        me = member.post(f"/api/bookings/{bid}/cancel").json()
        assert me["credits"] == 25 and me["bookings"][0]["status"] == "cancelled" and me["bookings"][0]["refunded"]
        # --- annulation à moins de 12 h : crédits perdus
        with core.SessionLocal() as db:
            s = db.get(core.ClassSession, near["id"])
            soon = core.now_local() + timedelta(hours=2)
            if soon.date() != core.now_local().date():      # tard le soir : rester sur la même journée
                soon = core.now_local().replace(hour=23, minute=59)
            s.starts_at = soon
            db.commit()
        me = member.post("/api/bookings", json={"session_id": near["id"]}).json()
        assert me["credits"] == 17
        late = me["bookings"][0]
        assert member.post(f"/api/bookings/{late['id']}/cancel").json()["credits"] == 17
        # --- validation d'une arrivée : mauvais studio, puis bon code, puis code déjà utilisé
        me = member.post("/api/bookings", json={"session_id": near["id"]}).json()
        code = me["bookings"][0]["code"]
        assert me["credits"] == 9
        assert studio.post("/api/studio/checkin", json={"code": "GP-ZZZZZZ"}).status_code == 404
        assert studio.post("/api/studio/checkin", json={"code": code.lower()[3:]}).json()["name"] == "Lina"
        assert studio.post("/api/studio/checkin", json={"code": code}).status_code == 400
        # --- le studio annule une séance : remboursement automatique
        sara = other.get("/api/me").json()
        assert sara["credits"] == 17
        assert studio.post(f"/api/studio/sessions/{far['id']}/cancel").status_code == 200
        sara = other.get("/api/me").json()
        assert sara["credits"] == 25 and sara["bookings"][0]["status"] == "studio_cancelled"
        assert far["id"] not in [s["id"] for s in member.get("/api/sessions").json()]
        assert studio.post(f"/api/studio/sessions/{far['id']}/restore").status_code == 200
        # --- suppression de la série : les séances à venir disparaissent du catalogue
        sid = studio.get("/api/studio/dashboard").json()["series"][0]["id"]
        assert studio.delete(f"/api/studio/series/{sid}").status_code == 200
        assert member.get("/api/sessions").json() == []
        # --- relevé : la présence validée et l'annulation tardive sont dues au studio (75 % de 3 200 DA)
        st = studio.get("/api/studio/statement", params={"month": core.now_local().strftime("%Y-%m")}).json()
        kinds = sorted(l["kind"] for l in st["lines"])
        assert kinds == ["attended", "late_cancel"] and st["total"] == 4800
        # --- ajustement de crédits par l'administrateur, avec motif
        uid = [m for m in admin.get("/api/admin/members", params={"q": "lina"}).json()][0]["id"]
        assert admin.post(f"/api/admin/members/{uid}/credits", json={"delta": -100, "reason": "test"}).status_code == 422
        assert admin.post(f"/api/admin/members/{uid}/credits", json={"delta": 3, "reason": "geste commercial"}).status_code == 200
        assert member.get("/api/me").json()["credits"] == 12
        # --- accès réservés
        assert member.get("/api/studio/dashboard").status_code == 403
        assert studio.get("/api/admin/overview").status_code == 403
        assert admin.get("/api/admin/overview").json()["counts"]["studios"] == 1


def test_online_payment_webhook(monkeypatch):
    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"id": "chk_123", "checkout_url": "https://pay.example/chk_123"}
    sent = {}
    def fake_post(url, **kw):
        sent.update(url=url, **kw)
        return FakeResponse()
    monkeypatch.setattr("app.api.httpx.post", fake_post)
    with client() as c:
        c.post("/api/auth/register", json={"name": "Nour", "email": "nour@test.dz", "password": "motdepasse1"})
        plan = c.get("/api/plans").json()[0]
        r = c.post("/api/payments/checkout", json={"plan_id": plan["id"]})
        assert r.json()["url"] == "https://pay.example/chk_123"
        assert sent["json"]["amount"] == plan["price"] and sent["json"]["currency"] == "dzd"
        assert sent["headers"]["Authorization"] == "Bearer test_sk"
        assert c.get("/api/me").json()["credits"] == 0                       # rien tant que le paiement n'est pas confirmé
        payload = json.dumps({"type": "checkout.paid", "data": {"id": "chk_123", "status": "paid"}}).encode()
        good = hmac.new(b"test_sk", payload, hashlib.sha256).hexdigest()
        assert c.post("/api/webhooks/chargily", content=payload).status_code == 400                       # sans signature
        assert c.post("/api/webhooks/chargily", content=payload, headers={"signature": "x" * 64}).status_code == 403
        assert c.post("/api/webhooks/chargily", content=payload, headers={"signature": good}).status_code == 200
        assert c.get("/api/me").json()["credits"] == plan["credits"]
        assert c.post("/api/webhooks/chargily", content=payload, headers={"signature": good}).status_code == 200
        assert c.get("/api/me").json()["credits"] == plan["credits"]         # webhook rejoué : pas de double crédit


def test_credits_rule_and_expiry():
    assert [logic.credits_for(p) for p in (200, 400, 800, 1200, 2800, 3200, 4000, 4800)] == [1, 1, 2, 3, 7, 8, 10, 12]
    with client() as c:
        c.post("/api/auth/register", json={"name": "Exp", "email": "exp@test.dz", "password": "motdepasse1"})
        with core.SessionLocal() as db:
            u = db.scalar(select(core.User).where(core.User.email == "exp@test.dz"))
            u.credits, u.plan_expires_at = 7, core.now_local() - timedelta(days=1)
            db.commit()
        assert c.get("/api/me").json()["credits"] == 0                       # formule expirée : crédits perdus
