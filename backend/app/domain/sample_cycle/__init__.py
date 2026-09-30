"""样本周期领域包：统一岩心台账、送样清单、化验待办的可用性判断。

分层说明见 docs/sample-cycle-refactor.md：
- stages.py     阶段/责任组/台账映射/闸口（事实表）
- events.py     幂等事件、事件归并与周期聚合
- projection.py 单一可用性裁决与三处台账回写投影
- cycle_store.py 周期表仓储（事件、周期、批次、双读差异）
- migration.py  确定性回填/回滚批次
- service.py    灰度门面（off/shadow/on 三模式新旧路径转换）
"""
