"""旧三台账服务的灰度适配测试：接口形态保持、三模式分流、统一裁决生效。"""
from __future__ import annotations

import unittest

from app.sample_cycle.service import (
    DEFAULT_BATCH_ID,
    MODE_DUAL,
    MODE_LEGACY,
    MODE_SHADOW,
    sample_cycle_service,
)
from app.services.assay import AssayService
from app.services.core import CoreService
from app.services.sample_registry import SampleRegistryService
from app.store import store


class LegacyAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        # 以 core#2（已编录）为锚点迁移，三台账矛盾在 strict=False 下带标迁移
        self.core = CoreService()
        self.dispatch = SampleRegistryService()
        self.assay = AssayService()

    def tearDown(self) -> None:
        sample_cycle_service.set_mode(MODE_LEGACY)
        store.reset()

    def test_legacy_mode_preserves_old_split_brain_behaviour(self) -> None:
        # 默认 legacy：旧路径完全不变 —— 已编录未复核也能直接送样（旧 bug 口径）
        entry, message = self.core.run_action(2, "送样分析")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "送样中")
        # 未收样也能录入化验结果
        assay_entry, _ = self.assay.run_action(2, "录入结果")
        self.assertEqual(assay_entry["status"], "已录入")

    def test_shadow_mode_keeps_legacy_writes_but_exposes_divergence(self) -> None:
        sample_cycle_service.set_mode(MODE_SHADOW)
        sample_cycle_service.run_backfill(DEFAULT_BATCH_ID, strict=False)
        # shadow 下旧动作照常生效
        entry, _ = self.core.run_action(2, "送样分析")
        self.assertEqual(entry["status"], "送样中")
        # 领域侧没有被这次非法跳转污染（没有 sample_dispatched 事件）
        cycle = sample_cycle_service.find_cycle_by_ledger_row("core", 2)
        types = [e["event_type"] for e in sample_cycle_service.events(int(cycle["id"]))]
        self.assertNotIn("sample_dispatched", types)
        # 双读校验必须把分歧报出来
        report = sample_cycle_service.reconcile(persist=False)
        self.assertFalse(report["consistent"])

    def test_dual_mode_unified_gate_blocks_old_shortcuts(self) -> None:
        sample_cycle_service.set_mode(MODE_DUAL)
        sample_cycle_service.run_backfill(DEFAULT_BATCH_ID, strict=False)
        cycle = sample_cycle_service.find_cycle_by_ledger_row("core", 2)
        # 已编录但深度复核未过：送样被统一裁决拒绝
        entry, message = self.core.run_action(2, "送样分析")
        self.assertIsNone(entry)
        self.assertIn("未放行", message)
        # 深度复核动作通过既有 actions 形态暴露
        entry, message = self.core.run_action(2, "深度复核通过")
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["样本可用性"], "可送样")
        # 复核通过后送样成功，三台账结论同源回写
        entry, _ = self.core.run_action(2, "送样分析")
        self.assertEqual(entry["status"], "送样中")
        dispatch_entry = store.find("sample_registry", 2)
        self.assertEqual(dispatch_entry["周期阶段"], "送样中")
        # 未签收前化验录入被拒
        assay_entry, message = self.assay.run_action(2, "录入结果")
        self.assertIsNone(assay_entry)
        self.assertIn("未放行", message)
        # 签收之后化验才放行
        received, _ = self.dispatch.run_action(2, "确认收样")
        self.assertEqual(received["status"], "已收样")
        assay_entry, _ = self.assay.run_action(2, "录入结果")
        self.assertEqual(assay_entry["status"], "已录入")
        self.assertEqual(assay_entry["样本可用性"], "可化验")

    def test_dual_mode_create_entry_opens_cycle(self) -> None:
        sample_cycle_service.set_mode(MODE_DUAL)
        entry, missing = self.core.create_entry({
            "岩心编号": "CORE-7777", "所属钻孔": "ZK-7", "取样深度起": "3.0",
        })
        self.assertEqual(missing, [])
        cycle = sample_cycle_service.find_cycle_by_ledger_row("core", int(entry["id"]))
        self.assertIsNotNone(cycle)
        self.assertEqual(cycle["stage"], "已登记")
        self.assertEqual(entry["样本可用性"], "不可用")

    def test_action_interface_shape_unchanged(self) -> None:
        # 所有模式下 run_action 都返回 (entry|None, message) 二元组
        sample_cycle_service.set_mode(MODE_LEGACY)
        result = self.core.run_action(999_999, "地质编录")
        self.assertIsNone(result[0])
        self.assertIn("不存在或已归档", result[1])
        bad = self.core.run_action(1, "不存在的动作")
        self.assertIsNone(bad[0])
        self.assertIn("不属于岩心管理可执行范围", bad[1])


if __name__ == "__main__":
    unittest.main()
