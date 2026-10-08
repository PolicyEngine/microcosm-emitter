"""SQLAlchemy ORM models for the local telemetry spool."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import (
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from microcosm_provider_telemetry.service.constants import (
    UPLOAD_STATE_PENDING,
)


class JsonObjectText(TypeDecorator[dict[str, Any]]):
    """Store JSON objects in the spool's existing text-column format."""

    impl = Text
    cache_ok = True

    def process_bind_param(
        self,
        value: dict[str, Any] | None,
        dialect,
    ) -> str | None:
        if value is None:
            return None
        return _serialize_json_object(value)

    def process_result_value(
        self,
        value: str | None,
        dialect,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise ValueError("telemetry spool JSON must contain an object")
        return decoded


def serialized_json_length(value: dict[str, Any]) -> int:
    """Return the stored character count for one mapped JSON object."""

    return len(_serialize_json_object(value))


def _serialize_json_object(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


class SpoolModel(DeclarativeBase):
    """Declarative base for the local telemetry spool."""


class TelemetryRunRecord(SpoolModel):
    """One telemetry producer registered for one build run."""

    __tablename__ = "telemetry_runs"

    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    producer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    registration: Mapped[dict[str, Any]] = mapped_column(
        "registration_json",
        JsonObjectText,
        nullable=False,
    )
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    upload_state: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=UPLOAD_STATE_PENDING,
    )
    local_only_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    events: Mapped[list[TelemetryEventRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
    )


class TelemetryEventRecord(SpoolModel):
    """One ordered telemetry event awaiting collector acknowledgement."""

    __tablename__ = "telemetry_events"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "producer_id",
            "sequence",
            name="uq_telemetry_events_run_producer_sequence",
        ),
        ForeignKeyConstraint(
            ["run_id", "producer_id"],
            ["telemetry_runs.run_id", "telemetry_runs.producer_id"],
            name="fk_telemetry_events_run",
        ),
        Index(
            "telemetry_events_run_sequence",
            "run_id",
            "producer_id",
            "sequence",
        ),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    producer_id: Mapped[str] = mapped_column(Text, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        "payload_json",
        JsonObjectText,
        nullable=False,
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    run: Mapped[TelemetryRunRecord] = relationship(back_populates="events")
