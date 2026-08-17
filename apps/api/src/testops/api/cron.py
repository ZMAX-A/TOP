"""Small, deterministic five-field Cron evaluator with IANA timezone support."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CronValidationError(ValueError):
    """Raised when a Cron expression or timezone is not supported."""


@dataclass(frozen=True, slots=True)
class _CronField:
    values: frozenset[int]
    wildcard: bool


def _parse_number(value: str, *, minimum: int, maximum: int, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise CronValidationError(f"{name} contains a non-numeric value: {value}") from exc
    if not minimum <= parsed <= maximum:
        raise CronValidationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _parse_field(raw: str, *, minimum: int, maximum: int, name: str) -> _CronField:
    if not raw:
        raise CronValidationError(f"{name} cannot be empty")
    values: set[int] = set()
    for item in raw.split(","):
        if not item or item.count("/") > 1:
            raise CronValidationError(f"invalid {name} segment: {item or raw}")
        base, separator, raw_step = item.partition("/")
        step = 1
        if separator:
            try:
                step = int(raw_step)
            except ValueError as exc:
                raise CronValidationError(f"{name} step must be numeric") from exc
            if step <= 0:
                raise CronValidationError(f"{name} step must be greater than zero")

        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, raw_end = base.split("-", 1)
            start = _parse_number(raw_start, minimum=minimum, maximum=maximum, name=name)
            end = _parse_number(raw_end, minimum=minimum, maximum=maximum, name=name)
            if start > end:
                raise CronValidationError(f"{name} range start must not exceed its end")
        else:
            start = _parse_number(base, minimum=minimum, maximum=maximum, name=name)
            end = maximum if separator else start
        values.update(range(start, end + 1, step))
    if not values:
        raise CronValidationError(f"{name} selects no values")
    return _CronField(frozenset(values), wildcard=raw == "*")


@dataclass(frozen=True, slots=True)
class CronExpression:
    """Parsed numeric Cron expression using minute/hour/day/month/weekday fields."""

    expression: str
    minute: _CronField
    hour: _CronField
    day_of_month: _CronField
    month: _CronField
    day_of_week: _CronField

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        normalized = " ".join(expression.split())
        parts = normalized.split(" ")
        if len(parts) != 5:
            raise CronValidationError("Cron expression must contain exactly five fields")
        minute = _parse_field(parts[0], minimum=0, maximum=59, name="minute")
        hour = _parse_field(parts[1], minimum=0, maximum=23, name="hour")
        day_of_month = _parse_field(parts[2], minimum=1, maximum=31, name="day of month")
        month = _parse_field(parts[3], minimum=1, maximum=12, name="month")
        raw_weekday = _parse_field(parts[4], minimum=0, maximum=7, name="day of week")
        day_of_week = _CronField(
            frozenset(0 if value == 7 else value for value in raw_weekday.values),
            wildcard=raw_weekday.wildcard,
        )
        return cls(
            expression=normalized,
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month=month,
            day_of_week=day_of_week,
        )

    def _date_matches(self, candidate: date) -> bool:
        if candidate.month not in self.month.values:
            return False
        day_match = candidate.day in self.day_of_month.values
        cron_weekday = (candidate.weekday() + 1) % 7
        weekday_match = cron_weekday in self.day_of_week.values
        if self.day_of_month.wildcard and self.day_of_week.wildcard:
            return True
        if self.day_of_month.wildcard:
            return weekday_match
        if self.day_of_week.wildcard:
            return day_match
        return day_match or weekday_match

    def next_after(self, instant: datetime, timezone_name: str) -> datetime:
        """Return the first matching UTC instant strictly after ``instant``."""

        zone = timezone(timezone_name)
        aware_instant = instant if instant.tzinfo is not None else instant.replace(tzinfo=UTC)
        utc_instant = aware_instant.astimezone(UTC)
        first_local_date = utc_instant.astimezone(zone).date()
        max_days = 366 * 8 + 2
        for offset in range(max_days):
            local_date = first_local_date + timedelta(days=offset)
            if not self._date_matches(local_date):
                continue
            candidates: list[datetime] = []
            for hour in sorted(self.hour.values):
                for minute in sorted(self.minute.values):
                    local_naive = datetime.combine(local_date, time(hour=hour, minute=minute))
                    for fold in (0, 1):
                        local_aware = local_naive.replace(tzinfo=zone, fold=fold)
                        candidate = local_aware.astimezone(UTC)
                        round_trip = candidate.astimezone(zone)
                        if (
                            round_trip.replace(tzinfo=None) != local_naive
                            or round_trip.fold != fold
                        ):
                            continue
                        if candidate > utc_instant:
                            candidates.append(candidate)
            if candidates:
                return min(candidates)
        raise CronValidationError("Cron expression has no occurrence within eight years")


def timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronValidationError(f"unknown IANA timezone: {name}") from exc


def normalize_cron_expression(expression: str) -> str:
    return CronExpression.parse(expression).expression


def validate_timezone(name: str) -> str:
    timezone(name)
    return name
