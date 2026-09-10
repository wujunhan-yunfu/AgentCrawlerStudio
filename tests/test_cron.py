"""backend.services.cron 测试: cron 表达式校验与下次运行时间计算。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.services.cron import CronError, next_runs, parse_cron, validate_cron


@pytest.mark.parametrize(
    "expr",
    [
        "* * * * *",
        "*/5 * * * *",
        "0 8 * * 1-5",
        "*/15 9-18 * * MON-FRI",
        "0 0 1 1 *",
        "0 0 * * 7",
        "0 0 * * SUN",
        "@daily",
        "@hourly",
        "@yearly",
        "1,2,3 0 1 JAN-MAR *",
    ],
)
def test_validate_valid(expr):
    ok, error = validate_cron(expr)
    assert ok, error
    assert error == ""


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "bad",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "0 24 * * *",
        "0 0 0 * *",
        "0 0 32 * *",
        "0 0 * 13 *",
        "0 0 * * 8",
        "0 0 * * FOO",
        "5-1 * * * *",
        "*/0 * * * *",
        "0 0 30 2 *",
        "0 0 31 2 *",
    ],
)
def test_validate_invalid(expr):
    ok, error = validate_cron(expr)
    assert not ok
    assert error


def test_parse_cron_sunday_7_normalized():
    spec = parse_cron("0 0 * * 7")
    assert 0 in spec.dow
    assert 7 not in spec.dow


def test_next_runs_weekdays():
    base = datetime(2026, 9, 10, 6, 0, tzinfo=timezone.utc)  # 周四
    runs = next_runs("0 8 * * 1-5", count=3, base=base)
    assert [r.isoformat() for r in runs] == [
        "2026-09-10T08:00:00+00:00",
        "2026-09-11T08:00:00+00:00",
        "2026-09-14T08:00:00+00:00",
    ]


def test_next_runs_step():
    base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    runs = next_runs("*/15 * * * *", count=4, base=base)
    assert [r.minute for r in runs] == [15, 30, 45, 0]


def test_next_runs_dom_or_dow():
    # 同时限定 日 与 周 时, 任一匹配即运行(Vixie cron 语义)
    base = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    runs = next_runs("0 0 1 * MON", count=4, base=base)
    assert len(runs) == 4


def test_next_runs_leap_day_returns_partial():
    base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    runs = next_runs("0 0 29 2 *", count=5, base=base)
    assert runs and all(r.month == 2 and r.day == 29 for r in runs)


def test_next_runs_impossible_raises():
    base = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(CronError):
        next_runs("0 0 30 2 *", count=1, base=base)


def test_next_runs_naive_base_treated_as_utc():
    runs = next_runs("0 12 * * *", count=1, base=datetime(2026, 1, 1, 11, 30))
    assert runs[0] == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
