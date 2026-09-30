"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

样本周期改造在本层补了两件基础设施，领域包 ``app.sample_cycle`` 依赖它们：

* ``transaction()`` —— 快照式事务，事务内任意一步抛错都会整体回滚，
  保证「事件落库 + 三台账回写」要么全部成功、要么全部不发生；
* ``reset()`` —— 按种子数据重建，供迁移回填的灰度演练与测试反复重跑。
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from app.seed import SEED_ROWS


class Transaction:
    """快照事务：进入时深拷贝全表，异常退出时整体恢复。

    支持嵌套：内层事务复用外层快照，只有最外层能提交/回滚，
    内层失败会标记回滚并把异常继续抛给外层，避免「内层失败、外层照提交」。
    """

    def __init__(self, store: "Store") -> None:
        self._store = store
        self._outermost = False
        self._need_rollback = False

    def __enter__(self) -> "Transaction":
        with self._store._lock:
            if not self._store._txn_stack:
                self._store._txn_stack.append(copy.deepcopy(self._store._tables))
                self._outermost = True
            return self

    def __exit__(self, exc_type, exc, tb) -> None:
        with self._store._lock:
            if not self._outermost:
                # 内层异常：交给最外层统一回滚；内层正常结束不做任何事。
                if exc_type is not None:
                    self._store._txn_nested_failed = True
                return None
            snapshot = self._store._txn_stack.pop()
            nested_failed = self._store._txn_nested_failed
            self._store._txn_nested_failed = False
            if exc_type is not None or nested_failed:
                self._store._tables = snapshot
            return None


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._txn_stack: list[dict[str, list[dict[str, Any]]]] = []
        self._txn_nested_failed = False
        self._tables: dict[str, list[dict[str, Any]]] = self._seeded()

    @staticmethod
    def _seeded() -> dict[str, list[dict[str, Any]]]:
        return {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}

    def transaction(self) -> Transaction:
        """开启一个事务边界。用法：``with store.transaction(): ...``"""
        return Transaction(self)

    def reset(self) -> None:
        """丢弃全部改动、按种子数据重建（迁移演练 / 测试用）。"""
        with self._lock:
            self._tables = self._seeded()
            self._txn_stack.clear()
            self._txn_nested_failed = False

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def upsert(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """按 ``id`` 插入或更新整行，供统一写口径使用。"""
        rows = self.rows(module)
        for index, existing in enumerate(rows):
            if int(existing.get("id", 0)) == int(row.get("id", 0)):
                rows[index] = row
                return row
        rows.append(row)
        return row

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

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


store = Store()
