"""AstrBot 模型调度插件入口。"""

from __future__ import annotations

import asyncio
import contextlib

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .model_switch.runtime import ModelSwitchRuntime


class Main(Star):
    """按时间段/会话场景自动切换模型，空闲再切以保留 prompt 缓存。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        self.runtime = ModelSwitchRuntime(self)
        self._task: asyncio.Task | None = None

    async def initialize(self) -> None:
        await self.runtime.load()
        self._task = asyncio.create_task(
            self.runtime.run(),
            name="astrbot-model-switch",
        )

    async def terminate(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @filter.on_waiting_llm_request()
    async def _before_llm(self, event: AstrMessageEvent) -> None:
        try:
            await self.runtime.on_request(event)
        except Exception:
            self.logger.exception("model-switch: on_waiting_llm_request failed")

    @filter.command("model")
    async def model_command(
        self,
        event: AstrMessageEvent,
        action: str = "status",
        value: str = "",
    ):
        """查看模型调度状态，或暂停/恢复/立即应用。"""
        action = (action or "status").strip().lower()

        if action in ("status", "s", "状态"):
            yield event.plain_result(await self.runtime.status_text(event))
            return

        if action in ("plan", "plans", "计划"):
            yield event.plain_result(await self.runtime.plan_summary())
            return

        if not event.is_admin():
            yield event.plain_result("这个操作需要管理员权限。")
            return

        if action in ("pause", "暂停"):
            try:
                minutes = int(value) if value.strip() else 60
            except ValueError:
                yield event.plain_result("用法：/model pause [分钟]，0 表示一直暂停")
                return
            yield event.plain_result(await self.runtime.pause(minutes))
            return

        if action in ("resume", "恢复"):
            yield event.plain_result(await self.runtime.resume())
            return

        if action in ("apply", "应用"):
            yield event.plain_result(await self.runtime.apply_now(event))
            return

        yield event.plain_result(
            "用法：/model [status|plan|pause [分钟]|resume|apply]"
        )
