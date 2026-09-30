"""岩心管理业务规则。

重构后这里不再自维护状态/可用性判断，统一委托“样本周期”领域能力
（app.domain.sample_cycle）。委托门面保持原有方法签名与返回形态，
router 层与前端接口零改动；灰度 off/shadow/on 由领域门面统一切换。
"""
from __future__ import annotations

from typing import Any

from app.domain.sample_cycle.service import service_for

MODULE = "core"
REQUIRED_FIELDS = ["岩心编号", "所属钻孔", "取样深度起"]
STATUS_ORDER = ["待编录", "已编录", "送样中", "已归还"]
ACTION_RULES = {"地质编录": "已编录", "送样分析": "送样中", "归还原箱": "已归还"}
NEGATIVE_ACTIONS = []

_unified = service_for(MODULE)


class CoreService:
    """岩心台账服务：样本周期门面的薄封装（接口形态与重构前一致）。"""

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
