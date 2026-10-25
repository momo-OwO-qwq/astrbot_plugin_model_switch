from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from model_switch.engine import (  # noqa: E402
    PlanError,
    describe_plan,
    load_plans,
    next_boundary,
    parse_clock,
    parse_weekdays,
    resolve,
)

TZ = ZoneInfo("Asia/Shanghai")


def at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def plans(*items):
    loaded, errors = load_plans(list(items))
    assert not errors, errors
    return loaded


class ParseTests(unittest.TestCase):
    def test_clock(self):
        self.assertEqual(parse_clock("09:30").strftime("%H:%M"), "09:30")
        self.assertEqual(parse_clock("9:05").strftime("%H:%M"), "09:05")
        self.assertEqual(parse_clock("24:00").strftime("%H:%M"), "00:00")
        self.assertIsNone(parse_clock(""))
        with self.assertRaises(PlanError):
            parse_clock("25:00")

    def test_weekdays(self):
        self.assertEqual(parse_weekdays("1-5"), frozenset({1, 2, 3, 4, 5}))
        self.assertEqual(parse_weekdays("mon-fri"), frozenset({1, 2, 3, 4, 5}))
        self.assertEqual(parse_weekdays("6,7"), frozenset({6, 7}))
        self.assertEqual(parse_weekdays("0"), frozenset({7}))
        self.assertIsNone(parse_weekdays(""))
        self.assertIsNone(parse_weekdays("*"))


class PlanTests(unittest.TestCase):
    def test_errors(self):
        _, errors = load_plans(
            [
                {"provider": ""},
                {"provider": "a", "end": "18:00"},
                {"provider": "a", "scope": "session"},
                {"provider": "a", "scope": "nope"},
            ]
        )
        self.assertEqual(len(errors), 4)

    def test_describe(self):
        plan = plans(
            {"name": "夜间", "provider": "cheap", "start": "23:00", "end": "07:00"}
        )[0]
        text = describe_plan(plan)
        self.assertIn("夜间", text)
        self.assertIn("跨夜", text)


class ResolveTests(unittest.TestCase):
    def test_constant_group_rule(self):
        data = plans({"name": "群聊便宜", "scope": "group", "provider": "cheap"})
        now = at("2026-10-21 12:00")  # 周三
        self.assertEqual(resolve(data, now, "x:GroupMessage:1", True).provider, "cheap")
        self.assertIsNone(resolve(data, now, "x:FriendMessage:1", False))

    def test_window(self):
        data = plans(
            {
                "name": "白天",
                "provider": "strong",
                "start": "09:00",
                "end": "18:00",
                "weekdays": "1-5",
            }
        )
        self.assertEqual(resolve(data, at("2026-10-21 10:00")).provider, "strong")
        self.assertIsNone(resolve(data, at("2026-10-21 19:00")))
        self.assertIsNone(resolve(data, at("2026-10-24 10:00")))  # 周六

    def test_cross_midnight(self):
        data = plans(
            {"name": "夜间", "provider": "cheap", "start": "23:00", "end": "07:00"}
        )
        self.assertEqual(resolve(data, at("2026-10-22 01:00")).provider, "cheap")
        self.assertIsNone(resolve(data, at("2026-10-22 12:00")))

    def test_open_ended_rule(self):
        data = plans({"name": "切换", "provider": "strong", "start": "09:00"})
        self.assertEqual(resolve(data, at("2026-10-22 10:00")).provider, "strong")
        # 到点后一直保持：次日凌晨仍然生效
        self.assertEqual(resolve(data, at("2026-10-23 03:00")).provider, "strong")

    def test_specificity_beats_priority(self):
        data = plans(
            {"name": "全局", "provider": "global-x", "priority": 99},
            {"name": "群聊", "scope": "group", "provider": "group-y", "priority": 0},
        )
        now = at("2026-10-21 12:00")
        self.assertEqual(resolve(data, now, "umo", True).provider, "group-y")
        self.assertEqual(resolve(data, now, "umo", False).provider, "global-x")

    def test_priority_and_later_start(self):
        data = plans(
            {"name": "A", "provider": "a", "priority": 1},
            {"name": "B", "provider": "b", "priority": 2},
        )
        self.assertEqual(resolve(data, at("2026-10-21 12:00")).provider, "b")

        data2 = plans(
            {"name": "早", "provider": "early", "start": "09:00", "priority": 1},
            {"name": "晚", "provider": "late", "start": "12:00", "priority": 1},
        )
        self.assertEqual(resolve(data2, at("2026-10-21 10:00")).provider, "early")
        self.assertEqual(resolve(data2, at("2026-10-21 13:00")).provider, "late")

    def test_next_boundary(self):
        data = plans(
            {
                "name": "白天",
                "provider": "strong",
                "start": "09:00",
                "end": "18:00",
                "weekdays": "1-5",
            }
        )
        boundary, plan = next_boundary(data, at("2026-10-21 10:00"))
        self.assertEqual(boundary.strftime("%m-%d %H:%M"), "10-21 18:00")
        self.assertEqual(plan.name, "白天")

        boundary, _ = next_boundary(data, at("2026-10-21 19:00"))
        self.assertEqual(boundary.strftime("%m-%d %H:%M"), "10-22 09:00")


if __name__ == "__main__":
    unittest.main()
