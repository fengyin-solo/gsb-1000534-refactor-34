"""样本周期管理接口：灰度开关、确定性回填/回滚批次、双读差异、周期只读查询。

这些都是新增接口，不改动既有 18 个业务模块的路径与出入参；
旧接口（/api/core、/api/sample_registry、/api/assay）形态保持不变。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.domain.sample_cycle.cycle_store import CommandRejected
from app.domain.sample_cycle.projection import assess_cycle
from app.domain.sample_cycle.service import (
    VALID_MODES,
    feature,
    migrator,
    repo,
)
from app.schemas import ActionResult, EntryPayload

router = APIRouter(prefix="/api/sample-cycles", tags=["样本周期"])


@router.get("/mode")
def get_mode() -> dict[str, object]:
    """读取当前灰度模式：off（旧路径）/ shadow（双读留痕）/ on（周期主导）。"""
    return {"mode": feature.mode, "valid_modes": list(VALID_MODES)}


@router.post("/mode", response_model=ActionResult)
def set_mode(payload: EntryPayload) -> ActionResult:
    """运行时切换灰度模式，新旧路径转换可随时灰度、随时回退。"""
    mode = str(payload.values.get("mode") or "").strip()
    try:
        current = feature.set_mode(mode)
    except ValueError as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"样本周期灰度模式已切换为 {current}")


@router.get("")
def list_cycles() -> dict[str, object]:
    """周期只读视图：当前阶段、责任组、三处台账的统一可用性裁决。"""
    items = []
    for cycle in repo.list_cycles():
        facts = repo.load_fact_rows(cycle)
        assessment = assess_cycle(cycle, facts)
        items.append(
            {
                "sample_key": cycle.sample_key,
                "current_stage": assessment.stage,
                "current_stage_label": assessment.stage_label,
                "owner_group": assessment.owner_group,
                "source_rows": dict(cycle.source_rows),
                "borehole_ready": assessment.borehole_ready,
                "depth_ready": assessment.depth_ready,
                "delivery_ready": assessment.delivery_ready,
                "assay_ready": assessment.assay_ready,
                "verdicts": {
                    ledger: verdict.to_dict()
                    for ledger, verdict in assessment.verdicts.items()
                },
                "events": [event.to_dict() for event in cycle.events],
            }
        )
    return {"total": len(items), "items": items}


@router.get("/batches")
def list_batches() -> dict[str, object]:
    batches = repo.list_batches()
    return {"total": len(batches), "items": batches}


@router.post("/backfill", response_model=ActionResult)
def backfill(payload: EntryPayload | None = None) -> ActionResult:
    """执行确定性回填；project=true 时同时把统一裁决回写三处台账。

    重跑同一批数据：批次 ID 相同、事件相同、投影相同（幂等），直接返回已有批次。
    """
    project = bool((payload.values if payload else {}).get("project"))
    batch = migrator.run(project=project)
    rerun = batch.pop("rerun", False)
    message = "回填批次已存在，重跑结果一致" if rerun else "样本周期回填完成"
    return ActionResult(ok=True, message=message, entry=batch)


@router.post("/batches/{batch_id}/project", response_model=ActionResult)
def project_batch(batch_id: str) -> ActionResult:
    """把回填批次的统一裁决回写三处台账（切流前/切流中可反复执行，幂等）。"""
    try:
        batch = migrator.project_batch(batch_id)
    except CommandRejected as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ActionResult(ok=True, message=f"批次 {batch_id} 已回写三处台账", entry=batch)


@router.post("/batches/{batch_id}/rollback", response_model=ActionResult)
def rollback_batch(batch_id: str) -> ActionResult:
    """回滚回填批次：摘除投影、删除其周期事件；已有在线业务事件时拒绝。"""
    try:
        result = migrator.rollback(batch_id)
    except CommandRejected as exc:
        return ActionResult(ok=False, message=str(exc))
    return ActionResult(ok=True, message=f"批次 {batch_id} 已回滚，旧台账恢复原口径", entry=result)


@router.get("/drifts")
def list_drifts(batch_id: str | None = None) -> dict[str, object]:
    """回填期间双读校验差异：旧口径与统一裁决不一致的逐条留痕。"""
    drifts = repo.list_drifts(batch_id=batch_id)
    return {"total": len(drifts), "items": drifts}
