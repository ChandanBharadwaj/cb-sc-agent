"""Shared API helpers: JSON response that understands DB types, and DB access for request handlers."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute


def _default(o: Any) -> Any:
    if isinstance(o, datetime | date):
        return o.isoformat()
    if isinstance(o, timedelta):
        return o.total_seconds()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, set | frozenset):
        return sorted(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


class DbJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(content, default=_default, ensure_ascii=False).encode("utf-8")


def ok(content: Any, status_code: int = 200) -> DbJSONResponse:
    return DbJSONResponse(content, status_code=status_code)


class DbRoute(APIRoute):
    """Handlers return plain DB rows. Skip response-model inference from the ``-> Any`` annotation: pydantic would
    serialise ``Decimal`` as a string ("0.85"); without a response model FastAPI's encoder emits real numbers."""

    def __init__(self, path: str, endpoint: Any, **kwargs: Any) -> None:
        kwargs["response_model"] = None
        super().__init__(path, endpoint, **kwargs)
