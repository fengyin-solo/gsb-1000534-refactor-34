"""样本周期灰度门面：新旧读写路径按模式转换，可灰度、可回滚。

三种模式（运行时可切换，见 CycleFeature）：
- off：完全旧路径，样本周期能力不参与任何读写（回滚兜底态）；
- shadow：读旧写旧，同时用统一裁决双读校验，差异只留痕、不改返回
  （灰度观察期：先看三处口径会在哪里打架）；
- on：以样本周期为单一事实源，命令经状态机裁决，结果回写三处台账；
  行的对外视图由“存储事实 + 统一投影”合并得到，旧键语义保持等价。

门面保持旧服务的全部方法签名（list_entries/get_entry/create_entry/run_action），
三个 router/service 只需把实现委托到这里，接口形态零变化。
"""
from __future__ import annotations

import os
from typing import Any

from app.domain.sample_cycle.cycle_store import CycleRepository
from app.domain.sample_cycle.legacy import LEGACY_SPECS, LegacyLedgerService
from app.domain.sample_cycle.migration import CycleMigrator, MigrationPlanner
from app.domain.sample_cycle.projection import (
    assess_cycle,
    projection_fields,
    sample_key_of,
)
from app.store import store

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
VALID_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)
CYCLE_LEDGER_MODULES = tuple(LEGACY_SPECS.keys())


class CycleFeature:
    """灰度开关：环境变量初始化，运行时可切换（管理接口使用）。"""

    def __init__(self, mode: str = MODE_OFF) -> None:
        self.mode = mode if mode in VALID_MODES else MODE_OFF

    def set_mode(self, mode: str) -> str:
        if mode not in VALID_MODES:
            raise ValueError(f"未知灰度模式：{mode}")
        self.mode = mode
        return self.mode


def _initial_mode() -> str:
    mode = os.environ.get("SAMPLE_CYCLE_MODE", MODE_OFF).strip().lower()
    return mode if mode in VALID_MODES else MODE_OFF


feature = CycleFeature(_initial_mode())
repo = CycleRepository(store)
migrator = CycleMigrator(store, repo)


class UnifiedLedgerService:
    """委托门面：按灰度模式在旧口径与统一周期能力之间路由。"""

    def __init__(self, module: str) -> None:
        if module not in LEGACY_SPECS:
            raise ValueError(f"{module} 不在样本周期统一管理范围内")
        self.module = module
        self.legacy = LegacyLedgerService(module)

    # ----- 列表 / 明细 ----------------------------------------------------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        if feature.mode != MODE_ON:
            return self.legacy.list_entries(
                keyword=keyword, status=status, page=page, size=size
            )
        # on：先按旧口径过滤业务键（岩心编号/送检编号/化验编号），状态过滤对
        # 统一投影后的视图生效；分页前合并，保证总数口径一致。
        merged = [self._merged_view(row) for row in self.legacy.all_rows()]
        if keyword:
            field_name = LEGACY_SPECS[self.module]["keyword_field"]
            merged = [row for row in merged if keyword in str(row.get(field_name, ""))]
        if status:
            merged = [row for row in merged if row.get("status") == status]
        total = len(merged)
        start = max(page - 1, 0) * size
        return merged[start:start + size], total

    def all_entries(self) -> list[dict[str, Any]]:
        if feature.mode != MODE_ON:
            return self.legacy.all_rows()
        return [self._merged_view(row) for row in self.legacy.all_rows()]

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        if feature.mode != MODE_ON:
            return self.legacy.get_entry(entry_id)
        row = store.find(self.module, entry_id)
        if row is None:
            return None
        return self._merged_view(row)

    # ----- 登记 -----------------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        # 登记 + 周期建立 + 投影回写在同一事务：失败时连新行一起回滚，不留半成品。
        with store.transaction():
            entry, missing = self.legacy.create_entry(values)
            if missing or entry is None:
                return entry, missing
            if feature.mode == MODE_ON and self.module == "core":
                self._register_core_cycle(entry)
            elif feature.mode == MODE_ON:
                # 送样/化验行若带岩心编号引用，挂接到既有周期并统一投影，
                # 不隐式新建周期：没有岩心根的游离行仍按独立样本处理。
                ref = sample_key_of(self.module, entry)
                cycle = repo.get_cycle(ref) if ref else None
                if cycle is not None:
                    cycle.source_rows[self.module] = int(entry["id"])
                    repo.project_cycle(cycle)
                    repo.save_cycle(cycle)
            entry = store.find(self.module, int(entry["id"]))
        return entry, []

    def _register_core_cycle(self, entry: dict[str, Any]) -> None:
        from app.domain.sample_cycle.events import EVENT_KIND_COMMAND, CycleEvent, SampleCycle
        from app.domain.sample_cycle.stages import STAGE_REGISTERED
        from app.domain.sample_cycle.cycle_store import utc_now_iso

        sample_key = str(entry["岩心编号"])
        occurred_at = utc_now_iso()
        cycle = repo.get_cycle(sample_key) or SampleCycle(sample_key)
        cycle.source_rows["core"] = int(entry["id"])
        cycle.append(
            CycleEvent(
                id=f"reg-{sample_key}",
                sample_key=sample_key,
                kind=EVENT_KIND_COMMAND,
                action="岩心登记",
                from_stage=STAGE_REGISTERED,
                to_stage=STAGE_REGISTERED,
                negative=False,
                ledger="core",
                batch_id=None,
                seq=repo.reserve_seq(),
                occurred_at=occurred_at,
                payload={"entry_id": entry["id"]},
            )
        )
        repo.project_cycle(cycle, synced_at=occurred_at)
        repo.save_cycle(cycle)

    # ----- 动作 -----------------------------------------------------------

    def run_action(self, entry_id: int, action: str, idempotency_key: str | None = None) -> tuple[dict[str, Any] | None, str]:
        # off：旧路径；shadow：旧路径执行后双读留痕，返回仍是旧结果。
        if feature.mode in (MODE_OFF, MODE_SHADOW):
            entry, message = self.legacy.run_action(entry_id, action)
            if entry is not None and feature.mode == MODE_SHADOW and action in LEGACY_SPECS[self.module]["actions"]:
                self._shadow_record(entry_id)
            return entry, message

        # on：周期状态机裁决，事件 + 三处回写在一个事务里。
        try:
            entry, message = repo.run_command(
                ledger=self.module,
                entry_id=entry_id,
                action=action,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # 领域闸口拒绝：转成旧接口的 (None, 可读原因) 形态
            return None, str(exc)
        if entry is None:
            return None, message
        return self._merged_view(entry), message

    # ----- on 模式视图合并 -------------------------------------------------

    def _merged_view(self, row: dict[str, Any]) -> dict[str, Any]:
        """存储事实 + 统一投影 -> 对外行（纯读，不改存储；投影未落库也能读到）。"""
        merged = dict(row)
        cycle = repo.find_cycle_for_row(self.module, row)
        if cycle is None:
            return merged
        facts = repo.load_fact_rows(cycle)
        assessment = assess_cycle(cycle, facts)
        if self.module not in assessment.verdicts:
            return merged
        fields = projection_fields(
            self.module,
            assessment,
            synced_at=str(row.get("cycle_synced_at") or ""),
        )
        merged.update(fields)
        return merged

    def _shadow_record(self, entry_id: int) -> None:
        row = store.find(self.module, entry_id)
        if row is None:
            return
        drift = MigrationPlanner(store).shadow_check(self.module, row)
        if drift is not None:
            repo.record_drift(drift)


# 三个模块的门面单例（router 侧替换服务实现，接口签名不变）。
def service_for(module: str) -> UnifiedLedgerService:
    return UnifiedLedgerService(module)
