"""What happens when a worker takes a task and then goes silent - killed pod,
lost network, whatever. No graceful failure report ever arrives; the only
signal anyone gets is that the lease runs out.
"""

from datetime import timedelta

from app.clock import utcnow
from app.models import ReminderTask, TaskAttempt, TaskStatus
from app.notifier import NotifierResponse
from app.service import ReminderTaskService

OK = NotifierResponse(succeeded=True, provider_reference="msg_test")


def create(db, key="expiry-1"):
    service = ReminderTaskService(db)
    task, _ = service.create_task(
        idempotency_key=key,
        loan_id="loan-9",
        channel="email",
        message="overdue",
    )
    return service, task


def expire_the_lease(db, task_id: int) -> None:
    # Standing in for time passing - moves the lease into the past directly
    # rather than sleeping LEASE_SECONDS in a test.
    db.query(ReminderTask).filter(ReminderTask.id == task_id).update(
        {ReminderTask.lease_expires_at: utcnow() - timedelta(seconds=1)}
    )
    db.commit()


def test_expired_lease_is_reclaimable_by_another_worker(db):
    service, task = create(db)
    [claimed] = service.claim_due_tasks("worker-doomed", limit=10)
    expire_the_lease(db, task.id)

    reclaimed = service.claim_due_tasks("worker-rescuer", limit=10)

    assert len(reclaimed) == 1
    assert reclaimed[0].id == task.id
    assert reclaimed[0].lease_token != claimed.lease_token

    db.refresh(task)
    assert task.owner_id == "worker-rescuer"
    assert task.attempts_made == 2  # once for the doomed attempt, once for this one


def test_reclaim_leaves_a_record_of_the_abandoned_attempt(db):
    service, task = create(db)
    service.claim_due_tasks("worker-doomed", limit=10)
    expire_the_lease(db, task.id)

    service.claim_due_tasks("worker-rescuer", limit=10)

    attempts = (
        db.query(TaskAttempt)
        .filter(TaskAttempt.task_id == task.id)
        .order_by(TaskAttempt.attempt_number)
        .all()
    )
    assert len(attempts) == 1
    assert attempts[0].worker_id == "worker-doomed"
    assert attempts[0].succeeded == 0
    assert attempts[0].error_code == "lease_expired"


def test_rescuer_can_still_complete_the_task_normally(db):
    service, task = create(db)
    service.claim_due_tasks("worker-doomed", limit=10)
    expire_the_lease(db, task.id)
    [rescued] = service.claim_due_tasks("worker-rescuer", limit=10)

    result = service.complete_task(rescued.id, rescued.lease_token, "worker-rescuer", OK)

    assert result.status == TaskStatus.DONE


def test_unexpired_lease_is_left_alone(db):
    """The whole point of a lease TTL: a worker that is merely slow, not dead,
    must not have its work stolen out from under it.
    """
    service, task = create(db)
    service.claim_due_tasks("worker-slow-but-alive", limit=10)

    still_nothing = service.claim_due_tasks("worker-impatient", limit=10)

    assert still_nothing == []
