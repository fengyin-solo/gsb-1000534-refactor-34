"""幂等事件：样本周期内唯一允许发生的事实记录。

重构后三处台账不再各自直接改状态，而是向周期追加「事件」，
当前阶段由事件流归约得到（event-sourced），台账字段只是读模型投影。

幂等分两层：

1. 命令幂等（同一业务意图重放）：事件有自然键 ``natural_key``
   （周期 + 事件类型 + 同一阶段裁决），同自然键事件只保留 seq 最小的一条，
   后续重放直接返回既有事件，不改变状态；
2. 批次幂等（迁移回填重跑）：``MIGRATION_BASELINE`` / ``LEGACY_RULING``
   携带 ``batch_id``，事件 id 由内容哈希决定，重跑同一批次逐条命中、
   不新增事件、不改变既有裁决，因此同一批次重跑结果完全一致。

历史留痕：迁移时三张台账的旧裁决各自落成一条 ``LEGACY_RULING``，
即使三处结论互相矛盾也原样保留，再补一条 ``MIGRATION_BASELINE``
记录「以既有确认阶段为基准」归一后的初始阶段，旧裁决永不被覆盖。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

# --- 事件类型（每一种对应一次且仅一次的状态事实） -----------------------------

SAMPLE_REGISTERED = "sample_registered"        # 岩心建档
GEOLOGY_LOGGED = "geology_logged"              # 地质编录完成
DEPTH_REVIEW_PASSED = "depth_review_passed"    # 取样深度复核通过
DEPTH_REVIEW_REJECTED = "depth_review_rejected"  # 深度复核驳回
SAMPLE_DISPATCHED = "sample_dispatched"        # 样品送出（送样清单在途）
SAMPLE_RECEIVED = "sample_received"            # 实验室签收
SAMPLE_REJECTED = "sample_rejected"            # 送样退回（实验室拒收）
ASSAY_STARTED = "assay_started"                # 建立化验待办/开始化验
ASSAY_RETURNED = "assay_returned"              # 化验退回
ASSAY_COMPLETED = "assay_completed"            # 化验完成
SAMPLE_RETURNING = "sample_returning"          # 样品归还在途
SAMPLE_CLOSED = "sample_closed"                # 归还原箱、周期闭环

# 迁移专用事件：不代表业务动作，只做留痕
LEGACY_RULING = "legacy_ruling"                # 某台账的历史裁决（原样保留）
MIGRATION_BASELINE = "migration_baseline"      # 以既有确认阶段归一的基准

# 发起迁移回填/灰度管控的系统动作
BATCH_BACKFILL = "batch_backfill"
ROLLBACK_BATCH = "rollback_batch"
SET_MODE = "set_mode"

COMMAND_EVENTS = {
    SAMPLE_REGISTERED,
    GEOLOGY_LOGGED,
    DEPTH_REVIEW_PASSED,
    DEPTH_REVIEW_REJECTED,
    SAMPLE_DISPATCHED,
    SAMPLE_RECEIVED,
    SAMPLE_REJECTED,
    ASSAY_STARTED,
    ASSAY_RETURNED,
    ASSAY_COMPLETED,
    SAMPLE_RETURNING,
    SAMPLE_CLOSED,
}

MIGRATION_EVENTS = {LEGACY_RULING, MIGRATION_BASELINE}

# 同一周期内，这些事件允许重复发生（驳回后再次提交、退回后再送样）；
# 自然键里带上「所针对的阶段」，使同一阶段的重复提交仍被幂等去重。
REPEATABLE_EVENTS = {
    DEPTH_REVIEW_PASSED,
    DEPTH_REVIEW_REJECTED,
    SAMPLE_DISPATCHED,
    SAMPLE_RECEIVED,
    SAMPLE_REJECTED,
    ASSAY_STARTED,
    ASSAY_RETURNED,
}

# 三个责任台账的固定标识，历史裁决留痕用
LEDGER_CORE = "core"                    # 岩心台账
LEDGER_DISPATCH = "sample_registry"     # 送样清单
LEDGER_ASSAY = "assay"                 # 化验待办
LEDGERS = (LEDGER_CORE, LEDGER_DISPATCH, LEDGER_ASSAY)


@dataclass
class CycleEvent:
    """一条不可变事件。``id`` 由内容确定，天然支持去重与跨实例复算。"""

    seq: int
    cycle_id: int
    event_type: str
    stage: str                       # 事件发生后周期所处阶段（归约锚点）
    ledger: str                      # 事件来源台账 / 责任入口
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None  # 调用方显式给出的幂等键（如请求幂等号）
    batch_id: str | None = None      # 迁移批次号，业务事件为空
    occurred_at: int = 0             # 单调序号优先，时间仅用于展示
    event_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CycleEvent":
        return cls(**data)

    def natural_key(self) -> str:
        """自然键：同周期 + 同事件 + 同裁决阶段 + 同幂等号 视为同一事实。

        「裁决阶段」用事件自身的 stage（事件发生后周期所处阶段），
        不用 payload 里的 from_stage，保证同一业务意图重放时，
        无论当前阶段走到哪里，自然键都与首次提交完全一致。
        可重复事件（驳回后再提交、退回后再送样）因裁决阶段不同而成为
        不同的事实，天然允许再次发生。
        """
        basis = f"{self.cycle_id}:{self.event_type}:{self.stage}"
        if self.idempotency_key:
            basis = f"{basis}:{self.idempotency_key}"
        return basis


def content_hash(parts: list[Any]) -> str:
    """对事件内容做稳定哈希；键排序保证重跑逐条等价。"""
    blob = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def make_event_id(event: CycleEvent) -> str:
    if event.event_type in MIGRATION_EVENTS:
        # 迁移事件按内容寻址：批次、台账、旧裁决、归一阶段都进哈希，
        # 重跑同一批次时同一历史事实得到同一 id。
        return "evt-" + content_hash([
            "migration",
            event.batch_id,
            event.cycle_id,
            event.event_type,
            event.ledger,
            event.stage,
            json.dumps(event.payload, ensure_ascii=False, sort_keys=True),
        ])
    return "evt-" + content_hash([
        "command",
        event.cycle_id,
        event.event_type,
        event.natural_key(),
        event.payload,
    ])
