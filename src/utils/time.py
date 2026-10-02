from __future__ import annotations

from datetime import datetime, time, timedelta
from functools import lru_cache

import pytz
from dateutil import parser

from ..utils.config import get_config


@lru_cache(maxsize=1)
def get_timezone() -> pytz.BaseTzInfo:
    config = get_config()
    return pytz.timezone(config.market_hours.timezone)


def now() -> datetime:
    return datetime.now(get_timezone())


def today() -> datetime:
    return now().replace(hour=0, minute=0, second=0, microsecond=0)


def market_open_time(date: datetime | None = None) -> datetime:
    config = get_config()
    tz = get_timezone()
    if date is None:
        date = today()
    return tz.localize(
        datetime.combine(date.date(), time.fromisoformat(config.market_hours.open))
    )


def market_close_time(date: datetime | None = None) -> datetime:
    config = get_config()
    tz = get_timezone()
    if date is None:
        date = today()
    return tz.localize(
        datetime.combine(date.date(), time.fromisoformat(config.market_hours.close))
    )


def pre_market_start_time(date: datetime | None = None) -> datetime:
    config = get_config()
    tz = get_timezone()
    if date is None:
        date = today()
    return tz.localize(
        datetime.combine(date.date(), time.fromisoformat(config.market_hours.pre_market_start))
    )


def post_market_end_time(date: datetime | None = None) -> datetime:
    config = get_config()
    tz = get_timezone()
    if date is None:
        date = today()
    return tz.localize(
        datetime.combine(date.date(), time.fromisoformat(config.market_hours.post_market_end))
    )


def is_market_hours(dt: datetime | None = None) -> bool:
    if dt is None:
        dt = now()
    if dt.tzinfo is None:
        dt = get_timezone().localize(dt)
    open_time = market_open_time(dt)
    close_time = market_close_time(dt)
    return open_time <= dt <= close_time


def is_pre_market(dt: datetime | None = None) -> bool:
    if dt is None:
        dt = now()
    if dt.tzinfo is None:
        dt = get_timezone().localize(dt)
    pre_start = pre_market_start_time(dt)
    open_time = market_open_time(dt)
    return pre_start <= dt < open_time


def is_post_market(dt: datetime | None = None) -> bool:
    if dt is None:
        dt = now()
    if dt.tzinfo is None:
        dt = get_timezone().localize(dt)
    close_time = market_close_time(dt)
    post_end = post_market_end_time(dt)
    return close_time < dt <= post_end


def round_to_minute(dt: datetime, minute_interval: int = 1) -> datetime:
    if dt.tzinfo is None:
        dt = get_timezone().localize(dt)
    discard = timedelta(
        minutes=dt.minute % minute_interval,
        seconds=dt.second,
        microseconds=dt.microsecond,
    )
    return dt - discard


def next_bar_time(dt: datetime, minute_interval: int = 1) -> datetime:
    rounded = round_to_minute(dt, minute_interval)
    return rounded + timedelta(minutes=minute_interval)


def parse_iso_datetime(s: str) -> datetime:
    dt = parser.isoparse(s)
    if dt.tzinfo is None:
        dt = get_timezone().localize(dt)
    return dt.astimezone(get_timezone())


def format_ist(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = get_timezone().localize(dt)
    return dt.astimezone(get_timezone()).strftime("%Y-%m-%d %H:%M:%S %Z")


def minutes_since_open(dt: datetime | None = None) -> int:
    if dt is None:
        dt = now()
    open_time = market_open_time(dt)
    if dt < open_time:
        return 0
    diff = dt - open_time
    return int(diff.total_seconds() / 60)