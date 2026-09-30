"""样本周期的阶段定义与阶段责任组。

阶段责任组（stage ownership）是这次重构要显式化的第一件事：
每个阶段只有一个责任组可以裁决样本「能不能进入下一环节」，
三处台账（岩心/送样/化验）不再各自判可用性，只读取本组阶段的结论。

阶段顺序即样本生命周期：登记 → 地质编录 → 深度复核 → 待送样 →
送样在途 → 实验室已签收 → 化验中 → 化验完成 → 归还在途 → 已闭环。
另外保留三个反向状态（复核驳回 / 样品退回 / 化验退回），
它们回到上游责任组，由对应组重新裁决后再前进。
"""
from __future__ import annotations

# --- 周期阶段：全系统唯一的样本阶段词汇 -------------------------------------

REGISTERED = "已登记"            # 岩心台账已建档，等待地质编录
LOGGED = "已编录"                # 地质编录完成，等待取样深度复核
DEPTH_REJECTED = "深度复核驳回"  # 深度复核未过，退回编录组补录
DEPTH_REVIEWED = "深度复核通过"  # 取样深度复核通过，具备送样资格
DISPATCH_READY = "待送样"        # 送样清单可见、可安排送样
DISPATCHED = "送样中"            # 样品在途，实验室尚未签收
RECEIVED = "已签收"              # 实验室签收，化验待办可建立
IN_LAB = "化验中"                # 化验数据录入中
ASSAY_RETURNED = "化验退回"      # 化验退回（如样品污染/数据异议）
ASSAY_DONE = "化验完成"          # 化验完成，样品待归还
RETURNING = "归还在途"           # 样品归还在途
CLOSED = "已闭环"                # 样品归还原箱，周期结束

# 阶段在主轴上的次序；反向状态用「退回后的目标阶段」的位置参与比较，
# 这样「当前阶段是否已经走过某个责任组」只有一种答案。
STAGE_ORDER: list[str] = [
    REGISTERED,
    LOGGED,
    DEPTH_REJECTED,
    DEPTH_REVIEWED,
    DISPATCH_READY,
    DISPATCHED,
    RECEIVED,
    IN_LAB,
    ASSAY_RETURNED,
    ASSAY_DONE,
    RETURNING,
    CLOSED,
]


def stage_rank(stage: str) -> int:
    return STAGE_ORDER.index(stage)


# --- 阶段责任组 ---------------------------------------------------------------
# 每个阶段归且仅归一个责任组；跨组的动作（如送样清单直接判「可以化验」）
# 以前会让同一样本得到矛盾结论，现在在流转表里直接拒绝。

CORE_LEDGER_GROUP = "core_ledger"      # 岩心台账组：建档、地质编录
DEPTH_REVIEW_GROUP = "depth_review"    # 取样深度复核组：深度复核裁决
DISPATCH_GROUP = "dispatch"            # 送样组：送样清单、在途跟踪
LAB_GROUP = "lab"                      # 化验组：签收、化验待办、退样
ARCHIVE_GROUP = "archive"              # 归档组：归还原箱、闭环

STAGE_OWNER: dict[str, str] = {
    REGISTERED: CORE_LEDGER_GROUP,
    LOGGED: DEPTH_REVIEW_GROUP,
    DEPTH_REJECTED: CORE_LEDGER_GROUP,
    DEPTH_REVIEWED: DISPATCH_GROUP,
    DISPATCH_READY: DISPATCH_GROUP,
    DISPATCHED: LAB_GROUP,
    RECEIVED: LAB_GROUP,
    IN_LAB: LAB_GROUP,
    ASSAY_RETURNED: DISPATCH_GROUP,
    ASSAY_DONE: ARCHIVE_GROUP,
    RETURNING: ARCHIVE_GROUP,
    CLOSED: ARCHIVE_GROUP,
}

RESPONSIBILITY_GROUPS = (
    (CORE_LEDGER_GROUP, "岩心台账组"),
    (DEPTH_REVIEW_GROUP, "取样深度复核组"),
    (DISPATCH_GROUP, "送样组"),
    (LAB_GROUP, "化验组"),
    (ARCHIVE_GROUP, "归档组"),
)

# --- 三处台账的可用性结论 -----------------------------------------------------
# 统一的三态结论；每个台账只回答自己责任范围内的那个问题，
# 但三个答案都由 policy 基于同一个周期阶段推导，天然一致。

USABLE = "可送样"          # 岩心台账视角：钻孔/深度复核已过，允许送样
DISPATCHABLE = "可安排送样"  # 送样清单视角：可加入送样批次
TESTABLE = "可化验"        # 化验待办视角：实验室已签收，允许录入结果
NOT_USABLE = "不可用"
PENDING = "待裁决"


def owner_of(stage: str) -> str:
    return STAGE_OWNER[stage]
