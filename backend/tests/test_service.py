"""样本周期应用服务测试：命令幂等、事务回滚、三台账原子回写。"""
from __future__ import annotations

import unittest

from app.sample_cycle import stages as S
from app.sample_cycle.events import (
    ASSAY_COMPLETED,
    ASSAY_STARTED,
    DEPTH_REVIEW_PASSED,
    DEPTH_REVIEW_REJECTED,
    GEOLOGY_LOGGED,
    SAMPLE_CLOSED,
    SAMPLE_DISPATCHED,
    SAMPLE_RECEIVED,
    SAMPLE_RETURNING,
)
from app.sample_cycle.policy import PolicyViolation
from app.sample_cycle.service import (
    DEFAULT_BATCH_ID,
    MODE_LEGACY,
    MODE_SHADOW,
    SampleCycleService,
)
from app.store import Store


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.svc = SampleCycleService(self.store)
        # 隔离种子数据：只保留本用例构造的样本，避免双读统计串扰
        self.store.rows("core").clear()
        self.store.rows("sample_registry").clear()
        self.store.rows("assay").clear()
        # 岩心主档先行；送样清单/化验待办行在流转到对应责任组时再创建挂接
        self.store.rows("core").append({
            "id": 101, "status": "待编录", "pending": True, "abnormal": False,
            "岩心编号": "CORE-0501", "所属钻孔": "ZK-01",
            "取样深度起": "10.0", "取样深度止": "12.0",
        })
        result = self.svc.run_backfill(DEFAULT_BATCH_ID, strict=False)
        self.assertTrue(result["ok"], result)
        self.cycle_id = next(
            int(c["id"]) for c in self.svc.list_cycles() if c["core_row_id"] == 101
        )
        # 运行期新建的清单/待办行（没有历史裁决，迁移不会把它们当矛盾行）
        self.store.rows("sample_registry").append({
            "id": 201, "status": "待收样", "pending": True, "abnormal": False,
            "送检编号": "SAMP-0501", "样品名称": "岩心样本0501",
        })
        self.store.rows("assay").append({
            "id": 301, "status": "待录入", "pending": True, "abnormal": False,
            "化验编号": "ASSA-0501", "样品编号": "SAMPLE-0501",
        })

    def tearDown(self) -> None:
        self.store.reset()

    def _cmd(self, event_type: str, ledger: str = "core", **payload):
        return self.svc.apply_command(self.cycle_id, event_type, ledger=ledger, payload=payload or None)

    def test_mode_defaults_to_legacy(self) -> None:
        fresh = SampleCycleService(Store())
        self.assertEqual(fresh.get_mode(), MODE_LEGACY)
        result = fresh.set_mode(MODE_SHADOW)
        self.assertTrue(result["changed"])
        self.assertEqual(fresh.get_mode(), MODE_SHADOW)
        # 幂等：重复设置同模式不新增审计行
        again = fresh.set_mode(MODE_SHADOW)
        self.assertFalse(again["changed"])

    def test_full_happy_path_writes_all_three_ledgers_consistently(self) -> None:
        self._cmd(GEOLOGY_LOGGED)
        self.assertEqual(self.svc.current_stage(self.cycle_id), S.LOGGED)
        self._cmd(DEPTH_REVIEW_PASSED, ledger="core")
        # 复核通过：岩心台账得到「可送样」结论
        core_row = self.store.find("core", 101)
        self.assertEqual(core_row["样本可用性"], "可送样")
        self.assertEqual(core_row["周期阶段"], S.DEPTH_REVIEWED)
        # 送样清单行在进入送样环节时挂接，立刻得到同源结论
        self.svc.attach_ledger_row(self.cycle_id, "sample_registry", 201)
        dispatch_row = self.store.find("sample_registry", 201)
        self.assertEqual(dispatch_row["样本可用性"], "可安排送样")

        self._cmd(SAMPLE_DISPATCHED, ledger="sample_registry")
        self._cmd(SAMPLE_RECEIVED, ledger="sample_registry")
        # 签收后挂接化验待办：自动得到「可化验」，三表同源
        self.svc.attach_ledger_row(self.cycle_id, "assay", 301)
        assay_row = self.store.find("assay", 301)
        self.assertEqual(assay_row["样本可用性"], "可化验")
        self.assertEqual(assay_row["status"], "待录入")

        self._cmd(ASSAY_STARTED, ledger="assay")
        self.assertEqual(self.store.find("assay", 301)["status"], "已录入")
        self._cmd(ASSAY_COMPLETED, ledger="assay")
        self.assertEqual(self.store.find("sample_registry", 201)["status"], "已出报告")
        self._cmd(SAMPLE_RETURNING, ledger="core")
        self._cmd(SAMPLE_CLOSED, ledger="core")
        self.assertEqual(self.store.find("core", 101)["status"], "已归还")

        # 终态双读校验必须无分歧
        report = self.svc.reconcile(persist=False)
        self.assertTrue(report["consistent"], report)

    def test_command_idempotency_same_intent_replayed(self) -> None:
        self._cmd(GEOLOGY_LOGGED)
        first = self.svc.apply_command(
            self.cycle_id, DEPTH_REVIEW_PASSED, ledger="core", idempotency_key="req-7"
        )
        self.assertFalse(first["idempotent"])
        events_before = len(self.svc.events(self.cycle_id))
        # 同一阶段、同一事件、同一幂等号：重放不新增事件、不改阶段
        replay = self.svc.apply_command(
            self.cycle_id, DEPTH_REVIEW_PASSED, ledger="core", idempotency_key="req-7"
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(len(self.svc.events(self.cycle_id)), events_before)
        # 驳回 -> 处置 -> 再送样是不同自然键（from_stage 不同），允许再次发生
        self._cmd(SAMPLE_DISPATCHED, ledger="sample_registry")
        self.svc.apply_command(self.cycle_id, "sample_rejected", ledger="sample_registry")
        self.assertEqual(self.svc.current_stage(self.cycle_id), S.DISPATCH_READY)
        again = self._cmd(SAMPLE_DISPATCHED, ledger="sample_registry")
        self.assertFalse(again["idempotent"])

    def test_transaction_rolls_back_everything_on_policy_violation(self) -> None:
        self._cmd(GEOLOGY_LOGGED)
        # 未过复核就送样：领域拒绝
        with self.assertRaises(PolicyViolation):
            self._cmd(SAMPLE_DISPATCHED, ledger="sample_registry")
        # 事件未追加、阶段未变、台账未被部分改写
        self.assertEqual(self.svc.current_stage(self.cycle_id), S.LOGGED)
        types = [e["event_type"] for e in self.svc.events(self.cycle_id)]
        self.assertNotIn(SAMPLE_DISPATCHED, types)
        self.assertEqual(self.store.find("core", 101)["status"], "已编录")

    def test_forced_failure_midway_leaves_no_partial_write(self) -> None:
        # 人为在投影阶段注入异常：事件与台账都必须回到命令前
        original = self.svc._project_all

        def boom(cycle_id: int, stage: str) -> None:
            raise RuntimeError("模拟回写中断")

        self._cmd(GEOLOGY_LOGGED)
        self.svc._project_all = boom  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self._cmd(DEPTH_REVIEW_PASSED)
        finally:
            self.svc._project_all = original  # type: ignore[method-assign]
        self.assertEqual(self.svc.current_stage(self.cycle_id), S.LOGGED)
        types = [e["event_type"] for e in self.svc.events(self.cycle_id)]
        self.assertNotIn(DEPTH_REVIEW_PASSED, types)
        self.assertNotIn("可送样", self.store.find("core", 101).get("样本可用性", ""))

    def test_reject_path_reopens_depth_review(self) -> None:
        self._cmd(GEOLOGY_LOGGED)
        self._cmd(DEPTH_REVIEW_REJECTED, ledger="core")
        self.assertEqual(self.svc.current_stage(self.cycle_id), S.DEPTH_REJECTED)
        self.assertTrue(self.store.find("core", 101)["abnormal"])
        # 驳回态不能送样
        with self.assertRaises(PolicyViolation):
            self._cmd(SAMPLE_DISPATCHED, ledger="sample_registry")
        # 补录重编后可以再次提交复核
        self._cmd(GEOLOGY_LOGGED)
        self._cmd(DEPTH_REVIEW_PASSED, ledger="core")
        self.assertEqual(self.svc.current_stage(self.cycle_id), S.DEPTH_REVIEWED)


if __name__ == "__main__":
    unittest.main()
