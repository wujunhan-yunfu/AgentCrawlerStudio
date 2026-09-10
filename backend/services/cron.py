"""cron 表达式校验与下次运行时间计算(纯标准库, 无第三方依赖)。

本模块同时被后端接口与导出的独立脚本包复用(导出时源码整体写入包的 cron.py)。

支持标准 5 字段 cron: `分 时 日 月 周`
- 字段语法: `*` | 数字 | 范围 `a-b` | 步进 `*/n` 或 `a-b/n` | 列表 `a,b,c`
- 月份支持 `JAN`-`DEC`, 星期支持 `SUN`-`SAT`(0 与 7 均表示周日)
- 支持宏: `@yearly` `@annually` `@monthly` `@weekly` `@daily` `@midnight` `@hourly`
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

__all__ = ["CronError", "parse_cron", "validate_cron", "next_runs"]

_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_DOW_NAMES = {
    "SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6,
}

# 搜索下次运行时间的最长跨度(覆盖 2 月 29 日等低频表达式)
_MAX_HORIZON = timedelta(days=366 * 5)


class CronError(ValueError):
    """cron 表达式非法。"""


def _parse_value(token: str, names: dict[str, int] | None, lo: int, hi: int) -> int:
    text = token.strip().upper()
    if names and text in names:
        return names[text]
    if not text.isdigit():
        raise CronError(f"非法取值: {token!r}")
    value = int(text)
    if value < lo or value > hi:
        raise CronError(f"取值超出范围 {lo}-{hi}: {token!r}")
    return value


def _parse_field(
    field: str,
    lo: int,
    hi: int,
    names: dict[str, int] | None = None,
    wrap: Callable[[int], int] | None = None,
) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError("存在空字段")
        step = 1
        rng = part
        if "/" in part:
            rng, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronError(f"非法步进值: {part!r}")
            step = int(step_text)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            left, _, right = rng.partition("-")
            start = _parse_value(left, names, lo, hi)
            end = _parse_value(right, names, lo, hi)
            if start > end:
                raise CronError(f"范围起始不能大于结束: {part!r}")
        else:
            start = end = _parse_value(rng, names, lo, hi)
        for value in range(start, end + 1, step):
            values.add(wrap(value) if wrap else value)
    if not values:
        raise CronError(f"字段为空: {field!r}")
    return values


class _Spec:
    def __init__(
        self,
        minutes: set[int],
        hours: set[int],
        dom: set[int],
        month: set[int],
        dow: set[int],
        dom_restricted: bool,
        dow_restricted: bool,
    ) -> None:
        self.minutes = minutes
        self.hours = hours
        self.dom = dom
        self.month = month
        self.dow = dow
        self.dom_restricted = dom_restricted
        self.dow_restricted = dow_restricted

    def matches(self, moment: datetime) -> bool:
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.month:
            return False
        dom_ok = moment.day in self.dom
        # datetime.weekday(): 周一=0 ... 周日=6; cron: 周日=0 ... 周六=6
        dow_ok = ((moment.weekday() + 1) % 7) in self.dow
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        if self.dom_restricted:
            return dom_ok
        if self.dow_restricted:
            return dow_ok
        return True


def parse_cron(expression: str) -> _Spec:
    """解析 cron 表达式, 非法时抛 CronError。"""
    expr = (expression or "").strip()
    if not expr:
        raise CronError("cron 表达式不能为空")
    macro = _MACROS.get(expr.lower())
    if macro:
        expr = macro
    fields = expr.split()
    if len(fields) != 5:
        raise CronError(f"需要 5 个字段(分 时 日 月 周), 实际 {len(fields)} 个: {expr!r}")
    return _Spec(
        minutes=_parse_field(fields[0], 0, 59),
        hours=_parse_field(fields[1], 0, 23),
        dom=_parse_field(fields[2], 1, 31),
        month=_parse_field(fields[3], 1, 12, _MONTH_NAMES),
        dow=_parse_field(fields[4], 0, 7, _DOW_NAMES, wrap=lambda v: 0 if v == 7 else v),
        dom_restricted=fields[2].strip() != "*",
        dow_restricted=fields[4].strip() != "*",
    )


def validate_cron(expression: str) -> tuple[bool, str]:
    """校验 cron 表达式, 返回 (是否合法, 错误信息)。

    除语法外还会确认 5 年内确实存在匹配的运行时间(如 2 月 30 日这类
    语法合法但永不触发的表达式会被判为非法)。
    """
    try:
        next_runs(expression, count=1)
        return True, ""
    except CronError as exc:
        return False, str(exc)


def next_runs(
    expression: str,
    count: int = 5,
    base: datetime | None = None,
) -> list[datetime]:
    """计算从 base(默认当前 UTC) 起接下来的 count 次运行时间(UTC)。"""
    spec = parse_cron(expression)
    now = base or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cursor = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    deadline = cursor + _MAX_HORIZON
    runs: list[datetime] = []
    while len(runs) < count and cursor <= deadline:
        if spec.matches(cursor):
            runs.append(cursor)
        cursor += timedelta(minutes=1)
    if not runs:
        raise CronError("在 5 年内未找到匹配的运行时间, 请检查表达式(如 2 月 30 日不存在)")
    return runs
