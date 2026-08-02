"""Worker process: claims due reminder tasks and sends them.

This is what runs in every pod of the Deployment in k8s/worker-deployment.yaml.
Scale the replica count and more of these run side by side, all claiming from
the same table safely - that safety is the entire point of this project.
"""

import logging
import signal
import time

from app.config import (
    CLAIM_BATCH_SIZE,
    LEASE_SECONDS,
    POLL_INTERVAL_SECONDS,
    WORKER_ID,
)
from app.database import SessionLocal
from app.notifier import FakeNotifier
from app.service import ReminderTaskService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

_shutdown_requested = False


def _handle_shutdown(signum, frame):
    # Kubernetes sends SIGTERM before killing a pod (on a rollout, a scale-down,
    # a node drain) and waits terminationGracePeriodSeconds before following up
    # with SIGKILL. Catching it here lets an in-flight send finish and report
    # its result normally instead of leaking a lease that some other pod then
    # has to sit around waiting to expire.
    global _shutdown_requested
    logger.info("Received signal %s, finishing current batch then exiting", signum)
    _shutdown_requested = True


def run_forever(notifier=None) -> None:
    notifier = notifier or FakeNotifier()

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    logger.info(
        "Worker %s starting: lease=%ss poll=%ss batch=%s",
        WORKER_ID,
        LEASE_SECONDS,
        POLL_INTERVAL_SECONDS,
        CLAIM_BATCH_SIZE,
    )

    while not _shutdown_requested:
        processed = run_one_tick(notifier, worker_id=WORKER_ID)
        if processed == 0 and not _shutdown_requested:
            time.sleep(POLL_INTERVAL_SECONDS)

    logger.info("Worker %s shut down cleanly", WORKER_ID)


def run_one_tick(notifier, worker_id: str = WORKER_ID) -> int:
    """Claim whatever is due, send it, report the result. Returns count processed.

    worker_id defaults to this process's identity but can be overridden -
    tests use that to simulate several distinct pods from one process.
    """
    db = SessionLocal()
    try:
        service = ReminderTaskService(db)
        claimed = service.claim_due_tasks(worker_id, CLAIM_BATCH_SIZE)

        for task in claimed:
            response = notifier.send(
                # Attempt number in the key for the same reason as the payment
                # engine: a provider that caches by idempotency key must not
                # be handed the same key twice for what are, from its point of
                # view, two different attempts.
                idempotency_key=f"{task.id}:{task.attempt_number}",
                channel=task.channel,
                message=task.message,
            )
            service.complete_task(task.id, task.lease_token, worker_id, response)

        return len(claimed)
    except Exception:
        logger.exception("Worker tick failed")
        db.rollback()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    run_forever()
