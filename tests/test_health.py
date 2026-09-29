from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "Lessen Pro KB Chat" in r.text
