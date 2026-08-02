from app.models import TaskStatus
from app.notifier import NotifierResponse, ScriptedNotifier
from app.service import ReminderTaskService

OK = NotifierResponse(succeeded=True, provider_reference="msg_test")
TRANSIENT = NotifierResponse(succeeded=False, error_code="provider_timeout")
PERMANENT = NotifierResponse(succeeded=False, error_code="invalid_recipient")


def create(db, key="lease-1", max_attempts=3):
    service = ReminderTaskService(db)
    task, _ = service.create_task(
        idempotency_key=key,
        loan_id="loan-9",
        channel="email",
        message="overdue",
        max_attempts=max_attempts,
    )
    return service, task


def test_claim_leases_the_task(db):
    service, task = create(db)

    claimed = service.claim_due_tasks("worker-a", limit=10)

    assert len(claimed) == 1
    assert claimed[0].id == task.id
    assert claimed[0].attempt_number == 1

    db.refresh(task)
    assert task.status == TaskStatus.LEASED
    assert task.owner_id == "worker-a"
    assert task.lease_token == claimed[0].lease_token
    assert task.lease_expires_at is not None
    assert task.attempts_made == 1


def test_leased_task_is_not_claimable_again_before_expiry(db):
    service, task = create(db)
    service.claim_due_tasks("worker-a", limit=10)

    second_claim = service.claim_due_tasks("worker-b", limit=10)

    assert second_claim == []


def test_successful_completion_marks_done(db):
    service, task = create(db)
    [claimed] = service.claim_due_tasks("worker-a", limit=10)

    result = service.complete_task(claimed.id, claimed.lease_token, "worker-a", OK)

    assert result.status == TaskStatus.DONE
    assert result.owner_id is None
    assert result.lease_token is None
    assert result.lease_expires_at is None


def test_transient_failure_schedules_retry_and_releases_lease(db):
    service, task = create(db)
    [claimed] = service.claim_due_tasks("worker-a", limit=10)

    result = service.complete_task(claimed.id, claimed.lease_token, "worker-a", TRANSIENT)

    assert result.status == TaskStatus.RETRY_SCHEDULED
    assert result.next_attempt_at is not None
    # The lease is released even on failure - a retry should not require
    # waiting out a lease that nobody is using anymore.
    assert result.owner_id is None
    assert result.lease_token is None


def test_permanent_failure_goes_straight_to_dead(db):
    service, task = create(db, max_attempts=5)
    [claimed] = service.claim_due_tasks("worker-a", limit=10)

    result = service.complete_task(claimed.id, claimed.lease_token, "worker-a", PERMANENT)

    assert result.status == TaskStatus.DEAD
    assert result.attempts_made == 1  # stopped because of the error kind, not the count


def test_exhausted_after_max_attempts(db):
    service, task = create(db, max_attempts=2)

    for _ in range(2):
        [claimed] = service.claim_due_tasks("worker-a", limit=10)
        task.next_attempt_at = None
        db.commit()
        result = service.complete_task(claimed.id, claimed.lease_token, "worker-a", TRANSIENT)

    assert result.status == TaskStatus.DEAD
    assert result.attempts_made == 2


def test_notifier_key_includes_attempt_number(db):
    notifier = ScriptedNotifier([TRANSIENT, OK])
    service, task = create(db, key="lease-key-check")

    [claimed_1] = service.claim_due_tasks("worker-a", limit=10)
    response_1 = notifier.send(
        idempotency_key=f"{claimed_1.id}:{claimed_1.attempt_number}",
        channel="email",
        message="x",
    )
    service.complete_task(claimed_1.id, claimed_1.lease_token, "worker-a", response_1)

    task.next_attempt_at = None
    db.commit()

    [claimed_2] = service.claim_due_tasks("worker-a", limit=10)
    response_2 = notifier.send(
        idempotency_key=f"{claimed_2.id}:{claimed_2.attempt_number}",
        channel="email",
        message="x",
    )
    service.complete_task(claimed_2.id, claimed_2.lease_token, "worker-a", response_2)

    assert notifier.calls == [f"{task.id}:1", f"{task.id}:2"]
