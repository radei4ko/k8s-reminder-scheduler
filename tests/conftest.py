import os

os.environ["DATABASE_URL"] = os.getenv(
    "TEST_DATABASE_URL",
    "mysql+pymysql://reminder_user:change_me@127.0.0.1:3308/reminder_scheduler_test",
)
os.environ.setdefault("MAX_ATTEMPTS", "5")
os.environ.setdefault("RETRY_BASE_DELAY_SECONDS", "10")
os.environ.setdefault("RETRY_MAX_DELAY_SECONDS", "300")
os.environ.setdefault("LEASE_SECONDS", "30")

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import ReminderTask, TaskAttempt  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def create_schema():
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    session.query(TaskAttempt).delete()
    session.query(ReminderTask).delete()
    session.commit()
    try:
        yield session
    finally:
        session.close()
