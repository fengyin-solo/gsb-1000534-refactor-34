"""统一可用性口径（policy）测试：证明三处结论同源。"""
from __future__ import annotations

import unittest

from app.sample_cycle import stages as S
from app.sample_cycle.events import (
    ASSAY_STARTED,
    DEPTH_REVIEW_PASSED,
    GEOLOGY_LOGGED,
    SAMPLE_DISPATCHED,
    SAMPLE_RECEIVED,
)
from app.sample_cycle.policy import (
    PolicyViolation,
    assay_availability,
    availability_triple,
    core_availability,
    dispatch_availability,
    legacy_ruling,
    resolve_transition,
)


class AvailabilityPolicyTest(unittest.TestCase):
    def test_depth_review_is_the_single_gate_for_dispatch(self) -> None:
        # 复核通过前三处都不可用/不能化验
        for stage in (S.REGISTERED, S.LOGGED):
            triple = availability_triple(stage)
            self.assertEqual(triple["core"], "不可用")
            self.assertNotEqual(triple["sample_registry"], "可安排送样")
            self.assertNotEqual(triple["assay"], "可化验")

        # 复核通过：岩心可送样 == 送样清单可安排，是同一个阶段推出的两个视图
        reviewed = availability_triple(S.DEPTH_REVIEWED)
        self.assertEqual(core_availability(S.DEPTH_REVIEWED), "可送样")
        self.assertEqual(dispatch_availability(S.DEPTH_REVIEWED), "可安排送样")
        self.assertEqual(reviewed["core"], "可送样")
        self.assertEqual(reviewed["sample_registry"], "可安排送样")
        # 此时实验室未签收，化验待办必须不可录入
        self.assertEqual(reviewed["assay"], "不可用")

    def test_assay_only_after_lab_receipt(self) -> None:
        # 在途：化验待办只能等待
        self.assertEqual(assay_availability(S.DISPATCHED), "待裁决")
        # 签收后：化验可录入，岩心/送样侧不再重复判「可送样」
        triple = availability_triple(S.RECEIVED)
        self.assertEqual(triple["assay"], "可化验")
        self.assertEqual(triple["core"], "不可用")
        self.assertNotEqual(triple["sample_registry"], "可安排送样")

    def test_transition_table_rejects_cross_group_jumps(self) -> None:
        # 旧 bug 复现点：已编录（未过深度复核）直接送样，必须被拒
        with self.assertRaises(PolicyViolation):
            resolve_transition(S.LOGGED, SAMPLE_DISPATCHED)
        # 未签收直接化验，必须被拒
        with self.assertRaises(PolicyViolation):
            resolve_transition(S.DISPATCHED, ASSAY_STARTED)
        # 合法链路：编录 -> 复核通过 -> 送样 -> 签收 -> 化验
        stage = S.REGISTERED
        for event in (GEOLOGY_LOGGED, DEPTH_REVIEW_PASSED, SAMPLE_DISPATCHED, SAMPLE_RECEIVED, ASSAY_STARTED):
            stage, group, _note = resolve_transition(stage, event)
        self.assertEqual(stage, S.IN_LAB)

    def test_legacy_ruling_reproduces_old_split_brain(self) -> None:
        # seed 里 CORE/SAMP/ASSA-0002 的旧状态，旧口径给出三个互相打架的结论
        self.assertEqual(legacy_ruling("core", "已编录")["legacy_availability"], "可送样")
        self.assertEqual(legacy_ruling("sample_registry", "已收样")["legacy_availability"], "可安排送样")
        self.assertEqual(legacy_ruling("assay", "已录入")["legacy_availability"], "可化验")
        # 同一样本在新口径下（已编录、深度未复核）三处都不该放行
        triple = availability_triple(S.LOGGED)
        self.assertEqual(triple, {"core": "不可用", "sample_registry": "不可用", "assay": "不可用"})


if __name__ == "__main__":
    unittest.main()
