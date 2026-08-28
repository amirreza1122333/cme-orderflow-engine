"""Economic-calendar blackout filter.

High-impact releases (NFP, CPI, FOMC) move gold and FX several stops in a
second and widen spreads while they do it. A short-horizon strategy has no edge
in that; the only sane play is to stand aside. This module keeps a cached copy
of the week's calendar and answers one question: "is `SYMBOL` inside a blackout
window right now?"

The fetch is plain blocking HTTP, so it is always run in a thread - the Twisted
the tick handler must never block waiting on a web request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("news")

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Which currencies' releases matter for each instrument.
SYMBOL_CURRENCIES = {
    "XAUUSD": {"USD"},
    "XAGUSD": {"USD"},
    "EURUSD": {"EUR", "USD"},
    "GBPUSD": {"GBP", "USD"},
    "USDJPY": {"USD", "JPY"},
    "US30": {"USD"},
    "US500": {"USD"},
    "NAS100": {"USD"},
    "USTEC": {"USD"},
}


@dataclass
class NewsEvent:
    when: datetime
    title: str
    currency: str
    impact: str
    forecast: str = ""
    previous: str = ""

    def describe(self) -> str:
        return f"{self.currency} {self.title} at {self.when:%H:%M} UTC"


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def fetch_calendar(timeout: int = 15) -> list[NewsEvent]:
    """Blocking. Call via `engine._in_thread`, never from a tick."""
    response = requests.get(
        CALENDAR_URL, timeout=timeout, headers={"User-Agent": "dtc-futures-bot/1.0"}
    )
    response.raise_for_status()
    events: list[NewsEvent] = []
    for row in response.json():
        when = _parse_time(row.get("date", ""))
        if when is None:
            continue
        events.append(
            NewsEvent(
                when=when,
                title=str(row.get("title", "")).strip(),
                currency=str(row.get("country", "")).strip().upper(),
                impact=str(row.get("impact", "")).strip().lower(),
                forecast=str(row.get("forecast", "") or ""),
                previous=str(row.get("previous", "") or ""),
            )
        )
    return events


class NewsFilter:
    def __init__(self, before_minutes: int = 30, after_minutes: int = 15) -> None:
        self.before = timedelta(minutes=before_minutes)
        self.after = timedelta(minutes=after_minutes)
        self.events: list[NewsEvent] = []
        self.last_update: datetime | None = None
        self.last_error: str = ""

    def update(self, events: list[NewsEvent]) -> None:
        self.events = [event for event in events if event.impact == "high"]
        self.last_update = datetime.now(timezone.utc)
        self.last_error = ""
        log.info("Calendar loaded: %d high-impact events this week", len(self.events))

    def update_failed(self, error: str) -> None:
        self.last_error = error
        log.warning("Calendar refresh failed: %s", error)

    @property
    def is_stale(self) -> bool:
        if self.last_update is None:
            return True
        return datetime.now(timezone.utc) - self.last_update > timedelta(hours=6)

    def currencies_for(self, symbol: str) -> set[str]:
        return SYMBOL_CURRENCIES.get(symbol.upper(), {"USD"})

    def blackout(
        self, symbol: str, now: datetime | None = None
    ) -> tuple[bool, str]:
        """(blocked, reason). Fails *open* when the calendar was never loaded.

        A missing calendar is logged loudly instead of halting trading, but if
        you want the stricter behaviour, flip this to return True when
        `self.last_update is None`.
        """
        now = now or datetime.now(timezone.utc)
        if self.last_update is None:
            return False, "calendar unavailable"

        relevant = self.currencies_for(symbol)
        for event in self.events:
            if event.currency not in relevant:
                continue
            if event.when - self.before <= now <= event.when + self.after:
                minutes = (event.when - now).total_seconds() / 60
                when = "in %.0f min" % minutes if minutes >= 0 else "%.0f min ago" % -minutes
                return True, f"high-impact {event.currency} {event.title} {when}"
        return False, "clear"

    def upcoming(self, symbol: str, hours: int = 4,
                 now: datetime | None = None) -> list[NewsEvent]:
        now = now or datetime.now(timezone.utc)
        horizon = now + timedelta(hours=hours)
        relevant = self.currencies_for(symbol)
        return [
            event
            for event in self.events
            if event.currency in relevant and now <= event.when <= horizon
        ]
