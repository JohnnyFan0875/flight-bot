from __future__ import annotations

from datetime import date

from .providers.google import build_query


def flights_url(origin: str, dest: str, depart: date, ret: date | None = None, max_stops: int = 0) -> str:
    return build_query(origin, dest, depart, ret, max_stops).url()
