"""岩心管理业务规则：状态流转、字段校验与筛选口径都收在这里。

重构后对外形态不变（list/get/create/actions 依旧），内部按灰度模式分流：

* legacy（默认）：走原裁决逻辑，领域完全不介入；
* shadow：旧逻辑照常生效，同时在领域侧试跑同一条命令，只记录双读差异；
* dual：命令改由「样本周期」领域裁决（统一可用性口径），结论原子回写
  岩心台账、送样清单、化验待办。

无论哪种模式，返回给路由层的 (entry, message) / (entry, missing) 结构不变。
"""
from __future__ import annotations

from typing import Any

from app.sample_cycle import stages as cycle_stages
from app.sample_cycle.events import (
    DEPTH_REVIEW_PASSED,
    DEPTH_REVIEW_REJECTED,
    GEOLOGY_LOGGED,
    SAMPLE_CLOSED,
    SAMPLE_DISPATCHED,
    SAMPLE_RETURNING,
)
from app.sample_cycle.policy import PolicyViolation
from app.sample_cycle.service import (
    MODE_DUAL,
    MODE_LEGACY,
    MODE_SHADOW,
    sample_cycle_service,
)
from app.store import store

MODULE = "core"
REQUIRED_FIELDS = ["岩心编号", "所属钻孔", "取样深度起"]
STATUS_ORDER = ["待编录", "已编录", "送样中", "已归还"]
ACTION_RULES = {"地质编录": "已编录", "送样分析": "送样中", "归还原箱": "已归还"}
NEGATIVE_ACTIONS = []

# 深度复核是新领域显式化的责任环节；旧台账没有独立动作入口，
# 这里以既有的 actions 形态追加两个动作名（接口路径不变）。
DEPTH_ACTIONS = {"深度复核通过": DEPTH_REVIEW_PASSED, "深度复核驳回": DEPTH_REVIEW_REJECTED}
ACTION_EVENT = {
    "地质编录": GEOLOGY_LOGGED,
    "送样分析": SAMPLE_DISPATCHED,
    "归还原箱": SAMPLE_CLOSED,
}
# 「归还原箱」之前样品要先启运归还（归档组责任），用归还在途事件过渡。
RETURNING_EVENT = SAMPLE_RETURNING
KNOWN_ACTIONS = set(ACTION_RULES) | set(DEPTH_ACTIONS)


class CoreService:
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
            rows = [row for row in rows if keyword in str(row.get("岩心编号", ""))]
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
        mode = sample_cycle_service.get_mode()
        with store.transaction():
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
            if mode in (MODE_DUAL, MODE_SHADOW):
                # 建档即建立样本周期（首事件），阶段投影统一回写台账字段；
                # 行先落表，投影才能按 id 找到它。
                sample_cycle_service.register_from_core(
                    entry["id"],
                    str(entry["岩心编号"]),
                    borehole=str(entry.get("所属钻孔", "")),
                    depth_from=entry.get("取样深度起"),
                    depth_to=entry.get("取样深度止"),
                )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"岩心样本 {entry_id} 不存在或已归档"
        if action not in KNOWN_ACTIONS:
            return None, f"动作「{action}」不属于岩心管理可执行范围"

        mode = sample_cycle_service.get_mode()
        if mode == MODE_LEGACY:
            return self._run_legacy(entry, action)

        cycle = sample_cycle_service.find_cycle_by_ledger_row("core", entry_id)
        if cycle is None:
            # 未接管的历史行（尚未回填）：shadow 下走旧路径，dual 下要求先回填
            if mode == MODE_SHADOW:
                return self._run_legacy(entry, action)
            return None, f"岩心样本 {entry_id} 尚未纳入样本周期，请先完成迁移回填"

        if mode == MODE_SHADOW:
            # shadow：旧裁决照常生效，领域只做不落库的试跑，分歧交双读暴露
            result_entry, message = self._run_legacy(entry, action)
            if action in DEPTH_ACTIONS:
                event_type = DEPTH_ACTIONS[action]
            elif action in ACTION_EVENT:
                event_type = ACTION_EVENT[action]
                if action == "归还原箱" and cycle["stage"] == cycle_stages.ASSAY_DONE:
                    # 旧动作没有「启运归还」环节，试跑直接验闭环事件即可
                    event_type = SAMPLE_CLOSED
            else:
                event_type = None
            if event_type is not None:
                sample_cycle_service.dry_run_command(int(cycle["id"]), event_type)
            return result_entry, message

        if action in DEPTH_ACTIONS:
            return self._run_cycle_command(entry, cycle, DEPTH_ACTIONS[action], action)
        event_type = ACTION_EVENT[action]
        try:
            if action == "归还原箱" and cycle["stage"] == cycle_stages.ASSAY_DONE:
                sample_cycle_service.apply_command(
                    int(cycle["id"]), RETURNING_EVENT, ledger="core"
                )
            result = sample_cycle_service.apply_command(
                int(cycle["id"]), event_type, ledger="core"
            )
        except PolicyViolation as exc:
            return None, str(exc)
        refreshed = store.find(MODULE, entry_id) or entry
        note = "（幂等重放）" if result.get("idempotent") else ""
        return refreshed, f"岩心样本已{action}{note}"

    # ------------------------------------------------------------ 旧路径

    def _run_legacy(self, entry: dict[str, Any], action: str) -> tuple[dict[str, Any], str]:
        if action in DEPTH_ACTIONS:
            return None, "深度复核动作仅在样本周期灰度开启后可用"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        self._apply_legacy_status(entry, target)
        return entry, f"岩心样本已{action}"

    @staticmethod
    def _apply_legacy_status(entry: dict[str, Any], target: str) -> None:
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = False

    def _run_cycle_command(
        self,
        entry: dict[str, Any],
        cycle: dict[str, Any],
        event_type: str,
        action: str,
    ) -> tuple[dict[str, Any] | None, str]:
        try:
            result = sample_cycle_service.apply_command(
                int(cycle["id"]), event_type, ledger="core"
            )
        except PolicyViolation as exc:
            return None, str(exc)
        refreshed = store.find(MODULE, int(entry["id"])) or entry
        note = "（幂等重放）" if result.get("idempotent") else ""
        return refreshed, f"岩心样本已{action}{note}"
