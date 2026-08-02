"""Several worker pods hammering the same table must never both send the
same reminder. Real threads, real database - mocks cannot catch a race in
the locking logic.
"""

import threading

from sqlalchemy import func

from app.database import SessionLocal
from app.models import ReminderTask, TaskAttempt
from app.notifier import NotifierResponse
from app.service import ReminderTaskService
from app.worker import run_one_tick

TASK_COUNT = 40


class AlwaysOk:
    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def send(self, *, idempotency_key, channel, message):
        with self._lock:
            self.calls.append(idempotency_key)
        return NotifierResponse(succeeded=True, provider_reference="msg_test")


def test_workers_never_send_the_same_task_twice(db):
    service = ReminderTaskService(db)
    for i in range(TASK_COUNT):
        service.create_task(
            idempotency_key=f"concurrent-{i:04d}",
            loan_id=f"loan-{i}",
            channel="email",
            message="overdue",
        )

    notifier = AlwaysOk()
    errors = []

    def worker(worker_id: str):
        try:
            # Enough ticks that even a slow claim eventually drains the table;
            # ticks past the point of "nothing left" are cheap no-ops.
            for _ in range(20):
                if run_one_tick(notifier, worker_id=worker_id) == 0:
                    break
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"pod-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a worker crashed: {errors}"

    check = SessionLocal()
    try:
        statuses = dict(
            check.query(ReminderTask.id, ReminderTask.status)
            .filter(ReminderTask.idempotency_key.like("concurrent-%"))
            .all()
        )
        assert len(statuses) == TASK_COUNT
        assert {s.value for s in statuses.values()} == {"done"}, "not every task finished"

        attempt_counts = dict(
            check.query(TaskAttempt.task_id, func.count(TaskAttempt.id))
            .join(ReminderTask, ReminderTask.id == TaskAttempt.task_id)
            .filter(ReminderTask.idempotency_key.like("concurrent-%"))
            .group_by(TaskAttempt.task_id)
            .all()
        )
        assert set(attempt_counts.values()) == {1}, "some task got more than one recorded attempt"

        assert len(notifier.calls) == TASK_COUNT
        assert len(set(notifier.calls)) == TASK_COUNT, "the notifier was called twice for one task"
    finally:
        check.close()
