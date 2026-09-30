"""历史数据迁移：以既有确认阶段为初始基准，旧裁决按原裁决留痕。

迁移是确定性的纯推导过程（``plan`` 只产出计划，``service`` 负责在一个
事务里落库），关键约束：

* **锚点**：岩心台账是样本的法定主档，一条岩心行对应一个周期；
  送样清单行、化验待办行通过编号关联挂到周期上，关联不上的不臆造周期，
  只在批次报告里记 ``unlinked``。
* **留痕**：每条被接管的台账行先写一条 ``LEGACY_RULING``，其可用性
  结论按重构前的旧口径（``policy.legacy_ruling``）原样记录；
  再写一条 ``MIGRATION_BASELINE``，记录按既有确认阶段归一的基准阶段。
* **矛盾不掩盖**：三处旧确认阶段不一致时，基准取岩心台账主档阶段，
  但批次结果标记 ``divergent=True``，双读校验会持续暴露，直到人工处置。
* **全有或全无**：单周期内任何一步推导/写入失败，整批回滚，不产生
  部分写入；批次按 ``batch_id`` 幂等，重跑只处理未迁移周期，
  已迁移周期逐事件命中自然键，重跑结果（阶段、事件、回写字段）完全一致。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.sample_cycle.events import (
    LEDGER_ASSAY,
    LEDGER_CORE,
    LEDGER_DISPATCH,
    LEDGERS,
    MIGRATION_BASELINE,
    LEGACY_RULING,
)
from app.sample_cycle.policy import (
    availability_triple,
    legacy_ruling,
    ledger_legacy_stage,
)

# 各台账的编号字段
CODE_FIELDS = {
    LEDGER_CORE: "岩心编号",
    LEDGER_DISPATCH: "送检编号",
    LEDGER_ASSAY: "化验编号",
}
# 送样清单/化验行用来反向指向样品的字段（旧表里没有外键，按编号后缀关联）
SAMPLE_REF_FIELDS = {
    LEDGER_DISPATCH: "样品名称",
    LEDGER_ASSAY: "样品编号",
}

_NUMBER_SUFFIX = re.compile(r"(\d+)\s*$")


def number_suffix(code: Any) -> str | None:
    """提取编号尾部的数字串作为同一样本的关联键（CORE-0002 / SAMP-0002）。"""
    match = _NUMBER_SUFFIX.search(str(code or ""))
    return match.group(1) if match else None


@dataclass
class LedgerLink:
    """一条台账行到样本周期的关联结果。"""

    ledger: str
    row_id: int
    code: str
    legacy_status: str
    implied_stage: str | None      # 旧确认阶段反查的周期阶段
    ruling: dict[str, Any]         # 旧裁决留痕内容
    explicit: bool = False         # 是否通过显式回填字段关联（优先于编号）


@dataclass
class CycleMigrationPlan:
    """单个周期的迁移计划：纯数据，可序列化，便于预检与重跑对账。"""

    core_row_id: int
    sample_code: str
    core_code: str
    baseline_stage: str
    links: list[LedgerLink] = field(default_factory=list)
    divergent: bool = False
    divergence_detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "core_row_id": self.core_row_id,
            "sample_code": self.sample_code,
            "core_code": self.core_code,
            "baseline_stage": self.baseline_stage,
            "divergent": self.divergent,
            "divergence_detail": self.divergence_detail,
            "links": [link.__dict__ for link in self.links],
        }


@dataclass
class BatchPlan:
    batch_id: str
    cycles: list[CycleMigrationPlan] = field(default_factory=list)
    unlinked: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "cycle_count": len(self.cycles),
            "unlinked": self.unlinked,
            "cycles": [cycle.to_dict() for cycle in self.cycles],
        }


def _index_by_suffix(rows: list[dict[str, Any]], code_field: str) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        suffix = number_suffix(row.get(code_field))
        if suffix and suffix not in index:  # 同后缀首条优先，保持确定
            index[suffix] = row
    return index


def build_batch_plan(
    core_rows: list[dict[str, Any]],
    dispatch_rows: list[dict[str, Any]],
    assay_rows: list[dict[str, Any]],
    batch_id: str,
) -> BatchPlan:
    """对三台账快照做确定性推导，产出整批迁移计划（不落库、不改数据）。

    任意行缺字段/状态无法识别都不会抛异常中断整批：该周期仍可迁移，
    问题行进入 ``unlinked`` 或在 divergence_detail 中体现，由人工裁决。
    """
    dispatch_index = _index_by_suffix(dispatch_rows, CODE_FIELDS[LEDGER_DISPATCH])
    assay_index = _index_by_suffix(assay_rows, CODE_FIELDS[LEDGER_ASSAY])

    plan = BatchPlan(batch_id=batch_id)
    for core_row in sorted(core_rows, key=lambda row: int(row.get("id", 0))):
        suffix = number_suffix(core_row.get(CODE_FIELDS[LEDGER_CORE]))
        core_stage = ledger_legacy_stage(LEDGER_CORE, str(core_row.get("status", "")))
        if suffix is None or core_stage is None:
            plan.unlinked.append({
                "ledger": LEDGER_CORE,
                "row_id": core_row.get("id"),
                "reason": "岩心编号无法解析或旧状态不在确认阶段表内",
            })
            continue

        cycle = CycleMigrationPlan(
            core_row_id=int(core_row["id"]),
            sample_code=f"SAMPLE-{suffix}",
            core_code=str(core_row[CODE_FIELDS[LEDGER_CORE]]),
            baseline_stage=core_stage,
        )
        cycle.links.append(_make_link(LEDGER_CORE, core_row, CODE_FIELDS[LEDGER_CORE]))

        implied_stages = {LEDGER_CORE: core_stage}
        for ledger, index, code_field in (
            (LEDGER_DISPATCH, dispatch_index, CODE_FIELDS[LEDGER_DISPATCH]),
            (LEDGER_ASSAY, assay_index, CODE_FIELDS[LEDGER_ASSAY]),
        ):
            row = index.get(suffix)
            if row is None:
                continue
            link = _make_link(ledger, row, code_field)
            cycle.links.append(link)
            if link.implied_stage is not None:
                implied_stages[ledger] = link.implied_stage

        _annotate_divergence(cycle, implied_stages)
        plan.cycles.append(cycle)

    linked_suffixes = {
        number_suffix(row.get(CODE_FIELDS[LEDGER_CORE]))
        for row in core_rows
    }
    for ledger, rows, code_field in (
        (LEDGER_DISPATCH, dispatch_rows, CODE_FIELDS[LEDGER_DISPATCH]),
        (LEDGER_ASSAY, assay_rows, CODE_FIELDS[LEDGER_ASSAY]),
    ):
        for row in rows:
            if number_suffix(row.get(code_field)) not in linked_suffixes:
                plan.unlinked.append({
                    "ledger": ledger,
                    "row_id": row.get("id"),
                    "code": row.get(code_field),
                    "reason": "找不到对应岩心主档，未纳入任何样本周期",
                })
    return plan


def _make_link(ledger: str, row: dict[str, Any], code_field: str) -> LedgerLink:
    legacy_status = str(row.get("status", ""))
    implied = ledger_legacy_stage(ledger, legacy_status)
    return LedgerLink(
        ledger=ledger,
        row_id=int(row.get("id", 0)),
        code=str(row.get(code_field, "")),
        legacy_status=legacy_status,
        implied_stage=implied,
        ruling=legacy_ruling(ledger, legacy_status),
        explicit=bool(row.get("周期ID")),
    )


def _annotate_divergence(cycle: CycleMigrationPlan, implied: dict[str, str]) -> None:
    """三台账旧确认阶段不一致即矛盾；以岩心主档为基准但必须显式标红。"""
    baseline = implied[LEDGER_CORE]
    distinct = {ledger: stage for ledger, stage in implied.items() if stage != baseline}
    if distinct:
        cycle.divergent = True
        cycle.divergence_detail = {
            "baseline_ledger": LEDGER_CORE,
            "baseline_stage": baseline,
            "conflicting": distinct,
            "baseline_triple": availability_triple(baseline),
        }


def migration_event_specs(
    cycle_id: int,
    plan: CycleMigrationPlan,
    batch_id: str,
    *,
    stage_override: str | None = None,
) -> list[dict[str, Any]]:
    """把单周期计划展开成有序的事件内容（seq 在落库时统一分配）。

    ``stage_override`` 用于周期已存在的后续批次：留痕事件必须锚定周期的
    实际基准阶段，而不是本次重新推导的阶段（基线以第一次确认为准）。
    """
    baseline = stage_override or plan.baseline_stage
    specs: list[dict[str, Any]] = []
    for link in sorted(plan.links, key=lambda item: LEDGERS.index(item.ledger)):
        specs.append({
            "cycle_id": cycle_id,
            "event_type": LEGACY_RULING,
            "stage": link.implied_stage or baseline,
            "ledger": link.ledger,
            "batch_id": batch_id,
            "payload": link.ruling,
        })
    specs.append({
        "cycle_id": cycle_id,
        "event_type": MIGRATION_BASELINE,
        "stage": baseline,
        "ledger": LEDGER_CORE,
        "batch_id": batch_id,
        "payload": {
            "baseline_stage": baseline,
            "divergent": plan.divergent,
            "divergence_detail": plan.divergence_detail,
            "sample_code": plan.sample_code,
        },
    })
    return specs
