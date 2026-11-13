"""SQLAlchemy ORM models for the Hugging Face module's hand-over database.

These tables live in their own SQLite file with their own migration history
(``core/handover_alembic``), never in the shared spool database: released
emitters refuse a spool database at a revision they don't know, and Microcosm
builds pinned to a released emitter share that database with newer ones.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, Float, Integer, LargeBinary, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from microcosm_emitter.core.models import JsonObjectText


class HandoverModel(DeclarativeBase):
    """Declarative base for the hand-over database."""


class StagedRunFile(HandoverModel):
    """The latest handed-over bytes for one repository path of a staging run.

    A later hand-over of the same path replaces pending bytes, so each commit
    carries only the newest state. Committed rows keep the committed digest.
    """

    __tablename__ = "staged_run_files"
    repository: Mapped[str] = mapped_column(Text, primary_key=True)
    path_in_repo: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    committed_sha256: Mapped[str | None] = mapped_column(Text)
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False)
    due_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class StagedRunDelivery(HandoverModel):
    """Delivery counts for one staging run, and its pending runs-index entry."""

    __tablename__ = "staged_run_deliveries"
    repository: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    files_attempted: Mapped[int] = mapped_column(Integer, nullable=False)
    files_committed: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    commits: Mapped[int] = mapped_column(Integer, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_commit: Mapped[str | None] = mapped_column(Text)
    # JSON text in the build's key order: the merged runs.json keeps it.
    run_index_entry: Mapped[str | None] = mapped_column(Text)
    run_index_pending: Mapped[bool] = mapped_column(Boolean, nullable=False)
    run_index_due_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class RepositoryCommitSchedule(HandoverModel):
    """When a repository may next receive a commit from this machine."""

    __tablename__ = "repository_commit_schedules"
    repository: Mapped[str] = mapped_column(Text, primary_key=True)
    last_commit_at: Mapped[float] = mapped_column(Float, nullable=False)
    retry_after: Mapped[float] = mapped_column(Float, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_until: Mapped[float] = mapped_column(Float, nullable=False)


class HandoverJob(HandoverModel):
    """One durable hand-over for one destination, retried until it settles.

    The same hand-over can have a row per destination (today only
    ``huggingface``), so a later destination needs no new table.
    """

    __tablename__ = "handover_jobs"
    handover_id: Mapped[str] = mapped_column(Text, primary_key=True)
    destination: Mapped[str] = mapped_column(Text, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JsonObjectText, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[float] = mapped_column(Float, nullable=False)
    lease_until: Mapped[float] = mapped_column(Float, nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JsonObjectText, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class HandoverFile(HandoverModel):
    """The bytes of one small hand-over file, copied when it was handed over.

    The build may rewrite such a file at once (Microcosm rewrites its build
    manifest, checksum listing and build record right after staging), so the
    delivery uploads these bytes instead of reading the file again. A
    described promotion's or withdrawal's row is the ``releases.json`` its
    change wrote, kept when the change landed for its collector row. The rows
    are deleted once every destination of the hand-over has settled.
    """

    __tablename__ = "handover_files"
    handover_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
