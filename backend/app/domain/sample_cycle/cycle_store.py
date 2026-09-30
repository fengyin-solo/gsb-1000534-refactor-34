"""样本周期仓储：周期聚合、回填批次、双读差异的存取与在线命令处理。

事务边界：所有“一次动作改多处”的方法都包在 ``store.transaction()`` 里，
事件追加与三处台账投影回写要么全部生效，要么全部不生效，调用方不会看到
“转换未成功但部分写入”的中间态。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from app.domain.sample_cycle.events import (
    EVENT_KIND_COMMAND,
    EVENT_KIND_RECOVERY,
    CycleEvent,
    SampleCycle,
    command_event_id,
    legacy_event_id,
)
from app.domain.sample_cycle.projection import (
    assess_cycle,
    projection_fields,
    sample_key_of,
)
from app.domain.sample_cycle.stages import (
    CYCLE_LEDGERS,
    LEDGER_ASSAY,
    LEDGER_CORE,
    LEDGER_SAMPLE_REGISTRY,
    STAGE_BY_LEDGER_STATUS,
    STAGE_DEPTH_RECHECKED,
    STAGE_LOGGED,
    STAGE_ORDER,
    STAGE_SAMPLE_RECEIVED,
    STAGE_TO_SAMPLE,
    COMMANDS_BY_LEDGER_ACTION,
    depth_recheck_ready,
)
from app.store import TABLE_CYCLES, TABLE_CYCLE_BATCHES, TABLE_CYCLE_DRIFTS, Store

# 台账 -> （模块名、行的称呼、动作主语），用于保持重构前的中文报错口径。
LEDGER_LABELS: Mapping[str, str] = {
    LEDGER_CORE: "岩心管理",
    LEDGER_SAMPLE_REGISTRY: "样品登记",
    LEDGER_ASSAY: "化验数据",
}
LEDGER_ROW_NOUNS: Mapping[str, str] = {
    LEDGER_CORE: "岩心样本",
    LEDGER_SAMPLE_REGISTRY: "送检样品",
    LEDGER_ASSAY: "化验结果",
}
LEDGER_ACTION_SUBJECT: Mapping[str, str] = {
    LEDGER_CORE: "岩心样本已",
    LEDGER_SAMPLE_REGISTRY: "送检样品已",
    LEDGER_ASSAY: "化验结果已",
}

# 负向旧状态：迁移与引导时按原裁决标记为负向事件。
NEGATIVE_STATUS: Mapping[str, frozenset[str]] = {
    LEDGER_CORE: frozenset(),
    LEDGER_SAMPLE_REGISTRY: frozenset(),
    LEDGER_ASSAY: frozenset({"已退回"}),
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def missing_message(ledger: str, entry_id: int) -> str:
    return f"{LEDGER_ROW_NOUNS[ledger]} {entry_id} 不存在或已归档"


def action_scope_message(ledger: str, action: str) -> str:
    return f"动作「{action}」不属于{LEDGER_LABELS[ledger]}可执行范围"


def action_ok_message(ledger: str, action: str) -> str:
    return f"{LEDGER_ACTION_SUBJECT[ledger]}{action}"


class CommandRejected(Exception):
    """命令被周期状态机拒绝。"""


class CycleRepository:
    def __init__(self, data_store: Store) -> None:
        self.store = data_store

    # ----- 内部表 ---------------------------------------------------------

    def _table(self, name: str) -> list[dict[str, Any]]:
        return self.store.rows(name)

    # ----- 周期聚合 -------------------------------------------------------

    def list_cycles(self) -> list[SampleCycle]:
        return [SampleCycle.from_snapshot(row) for row in self._table(TABLE_CYCLES)]

    def get_cycle(self, sample_key: str) -> SampleCycle | None:
        for row in self._table(TABLE_CYCLES):
            if row.get("sample_key") == sample_key:
                return SampleCycle.from_snapshot(row)
        return None

    def save_cycle(self, cycle: SampleCycle) -> None:
        rows = self._table(TABLE_CYCLES)
        for index, row in enumerate(rows):
            if row.get("sample_key") == cycle.sample_key:
                rows[index] = cycle.to_snapshot()
                return
        rows.append(cycle.to_snapshot())

    def delete_cycle(self, sample_key: str) -> None:
        self._table(TABLE_CYCLES)[:] = [
            row for row in self._table(TABLE_CYCLES) if row.get("sample_key") != sample_key
        ]

    # ----- 批次 -----------------------------------------------------------

    def find_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in self._table(TABLE_CYCLE_BATCHES):
            if row.get("batch_id") == batch_id:
                return row
        return None

    def list_batches(self) -> list[dict[str, Any]]:
        return list(self._table(TABLE_CYCLE_BATCHES))

    def save_batch(self, batch: Mapping[str, Any]) -> None:
        rows = self._table(TABLE_CYCLE_BATCHES)
        for index, row in enumerate(rows):
            if row.get("batch_id") == batch["batch_id"]:
                rows[index] = dict(batch)
                return
        rows.append(dict(batch))

    def delete_batch(self, batch_id: str) -> None:
        self._table(TABLE_CYCLE_BATCHES)[:] = [
            row for row in self._table(TABLE_CYCLE_BATCHES) if row.get("batch_id") != batch_id
        ]

    # ----- 双读差异 -------------------------------------------------------

    def record_drift(self, drift: Mapping[str, Any]) -> bool:
        """登记一条双读差异；同内容键已存在则幂等跳过，返回是否新增。"""
        key = drift["drift_key"]
        rows = self._table(TABLE_CYCLE_DRIFTS)
        if any(row.get("drift_key") == key for row in rows):
            return False
        rows.append(dict(drift))
        return True

    def list_drifts(self, *, batch_id: str | None = None) -> list[dict[str, Any]]:
        rows = list(self._table(TABLE_CYCLE_DRIFTS))
        if batch_id:
            rows = [row for row in rows if row.get("batch_id") == batch_id]
        return rows

    # ----- 事实行装载 -----------------------------------------------------

    def load_fact_rows(self, cycle: SampleCycle) -> dict[str, dict[str, Any] | None]:
        """按来源坐标装载三处台账事实行（行被删除时为 None，不报错）。"""
        facts: dict[str, dict[str, Any] | None] = {}
        for ledger in CYCLE_LEDGERS:
            row_id = cycle.source_rows.get(ledger)
            facts[ledger] = self.store.find(ledger, row_id) if row_id is not None else None
        return facts

    def find_cycle_for_row(self, ledger: str, row: Mapping[str, Any]) -> SampleCycle | None:
        sample_key = sample_key_of(ledger, row)
        return self.get_cycle(sample_key) if sample_key is not None else None

    # ----- 投影回写 -------------------------------------------------------

    def materialize_due_rows(self, cycle: SampleCycle, *, synced_at: str) -> list[str]:
        """按当前阶段物化“应存在但尚未登记”的下游台账行（送样清单、化验待办）。

        这是“判断结果回写送样清单和化验待办”的一部分：责任组阶段一旦到达，
        对应台账必须能看到该样本。岩心根行不自动创建（样本必须先在岩心台账登记）。
        物化是幂等的：已有行（含游离挂接）不重复创建。
        """
        stage = cycle.current_stage()
        created: list[str] = []

        def _next_id(ledger: str) -> int:
            return max((int(row.get("id", 0)) for row in self.store.rows(ledger)), default=0) + 1

        # 送样清单：TO_SAMPLE 及之后必须有行。
        if STAGE_ORDER[stage] >= STAGE_ORDER[STAGE_TO_SAMPLE]:
            existing_id = cycle.source_rows.get(LEDGER_SAMPLE_REGISTRY)
            row = self.store.find(LEDGER_SAMPLE_REGISTRY, existing_id) if existing_id else None
            if row is None:
                row = {
                    "id": _next_id(LEDGER_SAMPLE_REGISTRY),
                    "送检编号": f"SAMP-AUTO-{cycle.sample_key}",
                    "样品名称": cycle.sample_key,
                    "采样位置": "送样分析自动生成",
                    "岩心编号": cycle.sample_key,
                }
                self.store.rows(LEDGER_SAMPLE_REGISTRY).append(row)
                cycle.source_rows[LEDGER_SAMPLE_REGISTRY] = int(row["id"])
                created.append(LEDGER_SAMPLE_REGISTRY)

        # 化验待办：SAMPLE_RECEIVED 及之后必须有行。
        if STAGE_ORDER[stage] >= STAGE_ORDER[STAGE_SAMPLE_RECEIVED]:
            existing_id = cycle.source_rows.get(LEDGER_ASSAY)
            row = self.store.find(LEDGER_ASSAY, existing_id) if existing_id else None
            if row is None:
                row = {
                    "id": _next_id(LEDGER_ASSAY),
                    "化验编号": f"ASSA-AUTO-{cycle.sample_key}",
                    "样品编号": cycle.sample_key,
                    "元素名称": "待检测",
                    "岩心编号": cycle.sample_key,
                }
                self.store.rows(LEDGER_ASSAY).append(row)
                cycle.source_rows[LEDGER_ASSAY] = int(row["id"])
                created.append(LEDGER_ASSAY)
        return created

    def project_cycle(
        self,
        cycle: SampleCycle,
        *,
        synced_at: str | None = None,
        ledgers: Iterable[str] = CYCLE_LEDGERS,
        materialize: bool = False,
    ) -> dict[str, Any]:
        """把统一裁决回写到周期涉及的三处台账行（必须在事务内调用）。

        materialize=True 时先按阶段物化应存在的下游行（在线命令路径）；
        回填投影默认不造行，只在既有事实上回写。
        """
        synced_at = synced_at or utc_now_iso()
        if materialize:
            self.materialize_due_rows(cycle, synced_at=synced_at)
        facts = self.load_fact_rows(cycle)
        assessment = assess_cycle(cycle, facts)
        touched: dict[str, int] = {}
        for ledger in ledgers:
            fact = facts.get(ledger)
            if fact is None:
                continue
            fact.update(projection_fields(ledger, assessment, synced_at=synced_at))
            touched[ledger] = int(fact["id"])
        return {"sample_key": cycle.sample_key, "stage": assessment.stage, "touched": touched}

    # ----- 在线命令 -------------------------------------------------------

    def run_command(
        self,
        *,
        ledger: str,
        entry_id: int,
        action: str,
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """在周期状态机上执行一条旧动作；事件与三处回写在同一事务内完成。"""
        row = self.store.find(ledger, entry_id)
        if row is None:
            return None, missing_message(ledger, entry_id)
        spec = COMMANDS_BY_LEDGER_ACTION.get((ledger, action))
        if spec is None:
            return None, action_scope_message(ledger, action)
        ok_message = action_ok_message(ledger, action)

        with self.store.transaction():
            cycle = self.find_cycle_for_row(ledger, row)
            recovered = cycle is None
            if recovered:
                cycle = self.bootstrap_from_row(ledger, row)
            current = cycle.current_stage()

            target_index = STAGE_ORDER[spec.to_stage]
            if not spec.negative and target_index < STAGE_ORDER[current]:
                # 与旧实现同口径：目标状态不在允许的（向前）序列里。
                from app.domain.sample_cycle.stages import STAGE_LABELS

                return None, f"目标状态「{STAGE_LABELS[spec.to_stage]}」不在允许的状态序列里"
            if target_index == STAGE_ORDER[current] and not spec.negative:
                # 幂等重放：阶段未变化，不新增事件，直接回读（仍补齐应物化的行）。
                self.project_cycle(cycle, materialize=True)
                self.save_cycle(cycle)
                return self.store.find(ledger, entry_id), ok_message

            # 送样闸口：进入送样阶段前，取样深度复核必须通过。
            if spec.to_stage == STAGE_TO_SAMPLE and STAGE_ORDER[current] < STAGE_ORDER[STAGE_TO_SAMPLE]:
                core_row = (
                    row if ledger == LEDGER_CORE
                    else self.store.find(LEDGER_CORE, cycle.source_rows.get(LEDGER_CORE, -1))
                )
                if core_row is None or not depth_recheck_ready(core_row):
                    raise CommandRejected("取样深度复核未通过，不得送样")

            occurred_at = utc_now_iso()
            seq = self._next_seq_locked()
            main_event = CycleEvent(
                id=command_event_id(
                    cycle.sample_key, action, current, spec.to_stage, idempotency_key
                ),
                sample_key=cycle.sample_key,
                kind=EVENT_KIND_RECOVERY if recovered else EVENT_KIND_COMMAND,
                action=action,
                from_stage=current,
                to_stage=spec.to_stage,
                negative=spec.negative,
                ledger=ledger,
                batch_id=None,
                seq=seq,
                occurred_at=occurred_at,
                idempotency_key=idempotency_key,
                payload={"entry_id": entry_id},
            )
            if cycle.has_event(main_event.id):
                self.project_cycle(cycle, materialize=True)
                self.save_cycle(cycle)
                return self.store.find(ledger, entry_id), ok_message
            cycle.append(main_event)

            # “送样分析”隐含确认“取样深度复核通过”，以独立领域事件留痕，
            # 不新增接口动作，既有接口形态保持不变。
            if spec.to_stage == STAGE_TO_SAMPLE and STAGE_ORDER[current] < STAGE_ORDER[STAGE_TO_SAMPLE]:
                cycle.append(
                    CycleEvent(
                        id=command_event_id(
                            cycle.sample_key, "取样深度复核",
                            STAGE_LOGGED, STAGE_DEPTH_RECHECKED, idempotency_key,
                        ),
                        sample_key=cycle.sample_key,
                        kind=EVENT_KIND_COMMAND,
                        action="取样深度复核",
                        from_stage=STAGE_LOGGED,
                        to_stage=STAGE_DEPTH_RECHECKED,
                        negative=False,
                        ledger=LEDGER_CORE,
                        batch_id=None,
                        seq=seq,
                        occurred_at=occurred_at,
                        idempotency_key=idempotency_key,
                        payload={"entry_id": entry_id, "implicit": True},
                    )
                )

            self.project_cycle(cycle, synced_at=occurred_at, materialize=True)
            self.save_cycle(cycle)
            result = self.store.find(ledger, entry_id)
        return result, ok_message

    # ----- 引导与事件构造（迁移复用）-------------------------------------

    def bootstrap_from_row(self, ledger: str, row: Mapping[str, Any]) -> SampleCycle:
        """在线写入时样本尚未回填：以该行既有确认阶段为初始基准就地补齐。"""
        sample_key = sample_key_of(ledger, row) or self.synthetic_key(ledger, int(row["id"]))
        cycle = self.get_cycle(sample_key) or SampleCycle(sample_key)
        cycle.source_rows.setdefault(ledger, int(row["id"]))
        status = str(row.get("status") or "")
        stage = STAGE_BY_LEDGER_STATUS[ledger].get(status)
        if stage is not None:
            cycle.append(
                CycleEvent(
                    id=legacy_event_id(sample_key, ledger, int(row["id"]), status),
                    sample_key=sample_key,
                    kind=EVENT_KIND_RECOVERY,
                    action="历史裁决",
                    from_stage=stage,
                    to_stage=stage,
                    negative=status in NEGATIVE_STATUS[ledger],
                    ledger=ledger,
                    batch_id=None,
                    seq=self._next_seq_locked(),
                    occurred_at=utc_now_iso(),
                    payload={"source_status": status, "recovery": True},
                )
            )
        return cycle

    def reserve_seq(self) -> int:
        """下一个全局单调事件序号（同事务内连续调用递增）。"""
        return self._next_seq_locked()

    def _next_seq_locked(self) -> int:
        highest = 0
        for snapshot in self._table(TABLE_CYCLES):
            for event in snapshot.get("events", []):
                highest = max(highest, int(event.get("seq", 0)))
        return highest + 1

    @staticmethod
    def synthetic_key(ledger: str, entry_id: int) -> str:
        return f"{ledger}#{entry_id}"
