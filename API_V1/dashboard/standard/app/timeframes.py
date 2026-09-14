"""Timeframes: what "last 7 days" means, in bucket units and window bounds.

Every chart with a time axis is drawn for one timeframe and one page. Page 0
is the window that ends now; page 1 is the same span one step earlier, and so
on back in time. The window is aligned to whole buckets - a "last 7 days" chart
shows seven complete days, not six and a half plus the current afternoon - so
that the first and the last bar mean the same thing as the bars between them.

Nothing here touches the database. The SQL side (app/sqlbuild.py) takes a
Window and turns it into predicates and a date_trunc; the window itself is
computed in Python so the toolbar caption, the CSV header and the query agree
on the same bounds.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class Timeframe:
    key: str
    label: str
    # How long one window is, expressed in the bucket unit below.
    buckets: int
    # A date_trunc unit: hour, day, week, month, quarter. Chosen so a window
    # has between six and thirty bars - fewer reads as a table, more as noise.
    unit: str
    # strftime format for one bucket's axis label.
    label_format: str


TIMEFRAMES: dict[str, Timeframe] = {
    "24h": Timeframe("24h", "Last 24 hours", 24, "hour", "%H:00"),
    "7d":  Timeframe("7d",  "Last 7 days",   7,  "day", "%a %d %b"),
    "30d": Timeframe("30d", "Last 30 days",  30, "day", "%d %b"),
    "90d": Timeframe("90d", "Last 90 days",  13, "week", "%d %b"),
    "6m":  Timeframe("6m",  "Last 6 months", 6,  "month", "%b %Y"),
    "1y":  Timeframe("1y",  "Last year",     12, "month", "%b %Y"),
    "3y":  Timeframe("3y",  "Last 3 years",  12, "quarter", "Q%q %Y"),
    "5y":  Timeframe("5y",  "Last 5 years",  20, "quarter", "Q%q %Y"),
}

DEFAULT_TIMEFRAME = "7d"

# Units PostgreSQL's date_trunc accepts, and the only ones we ever pass to it.
# Interpolated as SQL text, never as a parameter from the request.
DATE_TRUNC_UNITS = ("hour", "day", "week", "month", "quarter")


class TimeframeError(ValueError):
    pass


def get(key: str | None) -> Timeframe:
    """The timeframe for a key from the URL; unknown or empty -> the default."""
    if not key:
        return TIMEFRAMES[DEFAULT_TIMEFRAME]
    try:
        return TIMEFRAMES[key]
    except KeyError:
        raise TimeframeError(
            f"unknown timeframe {key!r}; one of {', '.join(TIMEFRAMES)}") from None


# ── Calendar arithmetic ───────────────────────────────────────
# No dateutil: what is needed is "n units later" for five units, and the
# month case is the only one that is not a multiplication.

def _add_months(dt: datetime, months: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def add_units(dt: datetime, unit: str, n: int) -> datetime:
    if unit == "hour":
        return dt + timedelta(hours=n)
    if unit == "day":
        return dt + timedelta(days=n)
    if unit == "week":
        return dt + timedelta(weeks=n)
    if unit == "month":
        return _add_months(dt, n)
    if unit == "quarter":
        return _add_months(dt, 3 * n)
    raise TimeframeError(f"unknown unit {unit!r}")


def truncate(dt: datetime, unit: str) -> datetime:
    """The start of the bucket containing dt - the same answer PostgreSQL's
    date_trunc gives for the same unit (weeks start on Monday there too)."""
    dt = dt.replace(minute=0, second=0, microsecond=0)
    if unit == "hour":
        return dt
    dt = dt.replace(hour=0)
    if unit == "day":
        return dt
    if unit == "week":
        return dt - timedelta(days=dt.weekday())
    dt = dt.replace(day=1)
    if unit == "month":
        return dt
    if unit == "quarter":
        return dt.replace(month=dt.month - (dt.month - 1) % 3)
    raise TimeframeError(f"unknown unit {unit!r}")


# ── Windows ───────────────────────────────────────────────────

@dataclass(frozen=True)
class Window:
    timeframe: Timeframe
    page: int
    # Half-open: start <= t < end. Both UTC-aware.
    start: datetime
    end: datetime

    @property
    def unit(self) -> str:
        return self.timeframe.unit

    def buckets(self) -> list[datetime]:
        """Every bucket start in the window, so a chart can show zero bars for
        periods without rows instead of silently skipping them."""
        out = []
        t = self.start
        while t < self.end:
            out.append(t)
            t = add_units(t, self.unit, 1)
        return out

    def label(self, bucket_start: datetime) -> str:
        fmt = self.timeframe.label_format
        if "%q" in fmt:
            # strftime has no quarter directive.
            fmt = fmt.replace("%q", str((bucket_start.month - 1) // 3 + 1))
        return bucket_start.strftime(fmt)

    @property
    def caption(self) -> str:
        """What the toolbar says: the timeframe's name for the current window,
        the actual dates for anything older, where "last 7 days" would lie."""
        if self.page == 0:
            return self.timeframe.label
        last = self.end - timedelta(seconds=1)
        if self.unit == "hour":
            return f"{self.start:%Y-%m-%d %H:%M} to {last:%Y-%m-%d %H:%M} UTC"
        return f"{self.start:%Y-%m-%d} to {last:%Y-%m-%d}"

    def as_dict(self) -> dict:
        return {
            "timeframe": self.timeframe.key,
            "label": self.timeframe.label,
            "page": self.page,
            "unit": self.unit,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "caption": self.caption,
            "buckets": [{"start": b.isoformat(), "label": self.label(b)} for b in self.buckets()],
        }


def window_for(key: str | None, page: int = 0, now: datetime | None = None) -> Window:
    """The window for a timeframe and page.

    Page 0 ends at the end of the current bucket (the bar being filled right
    now is the last one); page n is n whole windows earlier. Negative pages are
    the future and are refused rather than drawn empty.
    """
    tf = get(key)
    if page < 0:
        raise TimeframeError("page must be 0 or more")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current_end = add_units(truncate(now, tf.unit), tf.unit, 1)
    end = add_units(current_end, tf.unit, -page * tf.buckets)
    start = add_units(end, tf.unit, -tf.buckets)
    return Window(tf, page, start, end)


def has_earlier_page(window: Window, oldest: datetime | None) -> bool:
    """Whether a "previous" button makes sense: only if the archive holds
    anything older than this window. `oldest` is the oldest timeline value the
    caller found, or None for an empty archive."""
    return oldest is not None and oldest < window.start
