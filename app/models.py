"""SQLAlchemy ORM models. Portable across Postgres (prod) and SQLite (tests)."""
from __future__ import annotations

import json
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _j(obj) -> str:
    return json.dumps(obj, default=str, sort_keys=True)


def _uj(s: str | None):
    if not s:
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s


class Rider(Base):
    __tablename__ = "riders"
    rider_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(64))
    joined_on: Mapped[date] = mapped_column(Date)


class Trip(Base):
    __tablename__ = "trips"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)  # row stability
    trip_id: Mapped[str] = mapped_column(String(16), index=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)  # as filed (may contain typos)
    norm_rider_id: Mapped[str] = mapped_column(String(16), index=True)  # normalized (policy.norm_rider_id)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ist_day: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(24))
    distance_km: Mapped[str] = mapped_column(String(32))  # raw string; interpret via policy.clean_distance
    surge_multiplier: Mapped[str] = mapped_column(String(16))
    is_duplicate: Mapped[int] = mapped_column(Integer, default=0)  # repeated trip_id row (R009 trap)


class PayoutLine(Base):
    __tablename__ = "payout_lines"
    line_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    payout_date: Mapped[date] = mapped_column(Date, index=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)  # as filed
    norm_rider_id: Mapped[str] = mapped_column(String(16), index=True)
    line_type: Mapped[str] = mapped_column(String(24))
    trip_id: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    amount: Mapped[int] = mapped_column(Integer)  # signed: penalties are negative


class Message(Base):
    __tablename__ = "messages"
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    text: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(12), default="processing")  # processing|done
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rider_id: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    # structured conversation state (pushback tracking etc, JSON blob)
    state: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class TraceStep(Base):
    __tablename__ = "trace_steps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    at: Mapped[str] = mapped_column(DateTime, default=func.now(), index=True)
    type: Mapped[str] = mapped_column(String(16))  # message_in|tool_call|decision|reply|error
    name: Mapped[str] = mapped_column(String(64))
    input: Mapped[str | None] = mapped_column(Text, nullable=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)


class PendingOp(Base):
    __tablename__ = "pending_ops"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    type: Mapped[str] = mapped_column(String(16))  # approval|escalation
    amount: Mapped[int | None] = mapped_column(Integer, nullable=True)  # null for escalations
    reason: Mapped[str] = mapped_column(Text)
    covered_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of discrepancy ids
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|approved|rejected
    idem_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payswift_payout_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PayoutLedger(Base):
    """Every compensation intent is recorded BEFORE money moves (holds seat), then
    flipped to paid/failed after the PaySwift response. Unique consistency: the
    (rider, day) once-auto-per-day rule + covered_ids both checked against rows here."""
    __tablename__ = "payout_ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rider_id: Mapped[str] = mapped_column(String(16), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)  # IST day of the message (anchor)
    amount: Mapped[int] = mapped_column(Integer)
    covered_ids: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    decided_by: Mapped[str] = mapped_column(String(24), default="auto")  # auto|ops|pending_ops
    status: Mapped[str] = mapped_column(String(12), default="held", index=True)  # held|paid|failed|queued|rejected
    idem_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payswift_payout_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_op_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())
    __table_args__ = (UniqueConstraint("idem_key", name="uq_payout_ledger_idem_key"),)


class LoaderState(Base):
    __tablename__ = "loader_state"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64))
    rows: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
