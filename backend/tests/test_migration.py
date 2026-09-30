"""迁移回填测试：初始基准、旧裁决留痕、批次幂等、整批回滚、双读校验。"""
from __future__ import annotations

import copy
import unittest

from app.sample_cycle.events import GEOLOGY_LOGGED, LEDGERS, MIGRATION_BASELINE, LEGACY_RULING
from app.sample_cycle.service import DEFAULT_BATCH_ID, SampleCycleService
from app.store import Store


def consistent_seed() -> dict[str, list[dict]]:
    """三台账完全一致的样本：已编录未复核（三处旧状态都停在早期阶段）。"""
    return {
        "core": [{
            "id": 1, "status": "待编录", "pending": True, "abnormal": False,
            "岩心编号": "CORE-0900", "所属钻孔": "ZK-9",
            "取样深度起": "1", "取样深度止": "2",
        }],
        "sample_registry": [],
        "assay": [],
    }


class BackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store()
        self.svc = SampleCycleService(self.store)

    def tearDown(self) -> None:
        self.store.reset()

    def test_preview_detects_seed_split_brain(self) -> None:
        plan = self.svc.preview_backfill()
        # 种子数据三台账对同一样本结论互相矛盾，必须全部标红
        self.assertEqual(plan["cycle_count"], 3)
        self.assertTrue(all(cycle["divergent"] for cycle in plan["cycles"]))
        # 每个周期都带三条旧裁决留痕内容，且按旧口径、原样
        first = plan["cycles"][0]
        ledgers = {link["ledger"] for link in first["links"]}
        self.assertEqual(ledgers, set(LEDGERS))
        self.assertEqual(first["links"][0]["ruling"]["legacy_availability"], "可送样")

    def test_strict_mode_refuses_divergent_batch_without_any_write(self) -> None:
        before_events = list(self.store.rows("sample_cycle_event"))
        result = self.svc.run_backfill(DEFAULT_BATCH_ID, strict=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["divergent"])
        # 转换未成功不能部分写入：事件表、周期表、台账字段都应原样
        self.assertEqual(self.store.rows("sample_cycle_event"), before_events)
        self.assertEqual(self.store.rows("sample_cycle"), [])
        self.assertNotIn("样本可用性", self.store.find("core", 1))

    def test_non_strict_migration_baseline_and_legacy_rulings(self) -> None:
        result = self.svc.run_backfill(DEFAULT_BATCH_ID, strict=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["migrated"], 3)
        cycle = next(c for c in self.svc.list_cycles() if c["core_row_id"] == 2)
        # 以既有确认阶段为初始基准：core#2 旧状态「已编录」-> 基准 LOGGED
        self.assertEqual(cycle["stage"], "已编录")
        events = self.svc.events(int(cycle["id"]))
        rulings = [e for e in events if e["event_type"] == LEGACY_RULING]
        baselines = [e for e in events if e["event_type"] == MIGRATION_BASELINE]
        self.assertEqual(len(rulings), 3)   # 三台账旧裁决各一条
        self.assertEqual(len(baselines), 1)
        self.assertTrue(baselines[0]["payload"]["divergent"])
        # 双读校验：矛盾样本必须持续报红，直到人工处置
        report = self.svc.reconcile(persist=False)
        self.assertGreaterEqual(report["mismatch_count"], 3)
        self.assertFalse(report["consistent"])

    def test_batch_rerun_is_deterministic_and_idempotent(self) -> None:
        first = self.svc.run_backfill(DEFAULT_BATCH_ID, strict=False)
        snapshot = copy.deepcopy({
            "cycles": self.store.rows("sample_cycle"),
            "events": sorted(self.store.rows("sample_cycle_event"), key=lambda e: e["event_id"]),
            "core": self.store.rows("core"),
            "dispatch": self.store.rows("sample_registry"),
            "assay": self.store.rows("assay"),
        })
        # 重跑同一批次：全部跳过，数据逐字节不变
        second = self.svc.run_backfill(DEFAULT_BATCH_ID, strict=False)
        self.assertTrue(second["ok"])
        self.assertEqual(second["migrated"], 0)
        self.assertEqual(second["skipped"], 3)
        self.assertEqual(
            sorted(self.store.rows("sample_cycle_event"), key=lambda e: e["event_id"]),
            snapshot["events"],
        )
        self.assertEqual(self.store.rows("core"), snapshot["core"])
        self.assertEqual(self.store.rows("sample_registry"), snapshot["dispatch"])
        self.assertEqual(self.store.rows("assay"), snapshot["assay"])
        # 新批次号：同周期允许以新批次再留痕一次（内容哈希含 batch_id，
        # 是独立事实）；但基线事件相同、周期阶段不被二次改变
        stage_before = {c["id"]: c["stage"] for c in self.svc.list_cycles()}
        third = self.svc.run_backfill("baseline-other", strict=False)
        self.assertTrue(third["ok"])
        self.assertEqual(third["migrated"], 3)
        self.assertEqual(
            {c["id"]: c["stage"] for c in self.svc.list_cycles()},
            stage_before,
        )
        # 再跑新批次号，幂等跳过、结果完全相同
        third_rerun = self.svc.run_backfill("baseline-other", strict=False)
        self.assertEqual(third_rerun["migrated"], 0)
        self.assertEqual(third_rerun["skipped"], 3)

    def test_rollback_second_then_first_batch(self) -> None:
        self.svc.run_backfill("batch-A", strict=False)
        self.svc.run_backfill("batch-B", strict=False)
        # 先回滚第二批次：只删 B 的留痕，周期保留
        rb_b = self.svc.rollback_backfill("batch-B")
        self.assertTrue(rb_b["ok"], rb_b)
        self.assertEqual(len(self.svc.list_cycles()), 3)
        self.assertTrue(
            all(e.get("batch_id") != "batch-B" for e in self.store.rows("sample_cycle_event"))
        )
        # 再回滚首批次：周期与台账镜像一并撤销
        rb_a = self.svc.rollback_backfill("batch-A")
        self.assertTrue(rb_a["ok"], rb_a)
        self.assertEqual(self.svc.list_cycles(), [])

    def test_rollback_restores_ledger_rows_and_removes_cycles(self) -> None:
        core_before = copy.deepcopy(self.store.find("core", 1))
        self.svc.run_backfill(DEFAULT_BATCH_ID, strict=False)
        self.assertTrue(self.svc.list_cycles())
        result = self.svc.rollback_backfill(DEFAULT_BATCH_ID)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.svc.list_cycles(), [])
        self.assertEqual(self.store.rows("sample_cycle_event"), [])
        self.assertEqual(self.store.find("core", 1), core_before)

    def test_rollback_refused_after_business_event(self) -> None:
        self.svc.run_backfill(DEFAULT_BATCH_ID, strict=False)
        cycle = next(c for c in self.svc.list_cycles() if c["core_row_id"] == 1)
        # core#1 基准是 REGISTERED，可以合法推进一次地质编录
        self.svc.apply_command(int(cycle["id"]), GEOLOGY_LOGGED, ledger="core")
        result = self.svc.rollback_backfill(DEFAULT_BATCH_ID)
        self.assertFalse(result["ok"])
        self.assertIn("业务事件", result["reason"])
        # 数据保持迁移后状态，没有被部分删除
        self.assertTrue(self.svc.list_cycles())


if __name__ == "__main__":
    unittest.main()
