"""样品登记（送样清单）业务规则。

对外形态不变；内部按样本周期灰度模式分流。送样清单在周期里的责任范围：

* 「确认收样」对应实验室签收（样品必须先送样在途）——旧代码没有这个前置，
  未送样也能签收，是三处结论矛盾的来源之一，dual 模式由领域流转表拒绝；
* 「退回样品」对应送样/化验退回，回到送样组重新裁决；
* 「登记报告」在统一周期里必须先有化验完成事实，dual 模式下没有
  化验完成事件时拒绝（化验待办未完成不能先出报告）。
"""
from __future__ import annotations

from typing import Any

from app.sample_cycle import stages as cycle_stages
from app.sample_cycle.events import (
    ASSAY_COMPLETED,
    SAMPLE_RECEIVED,
    SAMPLE_REJECTED,
)
from app.sample_cycle.policy import PolicyViolation
from app.sample_cycle.service import (
    MODE_DUAL,
    MODE_SHADOW,
    sample_cycle_service,
)
from app.store import store

MODULE = "sample_registry"
REQUIRED_FIELDS = ["送检编号", "样品名称", "采样位置"]
STATUS_ORDER = ["待收样", "已收样", "检测中", "已出报告"]
ACTION_RULES = {"确认收样": "已收样", "登记报告": "已出报告", "退回样品": "待收样"}
NEGATIVE_ACTIONS = []

# 送样清单动作 -> 周期事件
ACTION_EVENT = {
    "确认收样": SAMPLE_RECEIVED,
    "退回样品": SAMPLE_REJECTED,
}


class SampleRegistryService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("送检编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"送检样品 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于样品登记可执行范围"

        mode = sample_cycle_service.get_mode()
        cycle = sample_cycle_service.find_cycle_by_ledger_row(MODULE, entry_id)
        if mode == "legacy" or cycle is None:
            if mode == MODE_DUAL and cycle is None:
                return None, f"送检样品 {entry_id} 尚未纳入样本周期，请先完成迁移回填"
            return self._run_legacy(entry, action)

        # shadow：旧路径先生效，领域侧只试跑不裁决（失败仅留双读差异）
        if mode == MODE_SHADOW:
            legacy_entry, message = self._run_legacy(entry, action)
            self._shadow_try(cycle, action)
            return legacy_entry, message

        if action == "登记报告":
            if cycle["stage"] != cycle_stages.ASSAY_DONE:
                return None, "化验尚未完成，不能登记报告；请先在化验待办完成并审核"
            # 报告登记是化验完成的读模型结果，不重复制造业务事件
            return store.find(MODULE, entry_id) or entry, "送检样品已登记报告"

        event_type = ACTION_EVENT[action]
        try:
            result = sample_cycle_service.apply_command(
                int(cycle["id"]), event_type, ledger=MODULE
            )
        except PolicyViolation as exc:
            return None, str(exc)
        note = "（幂等重放）" if result.get("idempotent") else ""
        return store.find(MODULE, entry_id) or entry, f"送检样品已{action}{note}"

    # ------------------------------------------------------------ 旧路径

    def _run_legacy(self, entry: dict[str, Any], action: str) -> tuple[dict[str, Any], str]:
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"送检样品已{action}"

    def _shadow_try(self, cycle: dict[str, Any], action: str) -> None:
        """shadow 模式：在领域侧试跑同一意图，只校验不落库。

        旧路径与领域裁决的分歧由 ``reconcile()`` 统一暴露，
        这里不阻断业务、不写事件，避免污染领域事件流。
        """
        if action not in ACTION_EVENT:
            return
        sample_cycle_service.dry_run_command(int(cycle["id"]), ACTION_EVENT[action])
