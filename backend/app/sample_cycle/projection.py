"""读模型投影：把统一的周期阶段回写到三张既有台账。

回写只投影字段、不改变三张表的既有语义与接口形态：

* 岩心台账 core：写 ``status/pending/abnormal`` 以及统一可用性字段
  ``样本可用性``、``周期阶段``；
* 送样清单 sample_registry：投影已存在的送样清单行（由送样动作物化或
  迁移前已存在），写状态与可用性，不新增清单条目；
* 化验待办 assay：同上，写既有化验行。

可用性结论统一来自 :func:`policy.availability_triple`，因此三处回写
永远一致；投影是纯函数，同一周期状态重跑投影得到完全相同的行内容，
这是「重跑同一批次得到相同结果」在写路径上的保证。

为了让旧接口的状态过滤（如 ``?status=送样中``）在新模式下仍能命中，
投影后的旧状态词沿用三张表原有的状态词汇。
"""
from __future__ import annotations

from typing import Any

from app.sample_cycle.events import LEDGER_ASSAY, LEDGER_CORE, LEDGER_DISPATCH
from app.sample_cycle.policy import availability_triple
from app.sample_cycle import stages as S

# 周期阶段 -> 岩心台账旧状态词汇
CORE_STATUS = {
    S.REGISTERED: "待编录",
    S.LOGGED: "已编录",
    S.DEPTH_REJECTED: "已编录",
    S.DEPTH_REVIEWED: "已编录",
    S.DISPATCH_READY: "已编录",
    S.DISPATCHED: "送样中",
    S.RECEIVED: "送样中",
    S.IN_LAB: "送样中",
    S.ASSAY_RETURNED: "送样中",
    S.ASSAY_DONE: "送样中",
    S.RETURNING: "送样中",
    S.CLOSED: "已归还",
}

# 周期阶段 -> 送样清单旧状态词汇
DISPATCH_STATUS = {
    S.REGISTERED: "待收样",
    S.LOGGED: "待收样",
    S.DEPTH_REJECTED: "待收样",
    S.DEPTH_REVIEWED: "待收样",
    S.DISPATCH_READY: "待收样",
    S.DISPATCHED: "检测中",     # 旧清单没有「在途」词，在途期间以检测中承接
    S.RECEIVED: "已收样",
    S.IN_LAB: "检测中",
    S.ASSAY_RETURNED: "待收样",
    S.ASSAY_DONE: "已出报告",
    S.RETURNING: "已出报告",
    S.CLOSED: "已出报告",
}

# 周期阶段 -> 化验待办旧状态词汇（仅在化验行已存在时有意义）
ASSAY_STATUS = {
    S.RECEIVED: "待录入",
    S.IN_LAB: "已录入",
    S.ASSAY_RETURNED: "已退回",
    S.ASSAY_DONE: "已审核",
}

# 旧看板的 pending/abnormal 口径，投影后保持原语义：终态非 pending，
# 驳回/退回类阶段为 abnormal。
ABNORMAL_STAGES = {S.DEPTH_REJECTED, S.ASSAY_RETURNED}
TERMINAL_STAGES = {S.CLOSED}


def project_core(row: dict[str, Any], stage: str) -> dict[str, Any]:
    """回写岩心台账行。行内容就地更新并返回，字段集与旧接口一致 + 两个新字段。"""
    triple = availability_triple(stage)
    row["status"] = CORE_STATUS[stage]
    row["pending"] = stage not in TERMINAL_STAGES
    row["abnormal"] = stage in ABNORMAL_STAGES
    row["样本状态"] = _display_sample_state(stage)
    row["样本可用性"] = triple[LEDGER_CORE]
    row["周期阶段"] = stage
    return row


def project_dispatch(row: dict[str, Any], stage: str) -> dict[str, Any]:
    """回写送样清单行。"""
    triple = availability_triple(stage)
    row["status"] = DISPATCH_STATUS[stage]
    row["pending"] = stage not in TERMINAL_STAGES
    row["abnormal"] = stage in ABNORMAL_STAGES
    row["送检状态"] = _display_dispatch_state(stage)
    row["样本可用性"] = triple[LEDGER_DISPATCH]
    row["周期阶段"] = stage
    return row


def project_assay(row: dict[str, Any], stage: str) -> dict[str, Any]:
    """回写化验待办行；阶段尚未到达化验组时保持旧状态，只同步可用性与阶段。"""
    triple = availability_triple(stage)
    if stage in ASSAY_STATUS:
        row["status"] = ASSAY_STATUS[stage]
        row["结果状态"] = ASSAY_STATUS[stage]
    row["pending"] = stage in (S.RECEIVED, S.IN_LAB, S.ASSAY_RETURNED)
    row["abnormal"] = stage in (S.ASSAY_RETURNED,)
    row["样本可用性"] = triple[LEDGER_ASSAY]
    row["周期阶段"] = stage
    return row


def _display_sample_state(stage: str) -> str:
    mapping = {
        S.REGISTERED: "待编录",
        S.LOGGED: "待深度复核",
        S.DEPTH_REJECTED: "深度复核驳回",
        S.DEPTH_REVIEWED: "复核通过待送样",
        S.DISPATCH_READY: "待送样",
        S.DISPATCHED: "送样中",
        S.RECEIVED: "实验室已签收",
        S.IN_LAB: "化验中",
        S.ASSAY_RETURNED: "化验退回",
        S.ASSAY_DONE: "化验完成",
        S.RETURNING: "归还中",
        S.CLOSED: "已归还",
    }
    return mapping[stage]


def _display_dispatch_state(stage: str) -> str:
    mapping = {
        S.REGISTERED: "未进入送样",
        S.LOGGED: "未进入送样",
        S.DEPTH_REJECTED: "复核驳回",
        S.DEPTH_REVIEWED: "可安排送样",
        S.DISPATCH_READY: "待送样",
        S.DISPATCHED: "样品在途",
        S.RECEIVED: "已签收",
        S.IN_LAB: "检测中",
        S.ASSAY_RETURNED: "化验退回",
        S.ASSAY_DONE: "已出报告",
        S.RETURNING: "样品归还中",
        S.CLOSED: "已闭环",
    }
    return mapping[stage]
