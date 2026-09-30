"""岩心管理接口：维护岩心样本，覆盖地质编录、送样分析、归还原箱等动作。"""
from __future__ import annotations

from typing import Any

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.core import CoreService

router = APIRouter(prefix="/api/core", tags=["岩心管理"])

service = CoreService()

LIST_FIELDS = ["岩心编号", "所属钻孔", "取样深度起", "取样深度止", "岩性描述", "采取率", "存放位置", "样本状态"]
STATUSES = ["待编录", "已编录", "送样中", "已归还"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按岩心编号检索"),
    status: str | None = Query(default=None, description="待编录、已编录、送样中、已归还"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按岩心编号与状态过滤岩心管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条岩心样本明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"岩心样本 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条岩心样本，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="岩心样本已登记", entry=entry)


class _ActionPayload(BaseModel):
    """动作载荷：兼容前端两种历史形态——{"action": "..."} 与 {"values": {...}}。"""

    action: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: _ActionPayload) -> ActionResult:
    """对单条岩心样本执行地质编录、送样分析、归还原箱；不允许的动作会被拦下并说明原因。"""
    action = (payload.action or str(payload.values.get("action") or "")).strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出岩心管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "core", "total": total, "items": items}
