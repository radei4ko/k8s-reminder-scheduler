"""Settings come from the environment. In Kubernetes these are injected by
the Deployment/CronJob manifests from a ConfigMap and Secret; locally they
come from .env via docker-compose.
"""

import os
import uuid

from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "mysql+pymysql://reminder_user:change_me@mysql:3306/reminder_scheduler",
)

# In the Deployment this is the pod name (metadata.name via fieldRef), so a
# lease's owner_id directly identifies which pod is holding a task - useful
# when reading `kubectl logs` next to a row in the database. Outside k8s it
# falls back to a random id per process.
WORKER_ID = os.getenv("WORKER_ID") or f"local-{uuid.uuid4().hex[:8]}"

# Must comfortably exceed how long a real send takes. Too short and a slow but
# healthy worker gets its task stolen out from under it; too long and a
# genuinely crashed pod leaves work stuck for that whole window.
LEASE_SECONDS = _env_int("LEASE_SECONDS", 30)

MAX_ATTEMPTS = _env_int("MAX_ATTEMPTS", 5)
RETRY_BASE_DELAY_SECONDS = _env_int("RETRY_BASE_DELAY_SECONDS", 10)
RETRY_MAX_DELAY_SECONDS = _env_int("RETRY_MAX_DELAY_SECONDS", 300)

POLL_INTERVAL_SECONDS = _env_int("POLL_INTERVAL_SECONDS", 3)
CLAIM_BATCH_SIZE = _env_int("CLAIM_BATCH_SIZE", 10)

NOTIFIER_FAILURE_RATE = _env_float("NOTIFIER_FAILURE_RATE", 0.3)
