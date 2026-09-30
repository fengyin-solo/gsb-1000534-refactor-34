"""统一可用性裁决与三处台账回写投影。

重构前三处各自维护 ``status/pending/abnormal`` 与可用性口径，同一样本可能得到
互相矛盾的结论。这里把判断收敛为一个纯函数 ``assess_cycle``：
输入只有“周期事件流 + 三处台账事实行”，输出在任何进程、任何一次重跑中都相同。

裁决结果按台账投影回写：
- 既有键 ``status/pending/abnormal``：机械投影成旧状态语义（旧接口/旧页面不变）；
- 新增 ``cycle_*`` 键：统一阶段、责任组、可用性与阻断原因（只增不改，便于回滚）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from app.domain.sample_cycle.events import EVENT_KIND_LEGACY, CycleEvent, SampleCycle
from app.domain.sample_cycle.stages import (
    LEDGER_ASSAY,
    LEDGER_CORE,
    LEDGER_SAMPLE_REGISTRY,
    LEDGER_REF_FIELDS,
    LEDGER_STATUS_BY_STAGE,
    LEGACY_STATUS_ORDER,
    STAGE_ASSAY_APPROVED,
    STAGE_ASSAY_ENTERED,
    STAGE_LABELS,
    STAGE_LOGGED,
    STAGE_ORDER,
    STAGE_OWNERS,
    STAGE_RETURNED,
    STAGE_SAMPLE_RECEIVED,
    STAGE_TO_SAMPLE,
    depth_recheck_ready,
)

# 回写到台账行的统一键名（cycle_ 前缀，避免与既有中文业务列冲突）。
PROJ_SAMPLE_KEY = "cycle_sample_key"
PROJ_STAGE = "cycle_stage"
PROJ_STAGE_LABEL = "cycle_stage_label"
PROJ_OWNER = "cycle_owner_group"
PROJ_AVAILABLE = "cycle_available"
PROJ_REASONS = "cycle_unavailable_reasons"
PROJ_BOREDHOLE_READY = "cycle_borehole_ready"
PROJ_DEPTH_READY = "cycle_depth_ready"
PROJ_DELIVERY_READY = "cycle_delivery_ready"
PROJ_ASSAY_READY = "cycle_assay_ready"
PROJ_SYNCED_AT = "cycle_synced_at"

# 迁移/投影时可能写入既有行的全部键，回滚时据此摘除或还原。
PROJECTION_KEYS: tuple[str, ...] = (
    PROJ_SAMPLE_KEY,
    PROJ_STAGE,
    PROJ_STAGE_LABEL,
    PROJ_OWNER,
    PROJ_AVAILABLE,
    PROJ_REASONS,
    PROJ_BOREDHOLE_READY,
    PROJ_DEPTH_READY,
    PROJ_DELIVERY_READY,
    PROJ_ASSAY_READY,
    "status",
    "pending",
    "abnormal",
    PROJ_SYNCED_AT,
)
ADDITIVE_KEYS: tuple[str, ...] = tuple(k for k in PROJECTION_KEYS if k not in ("status", "pending", "abnormal"))


def sample_key_of(ledger: str, row: Mapping[str, Any]) -> str | None:
    """读取台账行上的样本引用（岩心编号）；缺失则该行暂不参与周期联动。"""
    value = str(row.get(LEDGER_REF_FIELDS[ledger]) or "").strip()
    return value or None


@dataclass
class LedgerVerdict:
    """一处台账对同一样本的统一结论。"""

    ledger: str
    present: bool                       # 该责任组是否已有行
    status: str | None                  # 投影出的旧状态（没有行为 None）
    pending: bool
    abnormal: bool
    available: bool                     # 该台账视角下样本是否可用/可推进
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger": self.ledger,
            "present": self.present,
            "status": self.status,
            "pending": self.pending,
            "abnormal": self.abnormal,
            "available": self.available,
            "reasons": list(self.reasons),
        }


@dataclass
class CycleAssessment:
    """同一样本在统一周期下的裁决结果，三处台账共用这一份结论。"""

    sample_key: str
    stage: str
    stage_label: str
    owner_group: str
    borehole_ready: bool
    depth_ready: bool
    delivery_ready: bool
    assay_ready: bool
    verdicts: dict[str, LedgerVerdict]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_key": self.sample_key,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "owner_group": self.owner_group,
            "borehole_ready": self.borehole_ready,
            "depth_ready": self.depth_ready,
            "delivery_ready": self.delivery_ready,
            "assay_ready": self.assay_ready,
            "verdicts": {ledger: verdict.to_dict() for ledger, verdict in self.verdicts.items()},
        }


def _last_directed_event(cycle: SampleCycle, ledger: str) -> CycleEvent | None:
    """该台账上最近一次定向裁决（含负向），用于 abnormal 与“已退回”投影。"""
    directed = [event for event in cycle.events if event.ledger == ledger and event.kind != EVENT_KIND_LEGACY]
    if directed:
        return sorted(directed, key=lambda e: (e.seq, e.id))[-1]
    # 迁移态：退回类历史状态本身就是一次负向裁决。
    legacy = [event for event in cycle.events if event.ledger == ledger and event.negative]
    return sorted(legacy, key=lambda e: (e.seq, e.id))[-1] if legacy else None


def _projected_status(ledger: str, stage: str, cycle: SampleCycle) -> str | None:
    status = LEDGER_STATUS_BY_STAGE[ledger][stage]
    if status is None:
        return None
    # 化验待办：SAMPLE_RECEIVED 区分“待录入”与“退回修改后的已退回”。
    if ledger == LEDGER_ASSAY and stage == STAGE_SAMPLE_RECEIVED:
        directed = _last_directed_event(cycle, ledger)
        if directed is not None and directed.negative:
            return "已退回"
    return status


def _effective_status(ledger: str, fact: Mapping[str, Any] | None) -> str | None:
    """行先于阶段存在（如回退后）时，沿用行自身既有状态，不造空值。"""
    if fact is None:
        return None
    return str(fact.get("status") or LEGACY_STATUS_ORDER[ledger][0])


def _stage_ge(stage: str, other: str) -> bool:
    return STAGE_ORDER[stage] >= STAGE_ORDER[other]


def assess_cycle(
    cycle: SampleCycle,
    ledger_rows: Mapping[str, Mapping[str, Any] | None],
) -> CycleAssessment:
    """对单个样本周期做出唯一一份可用性裁决（纯函数）。

    ledger_rows: {ledger: 该样本在此台账的事实行（可能为 None）}
    """
    stage = cycle.current_stage()
    core_row = ledger_rows.get(LEDGER_CORE)

    # —— 三个闸口判断，全部来自事实，顺序固定 ——
    borehole_ready = bool(
        core_row is not None and str(core_row.get("所属钻孔") or "").strip()
    )
    depth_ready = borehole_ready and core_row is not None and depth_recheck_ready(core_row)
    # 送样闸口：编录已完成、深度复核通过、周期尚未进入送样之后。
    delivery_ready = stage == STAGE_LOGGED and depth_ready
    # 化验待办闸口：已收样或录入被退回（重新可办）。
    assay_ready = stage in (STAGE_SAMPLE_RECEIVED, STAGE_ASSAY_ENTERED)

    verdicts: dict[str, LedgerVerdict] = {}

    # 岩心台账：周期根行，登记后即存在；未归还且钻孔事实齐全视为可用。
    core_present = core_row is not None
    core_reasons: list[str] = []
    if not core_present:
        core_reasons.append("岩心台账缺少样本行")
    elif not borehole_ready:
        core_reasons.append("所属钻孔缺失，样本身份不完整")
    if stage == STAGE_RETURNED:
        core_reasons.append("样本已归还原箱，周期闭合")
    verdicts[LEDGER_CORE] = LedgerVerdict(
        ledger=LEDGER_CORE,
        present=core_present,
        status=_projected_status(LEDGER_CORE, stage, cycle),
        pending=stage != STAGE_RETURNED,
        abnormal=False,
        available=core_present and borehole_ready and stage != STAGE_RETURNED,
        reasons=core_reasons,
    )

    # 送样清单：进入 TO_SAMPLE 才有行；报告完成/归还后流转关闭。
    sample_row = ledger_rows.get(LEDGER_SAMPLE_REGISTRY)
    sample_present = sample_row is not None or _stage_ge(stage, STAGE_TO_SAMPLE)
    sample_status = _projected_status(LEDGER_SAMPLE_REGISTRY, stage, cycle)
    if sample_status is None and sample_row is not None:
        sample_status = _effective_status(LEDGER_SAMPLE_REGISTRY, sample_row)
    sample_reasons: list[str] = []
    if not sample_present:
        sample_reasons.append("样本尚未进入送样流转")
    elif not depth_ready:
        sample_reasons.append("取样深度复核未通过，不得送样")
    if sample_present and stage in (STAGE_ASSAY_APPROVED, STAGE_RETURNED):
        sample_reasons.append("化验报告已完成，送样清单关闭")
    sample_directed = _last_directed_event(cycle, LEDGER_SAMPLE_REGISTRY)
    verdicts[LEDGER_SAMPLE_REGISTRY] = LedgerVerdict(
        ledger=LEDGER_SAMPLE_REGISTRY,
        present=sample_present,
        status=sample_status,
        pending=sample_status not in (None, LEGACY_STATUS_ORDER[LEDGER_SAMPLE_REGISTRY][-1]),
        abnormal=bool(sample_directed and sample_directed.negative and stage == STAGE_TO_SAMPLE),
        available=sample_present
        and depth_ready
        and stage in (STAGE_TO_SAMPLE, STAGE_SAMPLE_RECEIVED, STAGE_ASSAY_ENTERED),
        reasons=sample_reasons,
    )

    # 化验待办：已收样后出现；审核完成关闭，退回修改重新打开。
    assay_row = ledger_rows.get(LEDGER_ASSAY)
    assay_present = assay_row is not None or _stage_ge(stage, STAGE_SAMPLE_RECEIVED)
    assay_status = _projected_status(LEDGER_ASSAY, stage, cycle)
    if assay_status is None and assay_row is not None:
        assay_status = _effective_status(LEDGER_ASSAY, assay_row)
    assay_directed = _last_directed_event(cycle, LEDGER_ASSAY)
    assay_abnormal = bool(
        assay_directed
        and assay_directed.negative
        and stage == STAGE_SAMPLE_RECEIVED
    )
    assay_reasons: list[str] = []
    if not assay_present:
        assay_reasons.append("样本尚未收样，没有化验待办")
    elif assay_abnormal:
        assay_reasons.append("化验结果被退回修改，待重新处理")
    if assay_present and stage == STAGE_ASSAY_APPROVED:
        assay_reasons.append("化验结果已审核，待办关闭")
    verdicts[LEDGER_ASSAY] = LedgerVerdict(
        ledger=LEDGER_ASSAY,
        present=assay_present,
        status=assay_status,
        pending=assay_status not in (None, "已审核"),
        abnormal=assay_abnormal,
        available=assay_present and assay_ready,
        reasons=assay_reasons,
    )

    return CycleAssessment(
        sample_key=cycle.sample_key,
        stage=stage,
        stage_label=STAGE_LABELS[stage],
        owner_group=STAGE_OWNERS[stage].value,
        borehole_ready=borehole_ready,
        depth_ready=depth_ready,
        delivery_ready=delivery_ready,
        assay_ready=assay_ready,
        verdicts=verdicts,
    )


def projection_fields(
    ledger: str,
    assessment: CycleAssessment,
    *,
    synced_at: str,
) -> dict[str, Any]:
    """把统一裁决投影成某个台账行上要回写的键值（含既有键与新增键）。"""
    verdict = assessment.verdicts[ledger]
    fields: dict[str, Any] = {
        PROJ_SAMPLE_KEY: assessment.sample_key,
        PROJ_STAGE: assessment.stage,
        PROJ_STAGE_LABEL: assessment.stage_label,
        PROJ_OWNER: assessment.owner_group,
        PROJ_AVAILABLE: verdict.available,
        PROJ_REASONS: list(verdict.reasons),
        PROJ_BOREDHOLE_READY: assessment.borehole_ready,
        PROJ_DEPTH_READY: assessment.depth_ready,
        PROJ_DELIVERY_READY: assessment.delivery_ready,
        PROJ_ASSAY_READY: assessment.assay_ready,
        PROJ_SYNCED_AT: synced_at,
    }
    if verdict.status is not None:
        fields["status"] = verdict.status
        fields["pending"] = verdict.pending
        fields["abnormal"] = verdict.abnormal
    return fields
