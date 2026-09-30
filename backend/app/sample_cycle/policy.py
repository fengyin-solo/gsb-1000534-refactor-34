"""统一可用性口径：阶段流转表、命令合法性、三处台账的结论推导。

这里是整个重构的「唯一裁决点」：

* 岩心台账问「这条样本能不能送样」——看深度复核责任组有没有放行；
* 送样清单问「这条样品能不能安排送样 / 该不该签收」——看送样组阶段；
* 化验待办问「能不能录入结果」——看化验组是否已签收。

三个问题都从同一个周期阶段推导，因此同一样本不可能再出现三个结论。
旧代码里三处服务各写各的判断（core 的送样无前置、sample_registry 可
跳过签收、assay 可未收样先录入），在本文件以同一张流转表取代。
"""
from __future__ import annotations

from app.sample_cycle import stages as S
from app.sample_cycle.events import (
    ASSAY_COMPLETED,
    ASSAY_RETURNED,
    ASSAY_STARTED,
    DEPTH_REVIEW_PASSED,
    DEPTH_REVIEW_REJECTED,
    GEOLOGY_LOGGED,
    LEDGER_ASSAY,
    LEDGER_CORE,
    LEDGER_DISPATCH,
    SAMPLE_CLOSED,
    SAMPLE_DISPATCHED,
    SAMPLE_RECEIVED,
    SAMPLE_REGISTERED,
    SAMPLE_REJECTED,
    SAMPLE_RETURNING,
)
from app.sample_cycle.stages import (
    DISPATCHABLE,
    NOT_USABLE,
    PENDING,
    TESTABLE,
    USABLE,
)


class PolicyViolation(Exception):
    """命令不满足阶段责任组的裁决条件。message 直接面向既有接口返回。"""


# from_stage -> 事件 -> (to_stage, 责任组, 说明)。
# 只有列出的迁移合法；未列出的组合一律拒绝，越权动作在领域层就过不去。
TRANSITIONS: dict[str, dict[str, tuple[str, str, str]]] = {
    S.REGISTERED: {
        GEOLOGY_LOGGED: (S.LOGGED, S.CORE_LEDGER_GROUP, "地质编录完成，移交取样深度复核"),
    },
    S.LOGGED: {
        DEPTH_REVIEW_PASSED: (S.DEPTH_REVIEWED, S.DEPTH_REVIEW_GROUP, "取样深度复核通过"),
        DEPTH_REVIEW_REJECTED: (S.DEPTH_REJECTED, S.DEPTH_REVIEW_GROUP, "取样深度复核驳回，退回补录"),
    },
    S.DEPTH_REJECTED: {
        # 补录后重新编录：回到编录完成态，再由复核组裁决
        GEOLOGY_LOGGED: (S.LOGGED, S.CORE_LEDGER_GROUP, "补录后重新提交深度复核"),
        # 驳回问题当场消除（如仅修订深度描述），复核组也可直接再裁决放行
        DEPTH_REVIEW_PASSED: (S.DEPTH_REVIEWED, S.DEPTH_REVIEW_GROUP, "驳回项已消除，复核通过"),
    },
    S.DEPTH_REVIEWED: {
        SAMPLE_DISPATCHED: (S.DISPATCHED, S.DISPATCH_GROUP, "样品已交运，送样中"),
    },
    S.DISPATCH_READY: {
        SAMPLE_DISPATCHED: (S.DISPATCHED, S.DISPATCH_GROUP, "样品已交运，送样中"),
    },
    S.DISPATCHED: {
        SAMPLE_RECEIVED: (S.RECEIVED, S.LAB_GROUP, "实验室已签收"),
        SAMPLE_REJECTED: (S.DISPATCH_READY, S.LAB_GROUP, "实验室拒收，样品退回送样组"),
    },
    S.RECEIVED: {
        ASSAY_STARTED: (S.IN_LAB, S.LAB_GROUP, "已建立化验待办，开始化验"),
        SAMPLE_REJECTED: (S.DISPATCH_READY, S.LAB_GROUP, "签收后退回样品"),
    },
    S.IN_LAB: {
        ASSAY_COMPLETED: (S.ASSAY_DONE, S.LAB_GROUP, "化验完成"),
        ASSAY_RETURNED: (S.ASSAY_RETURNED, S.LAB_GROUP, "化验退回，等待送样组处置"),
    },
    S.ASSAY_RETURNED: {
        SAMPLE_DISPATCHED: (S.DISPATCHED, S.DISPATCH_GROUP, "退回样品重新送样"),
        SAMPLE_REJECTED: (S.DISPATCH_READY, S.DISPATCH_GROUP, "退回样品改判不可送检"),
        ASSAY_STARTED: (S.IN_LAB, S.LAB_GROUP, "异议解除后重新化验"),
    },
    S.ASSAY_DONE: {
        SAMPLE_RETURNING: (S.RETURNING, S.ARCHIVE_GROUP, "样品启运归还"),
    },
    S.RETURNING: {
        SAMPLE_CLOSED: (S.CLOSED, S.ARCHIVE_GROUP, "样品归还原箱，周期闭环"),
        SAMPLE_REJECTED: (S.DISPATCH_READY, S.ARCHIVE_GROUP, "归还异常，退回送样组"),
    },
}

# 建周期时的首事件不走上表，单独定义，避免和「重新编录」之类迁移混淆。
FIRST_EVENT = (SAMPLE_REGISTERED, S.REGISTERED, S.CORE_LEDGER_GROUP)


def resolve_transition(from_stage: str, event_type: str) -> tuple[str, str, str]:
    """返回 (目标阶段, 责任组, 说明)；不合法时抛 PolicyViolation。"""
    table = TRANSITIONS.get(from_stage, {})
    if event_type not in table:
        raise PolicyViolation(
            f"样本当前处于「{from_stage}」，责任组「{S.STAGE_OWNER.get(from_stage, '?')}」"
            f"未放行事件「{event_type}」，不能直接执行"
        )
    return table[event_type]


# --- 三处台账的统一可用性结论 -------------------------------------------------

def core_availability(stage: str) -> str:
    """岩心台账口径：所属钻孔 + 取样深度复核通过，样本才「可送样」。

    送样在途之后样本已离开岩心箱，对台账而言不再处于「可送样」状态，
    闭环后同样不可再送。
    """
    if stage in (S.DEPTH_REVIEWED, S.DISPATCH_READY, S.ASSAY_RETURNED):
        return USABLE
    if stage in (S.REGISTERED, S.LOGGED, S.DEPTH_REJECTED):
        return NOT_USABLE
    return NOT_USABLE


def dispatch_availability(stage: str) -> str:
    """送样清单口径：深度复核放行后可安排送样；在途/签收后不可重复安排。"""
    if stage in (S.DEPTH_REVIEWED, S.DISPATCH_READY, S.ASSAY_RETURNED):
        return DISPATCHABLE
    if stage in (S.DISPATCHED, S.RETURNING):
        return PENDING
    if stage == S.CLOSED:
        return NOT_USABLE
    return NOT_USABLE


def assay_availability(stage: str) -> str:
    """化验待办口径：实验室签收之后才允许录入结果。"""
    if stage in (S.RECEIVED,):
        return TESTABLE
    if stage == S.IN_LAB:
        return TESTABLE  # 化验中仍可继续录入
    if stage in (S.DISPATCHED,):
        return PENDING
    return NOT_USABLE


def availability_triple(stage: str) -> dict[str, str]:
    """一个阶段 -> 三处台账结论的完整三元组。三处永远同源。"""
    return {
        LEDGER_CORE: core_availability(stage),
        LEDGER_DISPATCH: dispatch_availability(stage),
        LEDGER_ASSAY: assay_availability(stage),
    }


# --- 旧台账状态 -> 隐含阶段：迁移初始基准的映射 -------------------------------
# 「以既有确认阶段为初始基准」：历史记录不再重放业务，只按各台账最后一次
# 已确认状态反查周期阶段；三张表各自映射后再按规则对齐（见 migration.py）。

CORE_LEGACY_STAGE = {
    "待编录": S.REGISTERED,
    "已编录": S.LOGGED,
    "送样中": S.DISPATCHED,
    "已归还": S.CLOSED,
}

DISPATCH_LEGACY_STAGE = {
    "待收样": S.DISPATCH_READY,
    "已收样": S.RECEIVED,
    "检测中": S.IN_LAB,
    "已出报告": S.ASSAY_DONE,
}

ASSAY_LEGACY_STAGE = {
    "待录入": S.RECEIVED,
    "已录入": S.IN_LAB,
    "已审核": S.ASSAY_DONE,
    "已退回": S.ASSAY_RETURNED,
}

# 旧台账视角下的「可不可用」——迁移时必须按旧裁决原样留痕，不能用新口径改写。
def legacy_ruling(ledger: str, legacy_status: str) -> dict[str, str]:
    """复刻重构前三处各自维护的可用性判断，供 LEGACY_RULING 留痕。"""
    if ledger == LEDGER_CORE:
        # 旧 core.run_action：送样分析没有任何前置，任何状态都允许送样
        usable = "可送样" if legacy_status != "已归还" else "不可用"
    elif ledger == LEDGER_DISPATCH:
        # 旧 sample_registry：待收样即可操作收样/送样流程，出报告后终结
        usable = "可安排送样" if legacy_status in ("待收样", "已收样") else "待裁决"
    else:
        # 旧 assay：待录入/已录入都可继续录入，与是否签收无关
        usable = "可化验" if legacy_status in ("待录入", "已录入") else "待裁决"
    return {"ledger": ledger, "legacy_status": legacy_status, "legacy_availability": usable}


def ledger_legacy_stage(ledger: str, legacy_status: str) -> str | None:
    table = {
        LEDGER_CORE: CORE_LEGACY_STAGE,
        LEDGER_DISPATCH: DISPATCH_LEGACY_STAGE,
        LEDGER_ASSAY: ASSAY_LEGACY_STAGE,
    }[ledger]
    return table.get(legacy_status)
