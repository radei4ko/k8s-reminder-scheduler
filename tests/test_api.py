import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app


@pytest.fixture
def client(db):
    app.dependency_overrides[get_db] = lambda: db
    yield TestClient(app)
    app.dependency_overrides.clear()


def payload(key="api-1", loan="loan-7", channel="email"):
    return {"idempotency_key": key, "loan_id": loan, "channel": channel, "message": "overdue"}


def test_create_returns_201(client):
    response = client.post("/reminders", json=payload())
    assert response.status_code == 201
    assert response.json()["status"] == "pending"


def test_repeat_returns_200_same_task(client):
    first = client.post("/reminders", json=payload(key="api-repeat"))
    second = client.post("/reminders", json=payload(key="api-repeat"))
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_conflict_returns_409(client):
    client.post("/reminders", json=payload(key="api-conflict", channel="email"))
    response = client.post("/reminders", json=payload(key="api-conflict", channel="sms"))
    assert response.status_code == 409


def test_invalid_channel_rejected(client):
    response = client.post("/reminders", json=payload(key="api-badchan", channel="carrier_pigeon"))
    assert response.status_code == 422


def test_unknown_task_404(client):
    assert client.get("/reminders/999999").status_code == 404


def test_stats(client):
    client.post("/reminders", json=payload(key="api-stats-1"))
    client.post("/reminders", json=payload(key="api-stats-2", loan="loan-8"))
    body = client.get("/stats").json()
    assert body["total"] == 2
    assert body["by_status"]["pending"] == 2
    assert body["claimable_now"] == 2
