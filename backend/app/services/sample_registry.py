"""样品登记（送样清单）业务规则。

重构后状态/可用性判断统一由“样本周期”领域能力裁决并回写本台账，
本服务只保留与重构前一致的接口形态，内部委托领域门面（支持灰度切换）。
"""
from __future__ import annotations

from typing import Any

from app.domain.sample_cycle.service import service_for

MODULE = "sample_registry"
REQUIRED_FIELDS = ["送检编号", "样品名称", "采样位置"]
STATUS_ORDER = ["待收样", "已收样", "检测中", "已出报告"]
ACTION_RULES = {"确认收样": "已收样", "登记报告": "已出报告", "退回样品": "待收样"}
NEGATIVE_ACTIONS = ["退回样品"]

_unified = service_for(MODULE)


class SampleRegistryService:
    """送样清单服务：样本周期门面的薄封装（接口形态与重构前一致）。"""

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
