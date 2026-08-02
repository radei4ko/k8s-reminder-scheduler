"""The fencing token stops a zombie worker's late result from corrupting
whatever the rescuer has already done.

Scenario: worker A claims a task, gets stuck (GC pause, slow network,
whatever) past the lease TTL, worker B reclaims and finishes it. Then A
finally wakes up and reports its own result. A's report must be ignored -
without the token check it would silently overwrite B's completion.
"""

from datetime import timedelta

from app.clock import utcnow
from app.models import ReminderTask, TaskStatus
from app.notifier import NotifierResponse
from app.service import ReminderTaskService

OK = NotifierResponse(succeeded=True, provider_reference="msg_test")
TRANSIENT = NotifierResponse(succeeded=False, error_code="provider_timeout")


def expire_the_lease(db, task_id: int) -> None:
    db.query(ReminderTask).filter(ReminderTask.id == task_id).update(
        {ReminderTask.lease_expires_at: utcnow() - timedelta(seconds=1)}
    )
    db.commit()


def test_stale_completion_is_dropped_after_reclaim(db):
    service = ReminderTaskService(db)
    task, _ = service.create_task(
        idempotency_key="fencing-1",
        loan_id="loan-9",
        channel="email",
        message="overdue",
    )

    [claimed_by_a] = service.claim_due_tasks("worker-a", limit=10)
    expire_the_lease(db, task.id)
    [claimed_by_b] = service.claim_due_tasks("worker-b", limit=10)
    service.complete_task(claimed_by_b.id, claimed_by_b.lease_token, "worker-b", OK)

    # A never knew it lost the task and reports in late, with its now-stale token.
    result = service.complete_task(claimed_by_a.id, claimed_by_a.lease_token, "worker-a", TRANSIENT)

    assert result is None, "a fenced-out worker's result must be rejected, not applied"

    db.refresh(task)
    assert task.status == TaskStatus.DONE, "B's completion must stand, untouched by A"


def test_stale_completion_does_not_duplicate_attempt_history(db):
    from app.models import TaskAttempt

    service = ReminderTaskService(db)
    task, _ = service.create_task(
        idempotency_key="fencing-2",
        loan_id="loan-9",
        channel="email",
        message="overdue",
    )

    [claimed_by_a] = service.claim_due_tasks("worker-a", limit=10)
    expire_the_lease(db, task.id)
    [claimed_by_b] = service.claim_due_tasks("worker-b", limit=10)
    service.complete_task(claimed_by_b.id, claimed_by_b.lease_token, "worker-b", OK)
    service.complete_task(claimed_by_a.id, claimed_by_a.lease_token, "worker-a", TRANSIENT)

    attempts = db.query(TaskAttempt).filter(TaskAttempt.task_id == task.id).all()
    # One for the lease_expired record, one for B's real completion. A's
    # report never made it in.
    assert len(attempts) == 2
    assert {a.error_code for a in attempts} == {"lease_expired", None}
