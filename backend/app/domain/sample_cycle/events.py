"""幂等事件与样本周期聚合。

事件是周期状态机的唯一写入方式，且必须幂等：
- 在线写入：同一 ``(样本, 命令)`` 在同一阶段上重复提交，归并为同一条事件；
  调用方也可显式带 ``idempotency_key``（接口可选字段，默认形态不变）。
- 历史回填：从三处旧台账的已确认阶段机械转写为 ``LEGACY`` 事件，
  事件 ID 由来源坐标（台账、行 id、旧状态）决定，重跑同一批次结果完全一致。

事件只追加、不修改、不删除（含负向事件），“历史记录按原裁决留痕”由此保证。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.domain.sample_cycle.stages import (
    CANONICAL_STAGES,
    STAGE_ORDER,
    latest_stage,
    stage_index,
)

EVENT_KIND_COMMAND = "COMMAND"   # 在线命令产生
EVENT_KIND_LEGACY = "LEGACY"     # 回填：旧台账既有确认阶段转写
EVENT_KIND_RECOVERY = "RECOVERY"  # 在线写入时发现该样本尚未回填，按既有阶段补齐


def _stable_json(payload: Mapping[str, Any]) -> str:
    """规范化 JSON：键排序、无空白，保证哈希在任何机器/进程上一致。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def legacy_event_id(sample_key: str, ledger: str, row_id: int, status: str) -> str:
    """LEGACY 事件 ID：只由来源坐标决定，重跑得到同一 ID、同一内容。"""
    digest = hashlib.sha256(
        _stable_json(
            {
                "kind": EVENT_KIND_LEGACY,
                "sample": sample_key,
                "ledger": ledger,
                "row_id": row_id,
                "status": status,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"ev-{digest[:16]}"


def command_event_id(
    sample_key: str,
    action: str,
    from_stage: str,
    to_stage: str,
    idempotency_key: str | None,
) -> str:
    """命令事件 ID：显式幂等键优先，否则由 样本+动作+前后阶段 决定。

    这样“在同一阶段重复点同一个动作”天然幂等，而不会把合法的两次推进
    （例如两个不同阶段各自触发）错误合并。
    """
    digest = hashlib.sha256(
        _stable_json(
            {
                "kind": EVENT_KIND_COMMAND,
                "sample": sample_key,
                "action": action,
                "from": from_stage,
                "to": to_stage,
                "idempotency_key": idempotency_key or "",
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"ev-{digest[:16]}"


@dataclass
class CycleEvent:
    """一次阶段裁决的不可变事实。"""

    id: str
    sample_key: str
    kind: str
    action: str            # 旧接口动作名；LEGACY 用 "历史裁决"
    from_stage: str
    to_stage: str
    negative: bool
    ledger: str | None     # 入口台账；LEGACY 为来源台账
    batch_id: str | None   # 回填批次；在线命令为 None
    seq: int               # 批次内/在线顺序号
    occurred_at: str       # 回填使用确定性时间戳；在线使用 UTC ISO 时间
    idempotency_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sample_key": self.sample_key,
            "kind": self.kind,
            "action": self.action,
            "from_stage": self.from_stage,
            "to_stage": self.to_stage,
            "negative": self.negative,
            "ledger": self.ledger,
            "batch_id": self.batch_id,
            "seq": self.seq,
            "occurred_at": self.occurred_at,
            "idempotency_key": self.idempotency_key,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CycleEvent":
        return cls(
            id=str(raw["id"]),
            sample_key=str(raw["sample_key"]),
            kind=str(raw["kind"]),
            action=str(raw["action"]),
            from_stage=str(raw["from_stage"]),
            to_stage=str(raw["to_stage"]),
            negative=bool(raw.get("negative", False)),
            ledger=raw.get("ledger"),
            batch_id=raw.get("batch_id"),
            seq=int(raw.get("seq", 0)),
            occurred_at=str(raw.get("occurred_at", "")),
            idempotency_key=raw.get("idempotency_key"),
            payload=dict(raw.get("payload") or {}),
        )


class SampleCycle:
    """单个样本（按岩心编号）的周期聚合：事件流 + 归并结论。"""

    def __init__(
        self,
        sample_key: str,
        events: list[CycleEvent] | None = None,
        source_rows: dict[str, int] | None = None,
    ) -> None:
        self.sample_key = sample_key
        self._events: list[CycleEvent] = sorted(events or [], key=lambda e: (e.seq, e.id))
        # 来源台账行坐标：{ledger: row_id}，回写投影时定位具体行。
        self.source_rows: dict[str, int] = dict(source_rows or {})

    # ----- 事件流 ---------------------------------------------------------

    @property
    def events(self) -> list[CycleEvent]:
        return list(self._events)

    def has_event(self, event_id: str) -> bool:
        return any(event.id == event_id for event in self._events)

    def append(self, event: CycleEvent) -> bool:
        """追加事件。事件 ID 已存在则跳过（幂等），返回是否真正新增。"""
        if self.has_event(event.id):
            return False
        self._events.append(event)
        self._events.sort(key=lambda e: (e.seq, e.id))
        return True

    # ----- 归并结论 -------------------------------------------------------

    def current_stage(self) -> str:
        """当前周期阶段：事件流归并，负向事件也参与（显式回退）。

        从起点开始按 seq 顺序应用每条事件的 to_stage；
        LEGACY 回填事件来自三处台账，取其中推进最远的已确认阶段作为初始基准。
        """
        if not self._events:
            return CANONICAL_STAGES[0]
        legacy_stages = [e.to_stage for e in self._events if e.kind == EVENT_KIND_LEGACY]
        stage = latest_stage(legacy_stages) if legacy_stages else CANONICAL_STAGES[0]
        ordered = sorted(self._events, key=lambda e: (e.seq, e.id))
        for event in ordered:
            if event.kind == EVENT_KIND_LEGACY:
                continue
            # 只接受与当前阶段衔接的事件（from->to），防止乱序事件把状态机带歪。
            if event.from_stage == stage or stage_index(event.to_stage) >= stage_index(stage):
                stage = event.to_stage
        return stage

    def legacy_baseline(self) -> str:
        """以既有确认阶段为准的初始基准（回填前的旧裁决）。"""
        legacy = [e.to_stage for e in self._events if e.kind == EVENT_KIND_LEGACY]
        return latest_stage(legacy)

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "sample_key": self.sample_key,
            "current_stage": self.current_stage(),
            "source_rows": dict(self.source_rows),
            "events": [event.to_dict() for event in self._events],
        }

    @classmethod
    def from_snapshot(cls, raw: Mapping[str, Any]) -> "SampleCycle":
        return cls(
            sample_key=str(raw["sample_key"]),
            events=[CycleEvent.from_dict(item) for item in raw.get("events", [])],
            source_rows={str(k): int(v) for k, v in (raw.get("source_rows") or {}).items()},
        )


def is_advancing(from_stage: str, to_stage: str) -> bool:
    return STAGE_ORDER[to_stage] >= STAGE_ORDER[from_stage]
