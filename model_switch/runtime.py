"""运行时：把引擎算出来的目标状态应用到 AstrBot。

只做两件事：
1. 全局规则 -> 修改默认模型（agent_runner.config.model.provider_id）
2. 会话规则 -> 写入会话级 provider 偏好（复用 AstrBot 的 provider 隔离）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.core import sp
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.provider import Provider

from .engine import (
    Candidate,
    Plan,
    describe_plan,
    load_plans,
    next_boundary,
    resolve,
)

SP_KEY = f"provider_perf_{ProviderType.CHAT_COMPLETION.value}"
TICK_SECONDS = 20
_MISSING = object()


class ModelSwitchRuntime:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.context = plugin.context
        self.config = plugin.config or {}
        self.logger = plugin.logger

        self.enabled = True
        self.policy = "idle"
        self.idle_minutes = 30
        self.max_defer_hours = 6
        self.baseline = ""
        self.plans: list[Plan] = []
        self.plan_errors: list[str] = []
        self.tz: tzinfo = ZoneInfo("Asia/Shanghai")

        self._paused_until = 0.0
        self._written: dict[str, str] = {}
        self._last_desired: dict[str, str | None] = {}
        self._last_activity: dict[str, float] = {}
        self._pending_sessions: dict[str, tuple[str | None, float]] = {}
        self._pending_global: tuple[str, float] | None = None
        self._active_global_plan: int | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ load

    async def load(self) -> None:
        self.enabled = bool(self.config.get("enabled", True))
        self.policy = str(self.config.get("switch_policy") or "idle").strip()
        if self.policy not in ("idle", "immediate"):
            self.policy = "idle"
        self.idle_minutes = max(1, int(self.config.get("idle_minutes") or 30))
        self.max_defer_hours = max(0.0, float(self.config.get("max_defer_hours") or 0))
        self.tz = self._load_timezone()

        self.plans, self.plan_errors = load_plans(self.config.get("plans") or [])

        configured_baseline = str(self.config.get("baseline_provider") or "").strip()
        stored_baseline = str(await self._kv_get("baseline_provider", "") or "").strip()
        self.baseline = configured_baseline or stored_baseline
        if not self.baseline:
            current = self.global_provider_id()
            if current:
                self.baseline = current
                await self._kv_put("baseline_provider", current)

        self._paused_until = float(await self._kv_get("paused_until", 0) or 0)
        stored_written = await self._kv_get("written_prefs", {}) or {}
        if isinstance(stored_written, dict):
            self._written = {
                str(key): str(value) for key, value in stored_written.items()
            }

        self.logger.info(
            "model-switch loaded: %d plan(s), policy=%s, idle=%dmin, tz=%s, baseline=%s",
            len(self.plans),
            self.policy,
            self.idle_minutes,
            self.tz,
            self.baseline or "-",
        )
        for error in self.plan_errors:
            self.logger.warning("model-switch plan ignored: %s", error)

    def _load_timezone(self) -> tzinfo:
        name = str(self.config.get("timezone") or "").strip()
        if not name:
            try:
                name = str(self.context.get_config().get("timezone") or "").strip()
            except Exception:
                name = ""
        if not name:
            name = "Asia/Shanghai"
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            self.logger.warning("model-switch: unknown timezone %r, fallback to UTC", name)
            return ZoneInfo("UTC")

    # ----------------------------------------------------------------- state

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    async def _kv_get(self, key: str, default):
        try:
            value = await self.plugin.get_kv_data(key, default)
            return default if value is None else value
        except Exception:
            return default

    async def _kv_put(self, key: str, value) -> None:
        try:
            await self.plugin.put_kv_data(key, value)
        except Exception:
            self.logger.warning("model-switch: failed to persist %s", key, exc_info=True)

    def global_provider_id(self) -> str:
        try:
            cfg = self.context.get_config()
            agent_runner = cfg.get("agent_runner") or {}
            model = (agent_runner.get("config") or {}).get("model") or {}
            return str(model.get("provider_id") or "")
        except Exception:
            return ""

    def _runner_is_local(self) -> bool:
        try:
            agent_runner = self.context.get_config().get("agent_runner") or {}
            return str(agent_runner.get("runner_type") or "local") == "local"
        except Exception:
            return True

    def _provider(self, provider_id: str) -> Provider | None:
        if not provider_id:
            return None
        try:
            provider = self.context.get_provider_by_id(provider_id)
        except Exception:
            return None
        return provider if isinstance(provider, Provider) else None

    def _provider_label(self, provider_id: str) -> str:
        provider = self._provider(provider_id)
        if provider is None:
            return provider_id
        try:
            model = provider.meta().model
        except Exception:
            model = None
        return f"{provider_id} ({model})" if model else provider_id

    async def _session_provider_id(self, umo: str) -> str | None:
        try:
            value = await sp.session_get(umo, SP_KEY, None)
        except Exception:
            return None
        return str(value) if value else None

    def _is_paused(self, now: datetime) -> bool:
        paused_until = self._paused_until
        if paused_until == -1:
            return True
        if paused_until > 0:
            if now.timestamp() < paused_until:
                return True
            self._paused_until = 0.0  # 过期后自动恢复
        return False

    def _is_idle(self, umo: str, now: datetime) -> bool:
        last = self._last_activity.get(umo)
        if last is None:
            return True
        return now.timestamp() - last >= self.idle_minutes * 60

    def _all_idle(self, now: datetime) -> bool:
        if not self._last_activity:
            return True
        return all(self._is_idle(umo, now) for umo in self._last_activity)

    def _defer_expired(self, since: float, now: datetime) -> bool:
        if self.max_defer_hours <= 0:
            return False
        return now.timestamp() - since >= self.max_defer_hours * 3600

    # ------------------------------------------------------------------ tick

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("model-switch tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self) -> None:
        if not self.enabled:
            return
        now = self._now()
        if self._is_paused(now):
            return

        global_plans = [plan for plan in self.plans if plan.scope == "global"]
        candidate = resolve(global_plans, now)
        if candidate is not None:
            self._active_global_plan = candidate.plan.index
            desired = candidate.provider
            reason = self._reason(candidate)
        else:
            # 没有计划命中时，只在"计划刚刚结束"这一刻回落基线；
            # 之后用户手动改默认模型，插件不再干预。
            if self._active_global_plan is None:
                self._pending_global = None
                return
            desired = self.baseline
            reason = "回到基线"
            if not desired:
                self._active_global_plan = None
                return
        current = self.global_provider_id()

        if desired == current:
            self._pending_global = None
            if candidate is None:
                self._active_global_plan = None
            return

        if self._provider(desired) is None:
            self.logger.warning("model-switch: unknown/disabled provider %s", desired)
            return

        if self.policy == "idle" and not self._all_idle(now):
            if self._pending_global is None or self._pending_global[0] != desired:
                self._pending_global = (desired, now.timestamp())
            if not self._defer_expired(self._pending_global[1], now):
                return

        applied = await self._apply_global(desired, reason)
        if applied and candidate is None:
            self._active_global_plan = None

    def _reason(self, candidate: Candidate | None) -> str:
        if candidate is not None:
            return f"global 计划[{candidate.plan.name}]"
        return "回到基线"

    async def _apply_global(self, provider_id: str, reason: str) -> bool:
        if not self._runner_is_local():
            self.logger.warning(
                "model-switch: runner_type is not local, global switch to %s skipped",
                provider_id,
            )
            return False
        async with self._lock:
            cfg = self.context.get_config()
            agent_runner = cfg.setdefault("agent_runner", {})
            if str(agent_runner.get("runner_type") or "local") != "local":
                return False
            model = agent_runner.setdefault("config", {}).setdefault("model", {})
            previous = str(model.get("provider_id") or "")
            if previous == provider_id:
                self._pending_global = None
                return False
            model["provider_id"] = provider_id
            save_async = getattr(cfg, "save_config_async", None)
            if callable(save_async):
                await save_async()
            else:
                cfg.save_config()
            self._pending_global = None
            self.logger.info(
                "model-switch: global provider %s -> %s (%s)",
                previous or "-",
                provider_id,
                reason,
            )
            return True

    # --------------------------------------------------------------- session

    async def on_request(self, event) -> None:
        if not self.enabled:
            return
        now = self._now()
        umo = getattr(event, "unified_msg_origin", "")
        if not umo:
            return
        try:
            if not self._is_paused(now):
                await self._sync_session(event, umo, now)
        finally:
            self._last_activity[umo] = now.timestamp()

    async def _sync_session(self, event, umo: str, now: datetime) -> None:
        is_group = bool(event.get_group_id())
        candidate = resolve(self.plans, now, umo=umo, is_group=is_group)

        if candidate is not None and candidate.plan.scope != "global":
            desired: str | None = candidate.plan.provider
        else:
            desired = None  # 交给全局规则

        previous = self._last_desired.get(umo, _MISSING)
        if previous is not _MISSING and previous == desired:
            self._pending_sessions.pop(umo, None)
            return

        if self.policy == "idle" and not self._is_idle(umo, now):
            pending = self._pending_sessions.get(umo)
            since = pending[1] if pending else now.timestamp()
            if not self._defer_expired(since, now):
                self._pending_sessions[umo] = (desired, since)
                return

        current = await self._session_provider_id(umo)

        if desired is not None:
            if current == desired:
                self._last_desired[umo] = desired
                self._pending_sessions.pop(umo, None)
                if self._written.get(umo) != desired:
                    self._written[umo] = desired
                    await self._save_written()
                return
            if self._provider(desired) is None:
                self.logger.warning("model-switch: unknown/disabled provider %s", desired)
                return
            await self._set_session_provider(event, umo, desired)
            self._last_desired[umo] = desired
            self._pending_sessions.pop(umo, None)
            self._written[umo] = desired
            await self._save_written()
            self.logger.info(
                "model-switch: session %s -> %s (plan[%s])",
                umo,
                desired,
                candidate.plan.name if candidate else "-",
            )
            return

        # 目标回到全局：只清自己写过的偏好，用户的 /provider 不碰
        ours = self._written.get(umo)
        if current and ours and current == ours:
            await self._clear_session_provider(event, umo)
            self._written.pop(umo, None)
            await self._save_written()
            self.logger.info("model-switch: session %s cleared (%s)", umo, "no plan")
        self._last_desired[umo] = None
        self._pending_sessions.pop(umo, None)

    async def _set_session_provider(self, event, umo: str, provider_id: str) -> None:
        try:
            await self.context.provider_manager.set_provider(
                provider_id=provider_id,
                provider_type=ProviderType.CHAT_COMPLETION,
                umo=umo,
            )
        except Exception:
            self.logger.exception("model-switch: failed to switch session %s", umo)

    async def _clear_session_provider(self, event, umo: str) -> None:
        try:
            await sp.remove_async("umo", umo, SP_KEY)
        except Exception:
            self.logger.exception("model-switch: failed to clear session %s", umo)

    async def _save_written(self) -> None:
        await self._kv_put("written_prefs", dict(self._written))

    # -------------------------------------------------------------- commands

    async def status_text(self, event) -> str:
        now = self._now()
        umo = getattr(event, "unified_msg_origin", "")
        is_group = bool(event.get_group_id())
        global_id = self.global_provider_id()
        session_id = await self._session_provider_id(umo)
        effective = session_id or global_id

        candidate = resolve(self.plans, now, umo=umo, is_group=is_group)
        boundary = next_boundary(self.plans, now, umo=umo, is_group=is_group)
        pending = self._pending_sessions.get(umo)
        if self._pending_global is not None:
            pending_global = self._pending_global[0]
        else:
            pending_global = None

        lines = ["模型调度"]
        lines.append(f"当前：{self._provider_label(effective) if effective else '未配置'}")
        if umo:
            lines.append(f"会话：{umo}")
        if session_id:
            lines.append(f"来源：会话偏好（全局默认 {self._provider_label(global_id)}）")
        else:
            lines.append("来源：全局默认")
        if candidate is not None:
            plan = candidate.plan
            kind = "全局" if plan.scope == "global" else "会话"
            lines.append(f"生效：{kind}计划「{plan.name}」→ {plan.provider}")
        else:
            lines.append("生效：无命中计划")
        if boundary is not None:
            when, plan = boundary
            lines.append(f"下次：{when.strftime('%m-%d %H:%M')}「{plan.name}」")
        else:
            lines.append("下次：无")
        if pending is not None or pending_global is not None:
            target = pending[0] if pending is not None else pending_global
            lines.append(f"待切换：{target or '回到全局'}（等会话空闲）")
        lines.append(f"策略：{self.policy}（空闲 {self.idle_minutes} 分钟）")
        lines.append(f"基线：{self._provider_label(self.baseline) if self.baseline else '未设置'}")
        if self._is_paused(now):
            if self._paused_until == -1:
                lines.append("状态：已暂停（手动恢复）")
            else:
                until = datetime.fromtimestamp(self._paused_until, self.tz)
                lines.append(f"状态：已暂停到 {until.strftime('%m-%d %H:%M')}")
        else:
            lines.append("状态：运行中")
        if self.plan_errors:
            lines.append(f"配置问题：{len(self.plan_errors)} 条")
        return "\n".join(lines)

    async def pause(self, minutes: int) -> str:
        now = self._now()
        if minutes == 0:
            self._paused_until = -1
            await self._kv_put("paused_until", -1)
            return "已暂停调度，直到执行 /model resume"
        minutes = max(1, minutes)
        self._paused_until = now.timestamp() + minutes * 60
        await self._kv_put("paused_until", self._paused_until)
        until = datetime.fromtimestamp(self._paused_until, self.tz)
        return f"已暂停调度到 {until.strftime('%m-%d %H:%M')}"

    async def resume(self) -> str:
        self._paused_until = 0.0
        await self._kv_put("paused_until", 0)
        self._last_desired.clear()
        self._pending_sessions.clear()
        self._pending_global = None
        return "已恢复调度，下一条消息/下一次检查时重新求值"

    async def apply_now(self, event) -> str:
        """忽略空闲等待，立刻应用当前目标状态。"""
        now = self._now()
        umo = getattr(event, "unified_msg_origin", "")
        is_group = bool(event.get_group_id())
        messages: list[str] = []

        if self._is_paused(now):
            return "当前处于暂停状态，先执行 /model resume"

        global_plans = [plan for plan in self.plans if plan.scope == "global"]
        candidate = resolve(global_plans, now)
        desired = candidate.provider if candidate else self.baseline
        if desired and desired != self.global_provider_id():
            if self._provider(desired) is None:
                messages.append(f"全局目标 {desired} 不可用")
            elif await self._apply_global(desired, "手动 apply"):
                messages.append(f"全局 → {desired}")

        session_candidate = resolve(self.plans, now, umo=umo, is_group=is_group)
        if session_candidate is not None and session_candidate.plan.scope != "global":
            target = session_candidate.plan.provider
            if self._provider(target) is None:
                messages.append(f"会话目标 {target} 不可用")
            else:
                await self._set_session_provider(event, umo, target)
                self._written[umo] = target
                await self._save_written()
                self._last_desired[umo] = target
                messages.append(f"本会话 → {target}")
        else:
            current = await self._session_provider_id(umo)
            ours = self._written.get(umo)
            if current and ours and current == ours:
                await self._clear_session_provider(event, umo)
                self._written.pop(umo, None)
                await self._save_written()
                self._last_desired[umo] = None
                messages.append("本会话 → 回到全局")

        return "；".join(messages) if messages else "已经是目标状态，无需切换"

    async def plan_summary(self) -> str:
        if not self.plans:
            return "还没有可用计划，请在插件配置里添加。"
        return "\n".join(
            f"{index + 1}. {describe_plan(plan)}"
            for index, plan in enumerate(self.plans)
        )
