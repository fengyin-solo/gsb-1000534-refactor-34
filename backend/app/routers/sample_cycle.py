"""样本周期领域接口：统一可用性查询、命令、灰度模式与迁移回填。

既有三台账接口（/api/core、/api/sample_registry、/api/assay）保持不变，
本路由只暴露领域自身能力，供运维灰度、回填、双读校验与前端读取统一结论。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.sample_cycle.policy import PolicyViolation
from app.sample_cycle.service import (
    DEFAULT_BATCH_ID,
    MODES,
    sample_cycle_service,
)

router = APIRouter(prefix="/api/sample-cycles", tags=["样本周期"])


class CommandPayload(BaseModel):
    event_type: str
    ledger: str = "core"
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class ModePayload(BaseModel):
    mode: str
    actor: str = "system"


class BackfillPayload(BaseModel):
    batch_id: str = DEFAULT_BATCH_ID
    strict: bool = True
    actor: str = "migration"


@router.get("/mode")
def get_mode() -> dict[str, Any]:
    """读取当前灰度模式（legacy / shadow / dual）。"""
    return {"mode": sample_cycle_service.get_mode(), "history": sample_cycle_service.mode_history()}


@router.put("/mode")
def set_mode(payload: ModePayload) -> dict[str, Any]:
    """切换灰度模式；切换动作留痕，可据此回滚。"""
    if payload.mode not in MODES:
        raise HTTPException(status_code=400, detail=f"模式仅支持：{', '.join(MODES)}")
    return sample_cycle_service.set_mode(payload.mode, actor=payload.actor)


@router.get("")
def list_cycles() -> dict[str, Any]:
    """样本周期列表，含统一阶段与三台账可用性结论。"""
    cycles = sample_cycle_service.list_cycles()
    items = []
    for cycle in cycles:
        availability = sample_cycle_service.availability(int(cycle["id"]))
        items.append({**cycle, "availability": availability})
    return {"items": items, "total": len(items)}


@router.get("/{cycle_id}")
def get_cycle(cycle_id: int) -> dict[str, Any]:
    cycle = sample_cycle_service.get_cycle(cycle_id)
    if cycle is None:
        raise HTTPException(status_code=404, detail=f"样本周期 {cycle_id} 不存在")
    return {
        "cycle": cycle,
        "availability": sample_cycle_service.availability(cycle_id),
        "events": sample_cycle_service.events(cycle_id),
    }


@router.post("/{cycle_id}/commands")
def run_command(cycle_id: int, payload: CommandPayload) -> dict[str, Any]:
    """直接对周期下达领域命令（幂等）；三台账回写在同一事务内完成。"""
    try:
        result = sample_cycle_service.apply_command(
            cycle_id,
            payload.event_type,
            ledger=payload.ledger,
            payload=payload.payload,
            idempotency_key=payload.idempotency_key,
        )
    except PolicyViolation as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, **result}


@router.get("/backfill/preview")
def preview_backfill(batch_id: str = DEFAULT_BATCH_ID) -> dict[str, Any]:
    """回填预检：只推导、不落库，列出矛盾样本与关联不上的台账行。"""
    return sample_cycle_service.preview_backfill(batch_id)


@router.post("/backfill/run")
def run_backfill(payload: BackfillPayload) -> dict[str, Any]:
    """执行迁移回填：一个批次一个事务，失败整批回滚；批次可幂等重跑。"""
    result = sample_cycle_service.run_backfill(
        payload.batch_id, strict=payload.strict, actor=payload.actor
    )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.post("/backfill/rollback")
def rollback_backfill(payload: BackfillPayload) -> dict[str, Any]:
    """按批次回滚迁移；批次之后已有业务推进时拒绝删除历史。"""
    result = sample_cycle_service.rollback_backfill(payload.batch_id, actor=payload.actor)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@router.get("/reconcile/report")
def reconcile_report() -> dict[str, Any]:
    """双读校验报告：领域结论 vs 三台账当前裁决，不一致逐条列出。"""
    return sample_cycle_service.reconcile(persist=True)
