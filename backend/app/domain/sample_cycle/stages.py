"""样本周期：阶段、责任组与三处台账的状态映射。

这是重构后的单一事实源（single source of truth）：
- 岩心台账（core）、送样清单（sample_registry）、化验待办（assay）不再各自维护
  “可不可用”的判断，统一由样本周期的当前阶段投影得出。
- 每个阶段归属一个明确的责任组，阶段责任组决定动作由谁负责、结果回写哪几处。
- 旧台账状态与周期阶段之间是 1:1 的机械映射（含原裁决），用于灰度双读与回滚对齐。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

# ---------------------------------------------------------------------------
# 阶段与责任组
# ---------------------------------------------------------------------------


class StageGroup(str, Enum):
    """阶段责任组：每个阶段只能由一个责任组负责裁决。"""

    CORE_KEEPING = "岩心保管组"      # 编录、深度复核、在库保管
    SAMPLE_DELIVERY = "送样流转组"   # 送样、收样
    ASSAY_TESTING = "化验检测组"     # 化验录入、审核
    CLOSEOUT = "归档组"              # 归还销样


# 统一阶段序列。顺序即周期推进方向，禁止逆向跳转（回退以“退回/退回”负向事件显式表达）。
STAGE_REGISTERED = "REGISTERED"          # 已登记
STAGE_LOGGED = "LOGGED"                  # 已编录
STAGE_DEPTH_RECHECKED = "DEPTH_RECHECKED"  # 取样深度复核通过
STAGE_TO_SAMPLE = "TO_SAMPLE"            # 已送样（送样清单可见）
STAGE_SAMPLE_RECEIVED = "SAMPLE_RECEIVED"  # 实验室已收样（化验待办可见）
STAGE_ASSAY_ENTERED = "ASSAY_ENTERED"    # 化验结果已录入
STAGE_ASSAY_APPROVED = "ASSAY_APPROVED"  # 化验结果已审核
STAGE_RETURNED = "RETURNED"              # 已归还原箱，周期闭合

CANONICAL_STAGES: list[str] = [
    STAGE_REGISTERED,
    STAGE_LOGGED,
    STAGE_DEPTH_RECHECKED,
    STAGE_TO_SAMPLE,
    STAGE_SAMPLE_RECEIVED,
    STAGE_ASSAY_ENTERED,
    STAGE_ASSAY_APPROVED,
    STAGE_RETURNED,
]

STAGE_ORDER: Mapping[str, int] = {stage: i for i, stage in enumerate(CANONICAL_STAGES)}

# 阶段 -> 责任组、对外阶段名（回写字段时给人看的名字）
STAGE_LABELS: Mapping[str, str] = {
    STAGE_REGISTERED: "已登记",
    STAGE_LOGGED: "已编录",
    STAGE_DEPTH_RECHECKED: "深度已复核",
    STAGE_TO_SAMPLE: "已送样",
    STAGE_SAMPLE_RECEIVED: "已收样",
    STAGE_ASSAY_ENTERED: "结果已录入",
    STAGE_ASSAY_APPROVED: "结果已审核",
    STAGE_RETURNED: "已归还",
}

STAGE_OWNERS: Mapping[str, StageGroup] = {
    STAGE_REGISTERED: StageGroup.CORE_KEEPING,
    STAGE_LOGGED: StageGroup.CORE_KEEPING,
    STAGE_DEPTH_RECHECKED: StageGroup.CORE_KEEPING,
    STAGE_TO_SAMPLE: StageGroup.SAMPLE_DELIVERY,
    STAGE_SAMPLE_RECEIVED: StageGroup.SAMPLE_DELIVERY,
    STAGE_ASSAY_ENTERED: StageGroup.ASSAY_TESTING,
    STAGE_ASSAY_APPROVED: StageGroup.ASSAY_TESTING,
    STAGE_RETURNED: StageGroup.CLOSEOUT,
}


# ---------------------------------------------------------------------------
# 三处台账的机械映射（旧状态 <-> 周期阶段）
# ---------------------------------------------------------------------------

LEDGER_CORE = "core"
LEDGER_SAMPLE_REGISTRY = "sample_registry"
LEDGER_ASSAY = "assay"

# 受样本周期统一管理的三处台账（顺序固定，决定迁移与投影的确定性遍历顺序）
CYCLE_LEDGERS: tuple[str, ...] = (LEDGER_CORE, LEDGER_SAMPLE_REGISTRY, LEDGER_ASSAY)

# 台账内行的统一引用字段：三处都通过岩心编号关联到同一个样本周期。
LEDGER_REF_FIELDS: Mapping[str, str] = {
    LEDGER_CORE: "岩心编号",
    LEDGER_SAMPLE_REGISTRY: "岩心编号",
    LEDGER_ASSAY: "岩心编号",
}

# 旧台账状态 -> 周期阶段。映射只做机械转写，不做“再裁决”，历史原裁决据此留痕。
STAGE_BY_LEDGER_STATUS: Mapping[str, Mapping[str, str]] = {
    LEDGER_CORE: {
        "待编录": STAGE_REGISTERED,
        "已编录": STAGE_LOGGED,
        "送样中": STAGE_TO_SAMPLE,
        "已归还": STAGE_RETURNED,
    },
    LEDGER_SAMPLE_REGISTRY: {
        "待收样": STAGE_TO_SAMPLE,
        "已收样": STAGE_SAMPLE_RECEIVED,
        "检测中": STAGE_ASSAY_ENTERED,
        "已出报告": STAGE_ASSAY_APPROVED,
    },
    LEDGER_ASSAY: {
        "待录入": STAGE_SAMPLE_RECEIVED,
        "已录入": STAGE_ASSAY_ENTERED,
        "已审核": STAGE_ASSAY_APPROVED,
        "已退回": STAGE_SAMPLE_RECEIVED,
    },
}

# 周期阶段 -> 旧台账状态（反向投影，保证灰度切流后旧接口/旧页面读到等价语义）。
# None 表示该责任组在这个阶段还没有行（样本尚未进入该台账的生命周期）。
LEDGER_STATUS_BY_STAGE: Mapping[str, Mapping[str, str | None]] = {
    LEDGER_CORE: {
        STAGE_REGISTERED: "待编录",
        STAGE_LOGGED: "已编录",
        STAGE_DEPTH_RECHECKED: "已编录",
        STAGE_TO_SAMPLE: "送样中",
        STAGE_SAMPLE_RECEIVED: "送样中",
        STAGE_ASSAY_ENTERED: "送样中",
        STAGE_ASSAY_APPROVED: "送样中",
        STAGE_RETURNED: "已归还",
    },
    LEDGER_SAMPLE_REGISTRY: {
        STAGE_REGISTERED: None,
        STAGE_LOGGED: None,
        STAGE_DEPTH_RECHECKED: None,
        STAGE_TO_SAMPLE: "待收样",
        STAGE_SAMPLE_RECEIVED: "已收样",
        STAGE_ASSAY_ENTERED: "检测中",
        STAGE_ASSAY_APPROVED: "已出报告",
        STAGE_RETURNED: "已出报告",
    },
    LEDGER_ASSAY: {
        STAGE_REGISTERED: None,
        STAGE_LOGGED: None,
        STAGE_DEPTH_RECHECKED: None,
        STAGE_TO_SAMPLE: None,
        STAGE_SAMPLE_RECEIVED: "待录入",
        STAGE_ASSAY_ENTERED: "已录入",
        STAGE_ASSAY_APPROVED: "已审核",
        STAGE_RETURNED: "已审核",
    },
}


# ---------------------------------------------------------------------------
# 入口闸口（阶段责任组接样前必须满足的条件）
# ---------------------------------------------------------------------------

# 送样流转组接样前，岩心保管组必须完成取样深度复核：起/止深度齐全且可解析。
DEPTH_FIELDS = ("取样深度起", "取样深度止")


def depth_recheck_ready(row: Mapping[str, object]) -> bool:
    """取样深度复核闸口：起、止深度都能解析成数值，且止深不小于起深。"""
    try:
        start = float(str(row.get(DEPTH_FIELDS[0]) or "").strip())
        end = float(str(row.get(DEPTH_FIELDS[1]) or "").strip())
    except (TypeError, ValueError):
        return False
    return end >= start


# ---------------------------------------------------------------------------
# 命令定义：旧动作 -> 周期状态机命令
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandSpec:
    """一条旧接口动作在周期状态机里的定义。

    name:      旧接口里的动作名（接口形态不变，名字不改）。
    ledger:    动作的入口台账。
    to_stage:  成功后推进到的周期阶段。
    negative:  是否负向（退回/退回）。负向事件同样幂等留痕，不删历史。
    """

    name: str
    ledger: str
    to_stage: str
    negative: bool = False


# 以既有确认阶段为初始基准：
# - 岩心的“地质编录/送样分析/归还原箱”
# - 送样清单的“确认收样/登记报告/退回样品”
# - 化验待办的“录入结果/审核通过/退回修改”
COMMAND_SPECS: tuple[CommandSpec, ...] = (
    CommandSpec("地质编录", LEDGER_CORE, STAGE_LOGGED),
    CommandSpec("送样分析", LEDGER_CORE, STAGE_TO_SAMPLE),
    CommandSpec("归还原箱", LEDGER_CORE, STAGE_RETURNED),
    CommandSpec("确认收样", LEDGER_SAMPLE_REGISTRY, STAGE_SAMPLE_RECEIVED),
    CommandSpec("登记报告", LEDGER_SAMPLE_REGISTRY, STAGE_ASSAY_APPROVED),
    CommandSpec("退回样品", LEDGER_SAMPLE_REGISTRY, STAGE_TO_SAMPLE, negative=True),
    CommandSpec("录入结果", LEDGER_ASSAY, STAGE_ASSAY_ENTERED),
    CommandSpec("审核通过", LEDGER_ASSAY, STAGE_ASSAY_APPROVED),
    CommandSpec("退回修改", LEDGER_ASSAY, STAGE_SAMPLE_RECEIVED, negative=True),
)

COMMANDS_BY_LEDGER_ACTION: Mapping[tuple[str, str], CommandSpec] = {
    (spec.ledger, spec.name): spec for spec in COMMAND_SPECS
}

# 各台账旧接口允许出现的动作全集（用于保持“动作不属于可执行范围”的旧报错口径）。
ACTIONS_BY_LEDGER: Mapping[str, tuple[str, ...]] = {
    ledger: tuple(spec.name for spec in COMMAND_SPECS if spec.ledger == ledger)
    for ledger in CYCLE_LEDGERS
}

# 各台账旧状态序列（迁移与兼容投影时使用，内容与重构前三处服务完全一致）。
LEGACY_STATUS_ORDER: Mapping[str, list[str]] = {
    LEDGER_CORE: ["待编录", "已编录", "送样中", "已归还"],
    LEDGER_SAMPLE_REGISTRY: ["待收样", "已收样", "检测中", "已出报告"],
    LEDGER_ASSAY: ["待录入", "已录入", "已审核", "已退回"],
}


def stage_index(stage: str) -> int:
    return STAGE_ORDER[stage]


def latest_stage(stages: list[str]) -> str:
    """取一组阶段中推进最远的一个；空列表视为尚未登记。"""
    return max(stages, key=stage_index) if stages else STAGE_REGISTERED
