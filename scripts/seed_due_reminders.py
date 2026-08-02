"""Entry point for the Kubernetes CronJob (k8s/seed-cronjob.yaml).

Stands in for whatever real trigger marks a loan payment as missed - a
scheduled batch job, an event from the payments service, etc. Each run
queues one reminder per loan in a small fixed roster, so re-running the demo
does not need fresh input every time.

Idempotent by construction: the key includes the day, so running this twice
in the same day is a no-op rather than a duplicate reminder, and running it
tomorrow queues a new one.
"""

import logging

from app.clock import utcnow
from app.database import SessionLocal
from app.service import ReminderTaskService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)

DELINQUENT_LOANS = [
    ("loan-101", "email", "Your payment on loan-101 is 3 days overdue."),
    ("loan-102", "sms", "Reminder: loan-102 payment is past due."),
    ("loan-103", "email", "Your payment on loan-103 is 3 days overdue."),
    ("loan-104", "sms", "Reminder: loan-104 payment is past due."),
    ("loan-105", "email", "Your payment on loan-105 is 3 days overdue."),
]


def main() -> None:
    db = SessionLocal()
    try:
        service = ReminderTaskService(db)
        today = utcnow().strftime("%Y-%m-%d")
        created = 0

        for loan_id, channel, message in DELINQUENT_LOANS:
            _, was_created = service.create_task(
                idempotency_key=f"reminder-{loan_id}-{today}",
                loan_id=loan_id,
                channel=channel,
                message=message,
            )
            created += int(was_created)

        logger.info("Seed run for %s: %s new, %s already queued", today, created, len(DELINQUENT_LOANS) - created)
    finally:
        db.close()


if __name__ == "__main__":
    main()
