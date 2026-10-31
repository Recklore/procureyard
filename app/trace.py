"""Trace step recording."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from .models import TraceStep
from .tz import utc_now


def record(s: Session, rider_id: str, conversation_id: int, type: str, name: str,
           inp, output, at: str | None = None) -> None:
    step = TraceStep(
        rider_id=rider_id,
        conversation_id=conversation_id,
        at=datetime.fromisoformat(at) if at else utc_now(),
        type=type,
        name=name,
        input=json.dumps(inp, default=str, ensure_ascii=False) if inp is not None else None,
        output=json.dumps(output, default=str, ensure_ascii=False) if isinstance(output, (dict, list)) else str(output) if output is not None else None,
    )
    s.add(step)

