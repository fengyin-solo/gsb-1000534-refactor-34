"""化验数据（化验待办）业务规则。

重构后状态/可用性判断统一由“样本周期”领域能力裁决并回写本台账，
本服务只保留与重构前一致的接口形态，内部委托领域门面（支持灰度切换）。
"""
from __future__ import annotations

from typing import Any

from app.domain.sample_cycle.service import service_for

MODULE = "assay"
REQUIRED_FIELDS = ["化验编号", "样品编号", "元素名称"]
STATUS_ORDER = ["待录入", "已录入", "已审核", "已退回"]
ACTION_RULES = {"录入结果": "已录入", "审核通过": "已审核", "退回修改": "已退回"}
NEGATIVE_ACTIONS = ["退回修改"]

_unified = service_for(MODULE)


class AssayService:
    """化验待办服务：样本周期门面的薄封装（接口形态与重构前一致）。"""

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        return _unified.list_entries(keyword=keyword, status=status, page=page, size=size)

    def all_entries(self) -> list[dict[str, Any]]:
        return _unified.all_entries()

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return _unified.get_entry(entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        return _unified.create_entry(values)

    def run_action(
        self, entry_id: int, action: str, idempotency_key: str | None = None
    ) -> tuple[dict[str, Any] | None, str]:
        return _unified.run_action(entry_id, action, idempotency_key=idempotency_key)
