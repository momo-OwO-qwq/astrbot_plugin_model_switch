from __future__ import annotations

import asyncio
import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_switch import runtime as runtime_module  # noqa: E402


class FakeProvider:
    def __init__(self, provider_id):
        self._id = provider_id

    def meta(self):
        return type("Meta", (), {"id": self._id, "model": self._id.split("/")[-1]})()


class FakeProviderManager:
    def __init__(self):
        self.calls = []

    async def set_provider(self, provider_id, provider_type, umo=None):
        self.calls.append((provider_id, provider_type, umo))
        if umo:
            runtime_module.sp.data[(umo, runtime_module.SP_KEY)] = provider_id


class FakeConfig(dict):
    def __init__(self, data):
        super().__init__(data)
        self.saved = 0

    async def save_config_async(self):
        self.saved += 1


class FakeContext:
    def __init__(self, provider_id="base/model"):
        self.config = FakeConfig(
            {
                "timezone": "Asia/Shanghai",
                "agent_runner": {
                    "runner_type": "local",
                    "config": {"model": {"provider_id": provider_id}},
                },
            }
        )
        self.known = {"base/model", "cheap/model", "group/model", "manual/model"}
        self.provider_manager = FakeProviderManager()

    def get_config(self):
        return self.config

    def get_provider_by_id(self, provider_id):
        return FakeProvider(provider_id) if provider_id in self.known else None


class FakeSP:
    def __init__(self):
        self.data = {}

    async def session_get(self, umo, key, default=None):
        return self.data.get((umo, key), default)

    async def session_put(self, umo, key, value):
        self.data[(umo, key)] = value

    async def remove_async(self, scope, scope_id, key):
        self.data.pop((scope_id, key), None)


class FakePlugin:
    def __init__(self, context, config):
        self.context = context
        self.config = config
        self.logger = logging.getLogger("test.model_switch")
        self.kv = {}

    async def get_kv_data(self, key, default):
        return self.kv.get(key, default)

    async def put_kv_data(self, key, value):
        self.kv[key] = value


class FakeEvent:
    def __init__(self, umo, group=""):
        self.unified_msg_origin = umo
        self._group = group

    def get_group_id(self):
        return self._group

    def is_admin(self):
        return True


def make_runtime(plans, policy="immediate"):
    context = FakeContext()
    config = {
        "enabled": True,
        "switch_policy": policy,
        "idle_minutes": 30,
        "max_defer_hours": 0,
        "plans": plans,
    }
    plugin = FakePlugin(context, config)
    rt = runtime_module.ModelSwitchRuntime(plugin)
    runtime_module.Provider = FakeProvider
    runtime_module.sp = FakeSP()
    return rt, context


class RuntimeTests(unittest.TestCase):
    def test_global_switch(self):
        async def case():
            rt, context = make_runtime(
                [{"name": "常驻", "scope": "global", "provider": "cheap/model"}]
            )
            await rt.load()
            self.assertEqual(rt.baseline, "base/model")
            await rt.tick()
            self.assertEqual(
                context.config["agent_runner"]["config"]["model"]["provider_id"],
                "cheap/model",
            )
            self.assertEqual(context.config.saved, 1)

        asyncio.run(case())

    def test_session_switch_and_clear(self):
        async def case():
            rt, context = make_runtime(
                [{"name": "群聊", "scope": "group", "provider": "group/model"}]
            )
            await rt.load()
            event = FakeEvent("qq:GroupMessage:1", group="1")
            await rt.on_request(event)
            self.assertEqual(
                context.provider_manager.calls[-1][0], "group/model"
            )
            self.assertEqual(
                await runtime_module.sp.session_get(
                    "qq:GroupMessage:1", runtime_module.SP_KEY, None
                ),
                "group/model",
            )

            # 规则取消后应清掉自己写的偏好
            rt.plans = []
            event2 = FakeEvent("qq:GroupMessage:1", group="1")
            await rt.on_request(event2)
            self.assertIsNone(
                await runtime_module.sp.session_get(
                    "qq:GroupMessage:1", runtime_module.SP_KEY, None
                )
            )

        asyncio.run(case())

    def test_idle_defers(self):
        async def case():
            rt, context = make_runtime(
                [{"name": "群聊", "scope": "group", "provider": "group/model"}],
                policy="idle",
            )
            await rt.load()
            umo = "qq:GroupMessage:1"
            rt._last_activity[umo] = rt._now().timestamp()  # 刚说过话
            event = FakeEvent(umo, group="1")
            await rt.on_request(event)
            self.assertEqual(context.provider_manager.calls, [])
            self.assertIn(umo, rt._pending_sessions)

        asyncio.run(case())


    def test_no_plan_does_not_fight_manual_change(self):
        async def case():
            rt, context = make_runtime([])
            await rt.load()
            context.config["agent_runner"]["config"]["model"]["provider_id"] = (
                "manual/model"
            )
            await rt.tick()
            self.assertEqual(
                context.config["agent_runner"]["config"]["model"]["provider_id"],
                "manual/model",
            )

            # 计划生效 -> 结束后回落基线一次
            rt.plans = runtime_module.load_plans(
                [{"name": "常驻", "scope": "global", "provider": "cheap/model"}]
            )[0]
            await rt.tick()
            self.assertEqual(
                context.config["agent_runner"]["config"]["model"]["provider_id"],
                "cheap/model",
            )
            rt.plans = []
            await rt.tick()
            self.assertEqual(
                context.config["agent_runner"]["config"]["model"]["provider_id"],
                "base/model",
            )

            # 再手动改，不会被立即拉回
            context.config["agent_runner"]["config"]["model"]["provider_id"] = (
                "manual/model"
            )
            await rt.tick()
            self.assertEqual(
                context.config["agent_runner"]["config"]["model"]["provider_id"],
                "manual/model",
            )

        asyncio.run(case())

    def test_pause_and_status(self):
        async def case():
            rt, _ = make_runtime(
                [{"name": "常驻", "scope": "global", "provider": "cheap/model"}]
            )
            await rt.load()
            await rt.pause(30)
            await rt.tick()
            text = await rt.status_text(FakeEvent("qq:FriendMessage:1"))
            self.assertIn("已暂停", text)
            self.assertIn("模型调度", text)
            self.assertIn("已恢复", await rt.resume())

        asyncio.run(case())


if __name__ == "__main__":
    unittest.main()
