"""样品登记接口：维护送检样品，覆盖确认收样、登记报告、退回样品等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.sample_registry import SampleRegistryService

router = APIRouter(prefix="/api/sample_registry", tags=["样品登记"])

service = SampleRegistryService()

LIST_FIELDS = ["送检编号", "样品名称", "采样位置", "检测项目", "送检单位", "收样日期", "检测周期", "送检状态"]
STATUSES = ["待收样", "已收样", "检测中", "已出报告"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按送检编号检索"),
    status: str | None = Query(default=None, description="待收样、已收样、检测中、已出报告"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按送检编号与状态过滤样品登记列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出样品登记清单：返回当前过滤条件下的全量数据。"""
    items = service.all_entries()
    return {"module": "sample_registry", "total": len(items), "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条送检样品明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"送检样品 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条送检样品，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="送检样品已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条送检样品执行确认收样、登记报告、退回样品；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    idempotency_key = payload.values.get("idempotency_key")
    entry, message = service.run_action(
        entry_id, action, idempotency_key=str(idempotency_key) if idempotency_key else None
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
