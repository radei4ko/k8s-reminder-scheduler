from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import TaskStatus

VALID_CHANNELS = {"email", "sms"}


class ReminderCreate(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64)
    loan_id: str = Field(min_length=1, max_length=64)
    channel: str = Field(min_length=1, max_length=16)
    message: str = Field(min_length=1, max_length=500)
    max_attempts: Optional[int] = Field(default=None, ge=1, le=10)

    @field_validator("channel")
    @classmethod
    def known_channel(cls, value: str) -> str:
        value = value.lower()
        if value not in VALID_CHANNELS:
            raise ValueError(f"channel must be one of {sorted(VALID_CHANNELS)}")
        return value

    @field_validator("idempotency_key", "loan_id", "message")
    @classmethod
    def strip_and_check(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be empty")
        return value


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    attempt_number: int
    worker_id: str
    succeeded: bool
    error_code: Optional[str]
    created_at: datetime

    @field_validator("succeeded", mode="before")
    @classmethod
    def coerce_bool(cls, value):
        return bool(value)


class ReminderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    idempotency_key: str
    loan_id: str
    channel: str
    status: TaskStatus
    owner_id: Optional[str]
    attempts_made: int
    max_attempts: int
    next_attempt_at: Optional[datetime]
    last_error: Optional[str]
    created_at: datetime
    updated_at: datetime


class ReminderDetailOut(ReminderOut):
    message: str
    attempts: List[AttemptOut] = []


class StatsOut(BaseModel):
    total: int
    by_status: dict
    claimable_now: int
