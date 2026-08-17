from __future__ import annotations

from datetime import UTC, datetime

import pytest

from testops.api.cron import CronExpression, CronValidationError, validate_timezone


def test_cron_calculates_weekday_occurrences_in_project_timezone() -> None:
    expression = CronExpression.parse("0 9 * * 1-5")
    assert expression.next_after(
        datetime(2026, 8, 13, 0, 59, tzinfo=UTC),
        "Asia/Shanghai",
    ) == datetime(2026, 8, 13, 1, 0, tzinfo=UTC)
    assert expression.next_after(
        datetime(2026, 8, 14, 1, 0, tzinfo=UTC),
        "Asia/Shanghai",
    ) == datetime(2026, 8, 17, 1, 0, tzinfo=UTC)


def test_cron_skips_nonexistent_dst_time_and_preserves_repeated_fold() -> None:
    spring = CronExpression.parse("30 2 * * *")
    assert spring.next_after(
        datetime(2026, 3, 8, 6, 0, tzinfo=UTC),
        "America/New_York",
    ) == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)

    autumn = CronExpression.parse("30 1 * * *")
    assert autumn.next_after(
        datetime(2026, 11, 1, 5, 31, tzinfo=UTC),
        "America/New_York",
    ) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "expression",
    (
        "* * * *",
        "60 * * * *",
        "*/0 * * * *",
        "* 23-2 * * *",
        "* * * * MON",
    ),
)
def test_cron_rejects_unsupported_or_out_of_range_expressions(expression: str) -> None:
    with pytest.raises(CronValidationError):
        CronExpression.parse(expression)


def test_timezone_validation_requires_an_iana_zone() -> None:
    assert validate_timezone("UTC") == "UTC"
    with pytest.raises(CronValidationError):
        validate_timezone("Mars/Olympus")
