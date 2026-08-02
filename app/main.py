import logging
import time
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.clock import utcnow
from app.database import Base, engine, get_db
from app.models import ReminderTask, TaskStatus
from app.schemas import ReminderCreate, ReminderDetailOut, ReminderOut, StatsOut
from app.service import IdempotencyConflict, ReminderTaskService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)


def wait_for_db(attempts: int = 15, delay_seconds: int = 2) -> None:
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect():
                return
        except OperationalError:
            logger.warning("Database not ready, attempt %s/%s", attempt, attempts)
            time.sleep(delay_seconds)
    raise RuntimeError("Gave up waiting for the database")


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    # create_all only, no migrations - fine for a demo, see README for why
    # this would be Alembic in a real service.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Reminder Scheduler API",
    description=(
        "Read/write API for delinquency reminder tasks. The actual sending "
        "happens in separate worker pods (see app/worker.py) - this process "
        "only creates tasks and reports on them."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


def get_service(db: Session = Depends(get_db)) -> ReminderTaskService:
    return ReminderTaskService(db)


@app.get("/healthz", tags=["service"])
def healthz(db: Session = Depends(get_db)):
    db.execute(func.now().select())
    return {"status": "ok"}


@app.post("/reminders", response_model=ReminderOut, status_code=201, tags=["reminders"])
def create_reminder(
    payload: ReminderCreate,
    response: Response,
    service: ReminderTaskService = Depends(get_service),
):
    """Queue a reminder. Idempotent: 201 on create, 200 if the key was seen before."""
    try:
        task, created = service.create_task(
            idempotency_key=payload.idempotency_key,
            loan_id=payload.loan_id,
            channel=payload.channel,
            message=payload.message,
            max_attempts=payload.max_attempts,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    if not created:
        response.status_code = 200
    return task


@app.get("/reminders", response_model=List[ReminderOut], tags=["reminders"])
def list_reminders(
    loan_id: Optional[str] = None,
    status: Optional[TaskStatus] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    query = db.query(ReminderTask)
    if loan_id:
        query = query.filter(ReminderTask.loan_id == loan_id)
    if status:
        query = query.filter(ReminderTask.status == status)
    return query.order_by(ReminderTask.id.desc()).limit(limit).offset(offset).all()


@app.get("/reminders/{task_id}", response_model=ReminderDetailOut, tags=["reminders"])
def get_reminder(task_id: int, db: Session = Depends(get_db)):
    task = (
        db.query(ReminderTask)
        .options(selectinload(ReminderTask.attempts))
        .filter(ReminderTask.id == task_id)
        .one_or_none()
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Reminder not found")
    return task


@app.get("/stats", response_model=StatsOut, tags=["service"])
def stats(db: Session = Depends(get_db)):
    rows = db.query(ReminderTask.status, func.count(ReminderTask.id)).group_by(ReminderTask.status).all()
    by_status = {status.value: count for status, count in rows}

    now = utcnow()
    claimable_now = (
        db.query(func.count(ReminderTask.id))
        .filter(ReminderTaskService.claimable_condition(now))
        .scalar()
    )

    return StatsOut(total=sum(by_status.values()), by_status=by_status, claimable_now=claimable_now or 0)
