"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

样本周期重构后，Store 同时承担两件事：
- 业务台账表（18 个模块，module_names 口径不变，内部表不计入）；
- 内部周期表（事件、批次、双读差异），通过 INTERNAL_TABLES 显式标记，
  对外概览/模块列表不可见，避免污染既有接口。

事务边界：``with store.transaction():`` 内的全部写入先作用于工作副本，
异常时整体回滚，绝无“转换未成功但部分写入”的中间态（对应 UoW 模式）。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS

# 样本周期内部表：不参与 module_names / overview / 前端模块枚举。
TABLE_CYCLES = "_sample_cycles"
TABLE_CYCLE_BATCHES = "_sample_cycle_batches"
TABLE_CYCLE_DRIFTS = "_sample_cycle_drifts"
INTERNAL_TABLES: frozenset[str] = frozenset(
    {TABLE_CYCLES, TABLE_CYCLE_BATCHES, TABLE_CYCLE_DRIFTS}
)


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        for name in INTERNAL_TABLES:
            self._tables[name] = []

    def module_names(self) -> list[str]:
        """业务模块名（内部周期表对外不可见，保持重构前后口径一致）。"""
        return sorted(name for name in self._tables if name not in INTERNAL_TABLES)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ----- 事务边界（Unit of Work）---------------------------------------

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """整体提交 / 整体回滚。

        工作期内对表结构和行内容的全部改动都落在深拷贝上；只有 with 块正常
        结束才替换正式数据，任何异常都会丢弃副本，调用方看到的仍是旧状态。
        """
        checkpoint = copy.deepcopy(self._tables)
        committed = False
        self._tables = copy.deepcopy(checkpoint)
        try:
            yield self
            committed = True
        finally:
            if not committed:
                self._tables = checkpoint


store = Store()
