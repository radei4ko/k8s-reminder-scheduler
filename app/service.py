"""Business logic: creating reminder tasks and moving them through claim,
lease, complete/fail, and lease-expiry reclaim.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import utcnow
from app.config import LEASE_SECONDS, MAX_ATTEMPTS
from app.models import (
    CLAIMABLE_STATUSES,
    ReminderTask,
    TaskAttempt,
    TaskStatus,
    new_lease_token,
)
from app.notifier import NotifierResponse, is_retriable
from app.retry_policy import next_delay_seconds

logger = logging.getLogger(__name__)


class IdempotencyConflict(Exception):
    """Same idempotency key, different loan/channel/message.

    Almost always a caller bug - a key got reused for a different reminder.
    """


@dataclass(frozen=True)
class ClaimedTask:
    id: int
    loan_id: str
    channel: str
    message: str
    attempt_number: int
    lease_token: str


class ReminderTaskService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create_task(
        self,
        *,
        idempotency_key: str,
        loan_id: str,
        channel: str,
        message: str,
        max_attempts: Optional[int] = None,
    ) -> Tuple[ReminderTask, bool]:
        existing = self._find_by_key(idempotency_key)
        if existing is not None:
            self._assert_same_request(existing, loan_id, channel, message)
            return existing, False

        task = ReminderTask(
            idempotency_key=idempotency_key,
            loan_id=loan_id,
            channel=channel,
            message=message,
            status=TaskStatus.PENDING,
            attempts_made=0,
            max_attempts=max_attempts or MAX_ATTEMPTS,
        )
        self.db.add(task)

        try:
            self.db.commit()
        except IntegrityError:
            # Same race as in the payment-retry-engine project: two concurrent
            # inserts with the same key both pass the lookup above before
            # either commits. The unique index is what actually catches it.
            self.db.rollback()
            duplicate = self._find_by_key(idempotency_key)
            if duplicate is None:
                raise
            self._assert_same_request(duplicate, loan_id, channel, message)
            return duplicate, False

        self.db.refresh(task)
        return task, True

    def _find_by_key(self, idempotency_key: str) -> Optional[ReminderTask]:
        return (
            self.db.query(ReminderTask)
            .filter(ReminderTask.idempotency_key == idempotency_key)
            .one_or_none()
        )

    @staticmethod
    def _assert_same_request(task: ReminderTask, loan_id: str, channel: str, message: str) -> None:
        if task.loan_id != loan_id or task.channel != channel or task.message != message:
            raise IdempotencyConflict(
                f"Key {task.idempotency_key} was already used for a different reminder"
            )

    # ------------------------------------------------------------------
    # Claiming work
    # ------------------------------------------------------------------

    @staticmethod
    def claimable_condition(now: datetime):
        """SQLAlchemy filter for 'a worker may pick this up right now'.

        Shared between the claim query below and the /stats endpoint in
        app/main.py, so the two cannot silently drift apart.
        """
        return or_(
            (
                ReminderTask.status.in_(tuple(CLAIMABLE_STATUSES))
                & or_(
                    ReminderTask.next_attempt_at.is_(None),
                    ReminderTask.next_attempt_at <= now,
                )
            ),
            (
                (ReminderTask.status == TaskStatus.LEASED)
                & (ReminderTask.lease_expires_at <= now)
            ),
        )

    def claim_due_tasks(self, worker_id: str, limit: int) -> List[ClaimedTask]:
        """Lease up to `limit` due tasks to this worker.

        Does two things in one short transaction:

        1. Picks up fresh work - PENDING or RETRY_SCHEDULED tasks whose time
           has come.
        2. Reclaims abandoned work - tasks still marked LEASED by some worker
           whose lease expired, meaning it crashed or got killed mid-send
           without ever reporting back.

        Both are locked with SKIP LOCKED so a second pod polling at the same
        moment moves straight past whatever this one is already touching,
        instead of blocking behind it.

        The transaction is deliberately short: it claims and commits, then
        returns. It does not hold the lock while the caller goes off and
        actually sends the notification - that happens after this method has
        already returned and the lock has already been released. See the
        TaskStatus docstring in app/models.py for why that split matters.
        """
        now = utcnow()

        candidates = (
            self.db.query(
                ReminderTask.id,
                ReminderTask.status,
                ReminderTask.owner_id,
                ReminderTask.attempts_made,
                ReminderTask.loan_id,
                ReminderTask.channel,
                ReminderTask.message,
            )
            .filter(self.claimable_condition(now))
            .order_by(ReminderTask.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
            .all()
        )

        if not candidates:
            self.db.commit()  # nothing locked, but keep transactions short and tidy
            return []

        token = new_lease_token()
        expiry = now + timedelta(seconds=LEASE_SECONDS)
        claimed: List[ClaimedTask] = []

        for row in candidates:
            if row.status == TaskStatus.LEASED:
                # Abandoned by whoever held it before us. Record what happened
                # to that attempt before we overwrite the row - otherwise the
                # fact that a worker went silent leaves no trace at all.
                self.db.add(
                    TaskAttempt(
                        task_id=row.id,
                        attempt_number=row.attempts_made,
                        worker_id=row.owner_id or "unknown",
                        succeeded=0,
                        error_code="lease_expired",
                    )
                )
                logger.warning(
                    "Reclaiming task %s from %s, lease expired without a result",
                    row.id,
                    row.owner_id,
                )

            claimed.append(
                ClaimedTask(
                    id=row.id,
                    loan_id=row.loan_id,
                    channel=row.channel,
                    message=row.message,
                    attempt_number=row.attempts_made + 1,
                    lease_token=token,
                )
            )

        ids = [row.id for row in candidates]
        (
            self.db.query(ReminderTask)
            .filter(ReminderTask.id.in_(ids))
            .update(
                {
                    ReminderTask.status: TaskStatus.LEASED,
                    ReminderTask.owner_id: worker_id,
                    ReminderTask.lease_token: token,
                    ReminderTask.lease_expires_at: expiry,
                    # A relative update, safe to apply to the whole batch at
                    # once regardless of each row's prior value.
                    ReminderTask.attempts_made: ReminderTask.attempts_made + 1,
                },
                # "fetch" rather than False: it identifies the affected rows
                # with a SELECT and expires any of them already sitting in this
                # session's identity map. Skipping that is exactly what caused
                # the first version of this code to fail its own tests -
                # session.query(id=...) right after this commit was returning
                # a stale, pre-update copy of the row from the identity map
                # instead of what the UPDATE had just written, and
                # complete_task fenced out every result as "stale" on the
                # very first call.
                synchronize_session="fetch",
            )
        )
        self.db.commit()
        return claimed

    # ------------------------------------------------------------------
    # Reporting a result
    # ------------------------------------------------------------------

    def complete_task(
        self, task_id: int, lease_token: str, worker_id: str, response: NotifierResponse
    ) -> Optional[ReminderTask]:
        """Record the outcome of a send. Fenced by lease_token.

        If this task's lease was reclaimed by someone else while we were busy
        sending - we were just slow, not dead, and our lease expired anyway -
        lease_token on the row no longer matches what we were handed. We
        report None and drop the result on the floor rather than overwrite
        whatever the new owner has already done or is doing. That is the
        fencing token doing its job: without it, a late-arriving result from a
        zombie worker could stomp on a completion made by whoever took over.
        """
        task = (
            self.db.query(ReminderTask)
            .filter(ReminderTask.id == task_id)
            .with_for_update()
            .one_or_none()
        )

        if task is None or task.status != TaskStatus.LEASED or task.lease_token != lease_token:
            self.db.commit()
            logger.warning(
                "Dropping stale result for task %s from %s - lease no longer belongs to it",
                task_id,
                worker_id,
            )
            return None

        self.db.add(
            TaskAttempt(
                task_id=task.id,
                attempt_number=task.attempts_made,
                worker_id=worker_id,
                succeeded=1 if response.succeeded else 0,
                error_code=response.error_code,
            )
        )

        self._apply_outcome(task, response)

        self.db.commit()
        self.db.refresh(task)
        logger.info(
            "Task %s attempt %s/%s -> %s",
            task.id,
            task.attempts_made,
            task.max_attempts,
            task.status.value,
        )
        return task

    @staticmethod
    def _apply_outcome(task: ReminderTask, response: NotifierResponse) -> None:
        task.owner_id = None
        task.lease_token = None
        task.lease_expires_at = None

        if response.succeeded:
            task.status = TaskStatus.DONE
            task.next_attempt_at = None
            task.last_error = None
            return

        task.last_error = response.error_code

        if not is_retriable(response.error_code):
            task.status = TaskStatus.DEAD
            task.next_attempt_at = None
            return

        if task.attempts_made >= task.max_attempts:
            task.status = TaskStatus.DEAD
            task.next_attempt_at = None
            return

        task.status = TaskStatus.RETRY_SCHEDULED
        task.next_attempt_at = utcnow() + timedelta(
            seconds=next_delay_seconds(task.attempts_made)
        )
