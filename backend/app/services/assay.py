"""化验数据（化验待办）业务规则。

对外形态不变；内部按样本周期灰度模式分流。化验组在周期里的责任：

* 「录入结果」对应建立/推进化验——前置必须是实验室已签收，旧代码没有
  这个前置（未收样也能录入），是三处结论矛盾的来源之二，dual 模式由
  领域流转表拒绝；
* 「审核通过」对应化验完成，通知送样清单可以出报告；
* 「退回修改」对应化验退回，样品回到送样组处置。
"""
from __future__ import annotations

from typing import Any

from app.sample_cycle.events import (
    ASSAY_COMPLETED,
    ASSAY_RETURNED,
    ASSAY_STARTED,
)
from app.sample_cycle.policy import PolicyViolation
from app.sample_cycle.service import MODE_DUAL, MODE_SHADOW, sample_cycle_service
from app.store import store

MODULE = "assay"
REQUIRED_FIELDS = ["化验编号", "样品编号", "元素名称"]
STATUS_ORDER = ["待录入", "已录入", "已审核", "已退回"]
ACTION_RULES = {"录入结果": "已录入", "审核通过": "已审核", "退回修改": "已退回"}
NEGATIVE_ACTIONS = []

ACTION_EVENT = {
    "录入结果": ASSAY_STARTED,
    "审核通过": ASSAY_COMPLETED,
    "退回修改": ASSAY_RETURNED,
}


class AssayService:
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
            rows = [row for row in rows if keyword in str(row.get("化验编号", ""))]
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
            return None, f"化验结果 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于化验数据可执行范围"

        mode = sample_cycle_service.get_mode()
        cycle = sample_cycle_service.find_cycle_by_ledger_row(MODULE, entry_id)
        if mode == "legacy" or cycle is None:
            if mode == MODE_DUAL and cycle is None:
                return None, f"化验结果 {entry_id} 尚未纳入样本周期，请先完成迁移回填"
            return self._run_legacy(entry, action)

        if mode == MODE_SHADOW:
            legacy_entry, message = self._run_legacy(entry, action)
            sample_cycle_service.dry_run_command(int(cycle["id"]), ACTION_EVENT[action])
            return legacy_entry, message

        try:
            result = sample_cycle_service.apply_command(
                int(cycle["id"]), ACTION_EVENT[action], ledger=MODULE
            )
        except PolicyViolation as exc:
            return None, str(exc)
        note = "（幂等重放）" if result.get("idempotent") else ""
        return store.find(MODULE, entry_id) or entry, f"化验结果已{action}{note}"

    # ------------------------------------------------------------ 旧路径

    def _run_legacy(self, entry: dict[str, Any], action: str) -> tuple[dict[str, Any], str]:
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"化验结果已{action}"
