"""旧路径：重构前三处台账各自维护的判断逻辑，原样抽出、行为不变。

它有两个用途：
1. 灰度模式 off / shadow 下的实际读写路径（读旧写旧），保证随时可回滚；
2. 双读校验的“旧口径”基线：与统一周期裁决逐条比对，差异全部留痕。

代码刻意与重构前的 CoreService / SampleRegistryService / AssayService 保持逐行等价：
同一模块常量、同一报错文案、同一 pending/abnormal 算法。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.store import store

# 各台账旧口径常量（与重构前服务文件一致，顺序即权威顺序）。
LEGACY_SPECS: dict[str, dict[str, Any]] = {
    "core": {
        "required": ["岩心编号", "所属钻孔", "取样深度起"],
        "status_order": ["待编录", "已编录", "送样中", "已归还"],
        "actions": {"地质编录": "已编录", "送样分析": "送样中", "归还原箱": "已归还"},
        "keyword_field": "岩心编号",
        "missing_noun": "岩心样本",
        "scope_label": "岩心管理",
    },
    "sample_registry": {
        "required": ["送检编号", "样品名称", "采样位置"],
        "status_order": ["待收样", "已收样", "检测中", "已出报告"],
        "actions": {"确认收样": "已收样", "登记报告": "已出报告", "退回样品": "待收样"},
        "keyword_field": "送检编号",
        "missing_noun": "送检样品",
        "scope_label": "样品登记",
    },
    "assay": {
        "required": ["化验编号", "样品编号", "元素名称"],
        "status_order": ["待录入", "已录入", "已审核", "已退回"],
        "actions": {"录入结果": "已录入", "审核通过": "已审核", "退回修改": "已退回"},
        "keyword_field": "化验编号",
        "missing_noun": "化验结果",
        "scope_label": "化验数据",
    },
}


# 各台账允许透传的非必填业务列（旧实现丢弃多余键；周期重构后需要保留
# 取样深度止等复核字段和跨台账引用键岩心编号）。
PASSTHROUGH_FIELDS: dict[str, tuple[str, ...]] = {
    "core": ("取样深度止", "岩性描述", "采取率", "存放位置", "样本状态", "岩心编号"),
    "sample_registry": ("岩心编号", "检测项目", "送检单位", "收样日期", "检测周期", "送检状态"),
    "assay": ("岩心编号", "化验值", "单位", "化验方法", "化验日期", "结果状态"),
}


@dataclass
class LegacyLedgerService:
    """一处台账的旧口径服务（纯存储读写，不含跨台账判断）。"""

    module: str

    @property
    def spec(self) -> dict[str, Any]:
        return LEGACY_SPECS[self.module]

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(self.module)
        if keyword:
            field_name = self.spec["keyword_field"]
            rows = [row for row in rows if keyword in str(row.get(field_name, ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._public_view(row) for row in rows[start:start + size]], total

    def all_rows(self) -> list[dict[str, Any]]:
        """对外全量行（已剥离内部投影键）。"""
        return [self._public_view(row) for row in store.rows(self.module)]

    def raw_rows(self) -> list[dict[str, Any]]:
        """存储原始行（on 模式做视图合并时使用，避免读到被剥离的投影）。"""
        return list(store.rows(self.module))

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(self.module, entry_id)
        return self._public_view(row) if row is not None else None

    @staticmethod
    def _public_view(row: dict[str, Any]) -> dict[str, Any]:
        """旧路径视图：剥离样本周期内部投影键，保证回退到 off 后旧契约字节级一致。

        内部键仍保留在存储里，重新切回 on 即可继续使用，不需要重新回填。
        """
        return {key: value for key, value in row.items() if not key.startswith("cycle_")}

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        required = self.spec["required"]
        missing = [field for field in required if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(self.module)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in required})
        entry["status"] = self.spec["status_order"][0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 透传统一周期联动字段（岩心编号）与深度复核等已知业务列，不改变旧必填口径。
        for extra in PASSTHROUGH_FIELDS[self.module]:
            if extra in values and extra not in entry:
                entry[extra] = values.get(extra)
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        spec = self.spec
        entry = store.find(self.module, entry_id)
        if entry is None:
            return None, f"{spec['missing_noun']} {entry_id} 不存在或已归档"
        if action not in spec["actions"]:
            return None, f"动作「{action}」不属于{spec['scope_label']}可执行范围"
        target = spec["actions"][action]
        if target not in spec["status_order"]:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != spec["status_order"][-1]
        entry["abnormal"] = False
        subject = {"岩心管理": "岩心样本已", "样品登记": "送检样品已", "化验数据": "化验结果已"}[
            spec["scope_label"]
        ]
        return self._public_view(entry), f"{subject}{action}"
