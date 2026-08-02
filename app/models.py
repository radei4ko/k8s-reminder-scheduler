import enum
import uuid

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.clock import utcnow
from app.database import Base


class TaskStatus(str, enum.Enum):
    """Reminder task lifecycle.

        PENDING ──claimed by a worker──> LEASED ──sent ok────────> DONE
           ^                               │  │
           │                               │  └──lease expired,
           │                               │     nobody completed it──> back
           │                               │                            to PENDING
           │                               │
           │                               └──send failed, retriable──> RETRY_SCHEDULED
           │                                                                  │
           └──────────────────backoff elapsed────────────────────────────────┘
                                                                                │
                                                            attempts exhausted, or a
                                                            non-retriable failure
                                                                                │
                                                                                v
                                                                              DEAD

    LEASED is not the same thing as "locked". A database row lock is held only
    for the instant it takes to run the claim UPDATE - not for the duration of
    the actual notification send, which is an external HTTP-ish call with its
    own unpredictable latency. Holding a MySQL row lock across that call would
    block replication and any other worker's claim query for as long as the
    notifier is slow, which in production is exactly the kind of thing that
    turns a flaky third-party API into a database outage.

    So the lock is released the moment the claim commits, and LEASED plus
    lease_expires_at is what stands in for "someone is working on this" while
    no lock is held at all.
    """

    PENDING = "pending"
    LEASED = "leased"
    RETRY_SCHEDULED = "retry_scheduled"
    DONE = "done"
    DEAD = "dead"


# Statuses a worker may pick up. RETRY_SCHEDULED behaves like PENDING once
# next_attempt_at has passed - see ReminderTaskService._is_claimable.
CLAIMABLE_STATUSES = frozenset({TaskStatus.PENDING, TaskStatus.RETRY_SCHEDULED})


class ReminderTask(Base):
    """One delinquency reminder to send for a loan."""

    __tablename__ = "reminder_tasks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Caller-supplied. Reusing a key returns the existing task instead of
    # creating a duplicate reminder for the same loan and due date.
    idempotency_key = Column(String(64), nullable=False, unique=True)

    loan_id = Column(String(64), nullable=False, index=True)
    channel = Column(String(16), nullable=False)
    message = Column(String(500), nullable=False)

    status = Column(
        Enum(TaskStatus, native_enum=False, length=32),
        nullable=False,
        default=TaskStatus.PENDING,
    )

    # Who currently holds the lease, and until when. Both NULL when the task
    # is not currently leased.
    owner_id = Column(String(128), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)

    # Bumped on every claim. This is the fencing token: a completion or
    # failure report is only honoured if it carries the lease_token that is
    # still current, so a worker whose lease already expired and was handed
    # to someone else cannot clobber that someone else's result by finishing
    # late.
    lease_token = Column(String(36), nullable=True)

    attempts_made = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False)
    next_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(String(64), nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    attempts = relationship(
        "TaskAttempt",
        back_populates="task",
        order_by="TaskAttempt.attempt_number",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        # The claim query is
        #   WHERE status IN (...) AND (next_attempt_at IS NULL OR next_attempt_at <= now)
        # run every POLL_INTERVAL_SECONDS by every pod. Without this index it
        # is a full table scan on every poll, from every replica.
        Index("ix_reminder_tasks_status_next_attempt", "status", "next_attempt_at"),
        # The sweep for abandoned leases:
        #   WHERE status = 'leased' AND lease_expires_at <= now
        Index("ix_reminder_tasks_status_lease_expiry", "status", "lease_expires_at"),
        # The Pydantic schema in app/schemas.py rejects an unknown channel on
        # the way in through the API, but that only guards the API. Anything
        # writing to this table another way - a migration, a one-off script,
        # a future second API - would not go through it. The constraint is the
        # backstop.
        CheckConstraint("channel IN ('email', 'sms')", name="ck_reminder_tasks_channel"),
    )

    def __repr__(self) -> str:
        return (
            f"<ReminderTask id={self.id} loan={self.loan_id} "
            f"status={self.status.value if self.status else None} "
            f"owner={self.owner_id}>"
        )


class TaskAttempt(Base):
    """History of send attempts, one row per attempt regardless of outcome."""

    __tablename__ = "task_attempts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(
        BigInteger,
        ForeignKey("reminder_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    attempt_number = Column(Integer, nullable=False)
    worker_id = Column(String(128), nullable=False)
    succeeded = Column(Integer, nullable=False)  # 0/1, kept simple across drivers
    error_code = Column(String(64), nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)

    task = relationship("ReminderTask", back_populates="attempts")

    __table_args__ = (
        # Last line of defence: even if a stale lease and a fresh one somehow
        # both tried to record the same attempt number, the database refuses
        # the second insert.
        UniqueConstraint("task_id", "attempt_number", name="uq_task_attempt_number"),
    )


def new_lease_token() -> str:
    return uuid.uuid4().hex
