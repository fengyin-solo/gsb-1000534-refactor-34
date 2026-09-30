"""化验数据接口：维护化验结果，覆盖录入结果、审核通过、退回修改等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.assay import AssayService

router = APIRouter(prefix="/api/assay", tags=["化验数据"])

service = AssayService()

LIST_FIELDS = ["化验编号", "样品编号", "元素名称", "化验值", "单位", "化验方法", "化验日期", "结果状态"]
STATUSES = ["待录入", "已录入", "已审核", "已退回"]


class _ActionPayload(BaseModel):
    """动作载荷：兼容前端两种历史形态——{"action": "..."} 与 {"values": {...}}。"""

    action: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按化验编号检索"),
    status: str | None = Query(default=None, description="待录入、已录入、已审核、已退回"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按化验编号与状态过滤化验数据列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条化验结果明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"化验结果 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条化验结果，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="化验结果已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: _ActionPayload) -> ActionResult:
    """对单条化验结果执行录入结果、审核通过、退回修改；不允许的动作会被拦下并说明原因。"""
    action = (payload.action or str(payload.values.get("action") or "")).strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出化验数据清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "assay", "total": total, "items": items}
