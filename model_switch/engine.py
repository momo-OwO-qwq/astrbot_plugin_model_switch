"""纯逻辑调度引擎：不依赖 AstrBot，方便单测。

核心概念：
- 一条 Plan 描述"哪类会话、在什么时间、用哪个 provider"。
- 时间有两种写法：只有 start（到点后一直生效）和 start+end（时段）。
- 同一会话命中多条规则时，具体作用域优先，其次 priority，再其次开始时间更晚者。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

__all__ = [
    "Plan",
    "PlanError",
    "Candidate",
    "Interval",
    "load_plans",
    "parse_clock",
    "parse_weekdays",
    "resolve",
    "next_boundary",
    "scope_matches",
    "describe_plan",
]

WEEKDAY_NAMES = {
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
    "sun": 7,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
    "sunday": 7,
}

SCOPES = ("global", "group", "private", "session")
SCOPE_LABELS = {
    "global": "全局",
    "group": "群聊",
    "private": "私聊",
    "session": "指定会话",
}
_SPECIFICITY = {"global": 1, "group": 2, "private": 2, "session": 3}
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_EPOCH = datetime(1970, 1, 1)


class PlanError(ValueError):
    """规则配置不合法。"""


def parse_clock(value: object) -> time | None:
    """解析 HH:MM；空值返回 None，24:00 按次日 00:00 处理。"""
    text = str(value or "").strip()
    if not text:
        return None
    match = _CLOCK_RE.match(text)
    if not match:
        raise PlanError(f"时间格式应为 HH:MM，收到 {text!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour == 24 and minute == 0:
        return time(0, 0)
    if hour > 23 or minute > 59:
        raise PlanError(f"时间超出范围：{text!r}")
    return time(hour, minute)


def _parse_weekday(token: str) -> int:
    token = token.strip().lower()
    if token in WEEKDAY_NAMES:
        return WEEKDAY_NAMES[token]
    if token.isdigit():
        value = int(token)
        if 0 <= value <= 7:
            return 7 if value == 0 else value
    raise PlanError(f"无法识别的星期：{token!r}（可用 1-7 或 mon-sun）")


def parse_weekdays(value: object) -> frozenset[int] | None:
    """解析 1-5 / mon-fri / 1,3,5 这类写法；空值或 * 表示每天。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple, set)):
        tokens = [str(item) for item in value]
    else:
        text = str(value).strip()
        if not text or text == "*":
            return None
        tokens = [item for item in re.split(r"[,\s]+", text) if item]

    days: set[int] = set()
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token.lower() in ("*", "all", "每天"):
            return None
        if "-" in token:
            left, right = token.split("-", 1)
            start, end = _parse_weekday(left), _parse_weekday(right)
            if start <= end:
                days.update(range(start, end + 1))
            else:  # 支持 fri-mon 这种跨周写法
                days.update(range(start, 8))
                days.update(range(1, end + 1))
        else:
            days.add(_parse_weekday(token))
    return frozenset(days) if days else None


@dataclass(frozen=True)
class Plan:
    index: int
    name: str
    scope: str
    provider: str
    session: str
    start: time | None
    end: time | None
    weekdays: frozenset[int] | None
    priority: int
    enabled: bool

    @property
    def specificity(self) -> int:
        return _SPECIFICITY[self.scope]

    @property
    def always(self) -> bool:
        return self.start is None


@dataclass(frozen=True)
class Interval:
    """当前生效的时间区间；start 为 None 表示常驻规则。"""

    start: datetime | None
    end: datetime | None
    since: datetime | None


@dataclass(frozen=True)
class Candidate:
    plan: Plan
    interval: Interval
    key: tuple

    @property
    def provider(self) -> str:
        return self.plan.provider


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def parse_plan(item: dict, index: int) -> Plan:
    name = str(item.get("name") or f"计划 {index + 1}").strip()
    scope = str(item.get("scope") or "global").strip().lower()
    if scope not in SCOPES:
        raise PlanError(f"{name}: scope 只能是 {'/'.join(SCOPES)}")

    provider = str(item.get("provider") or "").strip()
    if not provider:
        raise PlanError(f"{name}: 未选择 provider")

    session = str(item.get("session") or "").strip()
    if scope == "session" and not session:
        raise PlanError(f"{name}: scope=session 时必须填写 session（umo）")

    start = parse_clock(item.get("start"))
    end = parse_clock(item.get("end"))
    if end is not None and start is None:
        raise PlanError(f"{name}: 填了结束时间就必须填开始时间")

    try:
        priority = int(item.get("priority") or 0)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{name}: priority 必须是整数") from exc

    return Plan(
        index=index,
        name=name,
        scope=scope,
        provider=provider,
        session=session,
        start=start,
        end=end,
        weekdays=parse_weekdays(item.get("weekdays")),
        priority=priority,
        enabled=_as_bool(item.get("enabled"), True),
    )


def load_plans(raw: object) -> tuple[list[Plan], list[str]]:
    """把配置里的 template_list 转成 Plan，返回 (可用规则, 错误列表)。"""
    plans: list[Plan] = []
    errors: list[str] = []
    if not isinstance(raw, list):
        return plans, errors
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"第 {index + 1} 条规则不是对象，已跳过")
            continue
        try:
            plan = parse_plan(item, index)
        except PlanError as exc:
            errors.append(str(exc))
            continue
        if plan.enabled:
            plans.append(plan)
    return plans, errors


def scope_matches(plan: Plan, umo: str, is_group: bool) -> bool:
    if plan.scope == "global":
        return True
    if plan.scope == "group":
        return is_group
    if plan.scope == "private":
        return not is_group
    return bool(umo) and umo == plan.session


def _combine(day: date, clock: time, tz: tzinfo | None) -> datetime:
    return datetime.combine(day, clock, tzinfo=tz)


def active_interval(
    plan: Plan,
    now: datetime,
    lookback_days: int = 8,
) -> Interval | None:
    """返回 now 时刻命中的区间；不命中返回 None。"""
    if plan.start is None:
        return Interval(None, None, None)

    today = now.date()
    for offset in range(0, lookback_days + 1):
        day = today - timedelta(days=offset)
        if plan.weekdays is not None and day.isoweekday() not in plan.weekdays:
            continue
        start_dt = _combine(day, plan.start, now.tzinfo)
        if start_dt > now:
            continue
        if plan.end is None:
            return Interval(start_dt, None, start_dt)
        end_dt = _combine(day, plan.end, now.tzinfo)
        if end_dt <= start_dt:  # 跨午夜或 24 小时
            end_dt = _combine(day + timedelta(days=1), plan.end, now.tzinfo)
        if now < end_dt:
            return Interval(start_dt, end_dt, start_dt)
        # 该窗口已经结束；更早的窗口只会更早结束。
        return None
    return None


def resolve(
    plans: list[Plan],
    now: datetime,
    umo: str = "",
    is_group: bool = False,
) -> Candidate | None:
    """返回当前应生效的规则；没有命中返回 None。"""
    best: Candidate | None = None
    epoch = _EPOCH.replace(tzinfo=now.tzinfo)
    for plan in plans:
        if not plan.enabled or not scope_matches(plan, umo, is_group):
            continue
        interval = active_interval(plan, now)
        if interval is None:
            continue
        since = interval.since or epoch
        key = (plan.specificity, plan.priority, since, plan.index)
        if best is None or key > best.key:
            best = Candidate(plan=plan, interval=interval, key=key)
    return best


def next_boundary(
    plans: list[Plan],
    now: datetime,
    umo: str = "",
    is_group: bool = False,
    horizon_days: int = 8,
) -> tuple[datetime, Plan] | None:
    """返回下一次规则切换时间（开始或结束）。"""
    best: tuple[datetime, Plan] | None = None
    today = now.date()
    for plan in plans:
        if not plan.enabled or plan.start is None:
            continue
        if not scope_matches(plan, umo, is_group):
            continue
        for offset in range(-1, horizon_days + 1):
            day = today + timedelta(days=offset)
            if plan.weekdays is not None and day.isoweekday() not in plan.weekdays:
                continue
            start_dt = _combine(day, plan.start, now.tzinfo)
            if start_dt > now and (best is None or start_dt < best[0]):
                best = (start_dt, plan)
            if plan.end is not None:
                end_dt = _combine(day, plan.end, now.tzinfo)
                if end_dt <= start_dt:
                    end_dt = _combine(day + timedelta(days=1), plan.end, now.tzinfo)
                if end_dt > now and (best is None or end_dt < best[0]):
                    best = (end_dt, plan)
    return best


def _format_weekdays(plan: Plan) -> str:
    if plan.weekdays is None:
        return "每天"
    if plan.weekdays == frozenset({1, 2, 3, 4, 5}):
        return "工作日"
    if plan.weekdays == frozenset({6, 7}):
        return "周末"
    return "周" + ",".join(str(day) for day in sorted(plan.weekdays))


def describe_plan(plan: Plan) -> str:
    if plan.start is None:
        when = "常驻"
    elif plan.end is None:
        when = f"{plan.start.strftime('%H:%M')} 起"
    else:
        when = f"{plan.start.strftime('%H:%M')}-{plan.end.strftime('%H:%M')}"
        if plan.end <= plan.start:
            when += "（跨夜）"
    return (
        f"{plan.name}｜{SCOPE_LABELS[plan.scope]}｜{when}｜"
        f"{_format_weekdays(plan)}｜{plan.provider}"
    )
