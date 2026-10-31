"""Engine + session factory, startup data loading (bulk, idempotent by file checksum)."""
from __future__ import annotations

import csv
import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, insert, text
from sqlalchemy.orm import Session, sessionmaker

from . import policy
from .models import Base, Conversation, LoaderState, PayoutLine, Rider, TraceStep, Trip
from .tz import trip_day, utc_now

log = logging.getLogger("qd.db")

_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = os.environ.get("DATABASE_URL", "sqlite:///./qd_dev.db")
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        _engine = create_engine(url, pool_pre_ping=True, future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def session() -> Session:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _unescape_ts(v: str) -> datetime:
    dt = v.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(dt)


def load_csvs(data_dir: Path) -> dict:
    """Load riders/trips/payout_lines. Idempotent: keyed on file checksums; a restart
    with unchanged files is a no-op (fast boot). Rows are normalized at load time
    (rider-id normalization, IST day, duplicate-row flagging)."""
    get_engine()
    Base.metadata.create_all(_engine)
    loaded = {}
    with session() as s:
        specs = [
            ("riders", Rider, data_dir / "riders.csv"),
            ("trips", Trip, data_dir / "trips.csv"),
            ("payout_lines", PayoutLine, data_dir / "payout_lines.csv"),
        ]
        seen_trip_ids: set[str] = set()
        for name, model, path in specs:
            if not path.exists():
                raise FileNotFoundError(path)
            ck = _checksum(path)
            prior = s.get(LoaderState, name)
            if prior is not None and prior.checksum == ck:
                loaded[name] = prior.rows
                continue
            if prior is not None:  # changed files: wipe & reload
                s.query(model).delete()
            rows = []
            if name == "riders":
                for r in csv.DictReader(open(path, encoding="utf-8")):
                    rows.append({"rider_id": r["rider_id"], "name": r["name"], "city": r["city"],
                                 "joined_on": datetime.strptime(r["joined_on"], "%Y-%m-%d").date()})
            elif name == "trips":
                for r in csv.DictReader(open(path, encoding="utf-8")):
                    rows.append({
                        "trip_id": r["trip_id"], "rider_id": r["rider_id"],
                        "norm_rider_id": policy.norm_rider_id(r["rider_id"]),
                        "started_at": _unescape_ts(r["started_at"]),
                        "ist_day": trip_day(r["started_at"]),
                        "status": r["status"], "distance_km": r["distance_km"],
                        "surge_multiplier": r["surge_multiplier"],
                        "is_duplicate": 1 if r["trip_id"] in seen_trip_ids else 0,
                    })
                    seen_trip_ids.add(r["trip_id"])
            else:
                for r in csv.DictReader(open(path, encoding="utf-8")):
                    rows.append({
                        "line_id": r["line_id"], "payout_date": datetime.strptime(r["payout_date"], "%Y-%m-%d").date(),
                        "rider_id": r["rider_id"], "norm_rider_id": policy.norm_rider_id(r["rider_id"]),
                        "line_type": r["line_type"], "trip_id": r["trip_id"] or None,
                        "amount": int(r["amount"]),
                    })
            if rows:
                s.execute(insert(model), rows)
            s.merge(LoaderState(name=name, checksum=ck, rows=len(rows), loaded_at=utc_now()))
            loaded[name] = len(rows)
        s.commit()
    log.info("data loaded: %s", loaded)
    return loaded


def data_ready() -> bool:
    """Health only when the riders table actually has rows."""
    try:
        with session() as s:
            n = s.execute(text("select count(*) from riders")).scalar()
            return bool(n)
    except Exception:
        return False
