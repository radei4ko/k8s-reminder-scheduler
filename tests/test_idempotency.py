import pytest

from app.models import ReminderTask
from app.service import IdempotencyConflict, ReminderTaskService


def test_same_key_does_not_create_second_task(db):
    service = ReminderTaskService(db)

    first, created_first = service.create_task(
        idempotency_key="reminder-loan-1-2026-08-02",
        loan_id="loan-1",
        channel="email",
        message="You are overdue.",
    )
    second, created_second = service.create_task(
        idempotency_key="reminder-loan-1-2026-08-02",
        loan_id="loan-1",
        channel="email",
        message="You are overdue.",
    )

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert db.query(ReminderTask).count() == 1


def test_same_key_different_message_is_a_conflict(db):
    service = ReminderTaskService(db)
    service.create_task(
        idempotency_key="reminder-loan-2-2026-08-02",
        loan_id="loan-2",
        channel="email",
        message="First version.",
    )

    with pytest.raises(IdempotencyConflict):
        service.create_task(
            idempotency_key="reminder-loan-2-2026-08-02",
            loan_id="loan-2",
            channel="email",
            message="Different version.",
        )


def test_race_is_caught_by_unique_index(db, monkeypatch):
    """Same reasoning as the payment-retry-engine project: the lookup alone
    cannot prevent a race, only the unique index actually can.
    """
    service = ReminderTaskService(db)
    service.create_task(
        idempotency_key="reminder-loan-3-2026-08-02",
        loan_id="loan-3",
        channel="sms",
        message="Overdue.",
    )

    real_find = service._find_by_key
    calls = {"n": 0}

    def lying_find(key):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_find(key)

    monkeypatch.setattr(service, "_find_by_key", lying_find)

    task, created = service.create_task(
        idempotency_key="reminder-loan-3-2026-08-02",
        loan_id="loan-3",
        channel="sms",
        message="Overdue.",
    )

    assert created is False
    assert db.query(ReminderTask).count() == 1
