"""确定性回填与回滚：把三处旧台账的已确认阶段机械转写为样本周期事件。

设计约束（对应迁移需求）：
- 以既有确认阶段为初始基准：只做 旧状态 -> 周期阶段 的机械映射，不重新裁决；
- 历史记录按原裁决留痕：每条旧裁决生成一条不可变 LEGACY 事件，负向状态保留负向标记；
- 可重跑：批次 ID 与事件 ID 都由来源数据内容决定，同一批数据重跑得到完全相同结果；
- 转换未成功不能部分写入：计划构造与校验先全部完成，落库在单个事务内提交，
  任何异常都整体回滚；
- 可回滚：回滚批次删除其产生的周期/事件并摘除投影字段，旧行原始字段从未被迁移改写；
- 双读校验：计划阶段逐条比对“旧口径”与“统一裁决”，差异全部登记，不阻断回填。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from app.domain.sample_cycle.cycle_store import (
    NEGATIVE_STATUS,
    CommandRejected,
    CycleRepository,
    utc_now_iso,
)
from app.domain.sample_cycle.events import EVENT_KIND_LEGACY, CycleEvent, SampleCycle
from app.domain.sample_cycle.projection import (
    ADDITIVE_KEYS,
    assess_cycle,
    sample_key_of,
)
from app.domain.sample_cycle.stages import (
    CYCLE_LEDGERS,
    LEDGER_REF_FIELDS,
    STAGE_BY_LEDGER_STATUS,
)
from app.store import Store

# 确定性回填时间戳：重跑必须一致，所以不用墙上时钟。
MIGRATION_EPOCH = "2026-09-30T00:00:00Z"
STATUS_UNKNOWN = object()


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass
class PlannedEvent:
    """回填计划中的一条事件（保留结构化中间产物，便于审计与单测断言）。"""

    event: CycleEvent
    ledger: str
    row_id: int
    status: str
    sample_key: str
    seq: int


class MigrationPlanner:
    """从三处台账当前数据构造确定性回填计划（纯计算，不碰存储）。"""

    def __init__(self, data_store: Store) -> None:
        self.store = data_store

    def snapshot_manifest(self) -> dict[str, Any]:
        """来源数据清单：只取决定裁决的字段，作为批次身份与重跑比对依据。"""
        ledgers: dict[str, list[dict[str, Any]]] = {}
        for ledger in CYCLE_LEDGERS:
            items = []
            for row in self.store.rows(ledger):
                item = {
                    "id": int(row.get("id", 0)),
                    "status": str(row.get("status") or ""),
                    "ref": str(row.get(LEDGER_REF_FIELDS[ledger]) or ""),
                }
                if ledger == "core":
                    item["所属钻孔"] = str(row.get("所属钻孔") or "")
                    item["取样深度起"] = str(row.get("取样深度起") or "")
                    item["取样深度止"] = str(row.get("取样深度止") or "")
                items.append(item)
            ledgers[ledger] = items
        return {"version": 1, "ledgers": ledgers}

    def batch_id_for(self, manifest: Mapping[str, Any]) -> str:
        digest = hashlib.sha256(_stable_json(manifest).encode("utf-8")).hexdigest()
        return f"batch-{digest[:16]}"

    def plan(self) -> tuple[str, list[SampleCycle], list[PlannedEvent], list[dict[str, Any]], dict[str, Any]]:
        """返回 (batch_id, 周期集合, 事件计划, 双读差异, 计划统计)。

        样本归并键：
        - 岩心行：岩心编号（周期根）；
        - 送样/化验行：行上的“岩心编号”引用；缺失则退化为 "<模块>#<行id>" 独立周期。
        """
        manifest = self.snapshot_manifest()
        batch_id = self.batch_id_for(manifest)

        cycles: dict[str, SampleCycle] = {}
        planned: list[PlannedEvent] = []
        drifts: list[dict[str, Any]] = []
        seq = 0
        unknown_rows: list[dict[str, Any]] = []

        # 顺序固定：core -> sample_registry -> assay，行按 id 升序，保证事件序确定。
        for ledger in CYCLE_LEDGERS:
            rows = sorted(self.store.rows(ledger), key=lambda r: int(r.get("id", 0)))
            for row in rows:
                row_id = int(row["id"])
                status = str(row.get("status") or "")
                sample_key = sample_key_of(ledger, row)
                if sample_key is None:
                    if ledger == "core":
                        # 岩心行没有岩心编号属于脏数据，无法充当周期根。
                        unknown_rows.append({"ledger": ledger, "row_id": row_id, "status": status})
                        continue
                    sample_key = CycleRepository.synthetic_key(ledger, row_id)

                stage = STAGE_BY_LEDGER_STATUS[ledger].get(status, STATUS_UNKNOWN)
                if stage is STATUS_UNKNOWN:
                    unknown_rows.append({"ledger": ledger, "row_id": row_id, "status": status})
                    continue

                # 未关联到岩心编号的行各自独立成周期（合成键带旧状态），
                # 避免同一台账的两条游离行被错误并成一个样本。
                if sample_key_of(ledger, row) is None and ledger != "core":
                    sample_key = CycleRepository.synthetic_key(ledger, row_id)

                cycle = cycles.setdefault(sample_key, SampleCycle(sample_key))
                cycle.source_rows[ledger] = row_id
                seq += 1
                event = CycleEvent(
                    id=f"{batch_id}:{ledger}:{row_id}",
                    sample_key=sample_key,
                    kind=EVENT_KIND_LEGACY,
                    action="历史裁决",
                    from_stage=stage,
                    to_stage=stage,
                    negative=status in NEGATIVE_STATUS[ledger],
                    ledger=ledger,
                    batch_id=batch_id,
                    seq=seq,
                    occurred_at=MIGRATION_EPOCH,
                    payload={"source_status": status, "ledger": ledger, "row_id": row_id},
                )
                cycle.append(event)
                planned.append(
                    PlannedEvent(event, ledger, row_id, status, sample_key, seq)
                )

                # 双读校验：同一行的旧口径 vs 统一裁决投影，逐条比对，不一致即登记。
                drift = self._compare_row(batch_id, cycle, ledger, row, status)
                if drift is not None:
                    drifts.append(drift)

        stats = {
            "ledgers": {ledger: len(self.store.rows(ledger)) for ledger in CYCLE_LEDGERS},
            "cycles": len(cycles),
            "events": len(planned),
            "drifts": len(drifts),
            "unknown_rows": unknown_rows,
        }
        return batch_id, list(cycles.values()), planned, drifts, stats

    # ----- 双读校验 -------------------------------------------------------

    def _compare_row(
        self,
        batch_id: str,
        cycle: SampleCycle,
        ledger: str,
        row: Mapping[str, Any],
        status: str,
    ) -> dict[str, Any] | None:
        # 用“计划中该周期目前已收集到的事实行”做评估，结论对计划顺序无依赖：
        # LEGACY 事件归并取最远阶段，行数据也全部来自现状。
        facts: dict[str, dict[str, Any] | None] = {name: None for name in CYCLE_LEDGERS}
        for name in CYCLE_LEDGERS:
            row_id = cycle.source_rows.get(name)
            facts[name] = self.store.find(name, row_id) if row_id is not None else None
        assessment = assess_cycle(cycle, facts)
        verdict = assessment.verdicts[ledger]
        # 旧口径以旧服务实际返回为准：直接读取行上的 status/pending/abnormal 存储值。
        legacy_pending = bool(row.get("pending", False))
        legacy_abnormal = bool(row.get("abnormal", False))

        mismatches: dict[str, Any] = {}
        if verdict.status is not None and verdict.status != status:
            mismatches["status"] = {"legacy": status, "cycle": verdict.status}
        if verdict.pending != legacy_pending:
            mismatches["pending"] = {"legacy": legacy_pending, "cycle": verdict.pending}
        if verdict.abnormal != legacy_abnormal:
            mismatches["abnormal"] = {"legacy": legacy_abnormal, "cycle": verdict.abnormal}
        if not mismatches:
            return None

        drift_key = hashlib.sha256(
            _stable_json(
                {
                    "batch_id": batch_id,
                    "ledger": ledger,
                    "row_id": row["id"],
                    "sample_key": cycle.sample_key,
                    "mismatches": mismatches,
                }
            ).encode("utf-8")
        ).hexdigest()[:16]
        return {
            "drift_key": drift_key,
            "batch_id": batch_id,
            "ledger": ledger,
            "row_id": int(row["id"]),
            "sample_key": cycle.sample_key,
            "legacy_status": status,
            "cycle_stage": assessment.stage,
            "mismatches": mismatches,
            "observed_at": MIGRATION_EPOCH,
        }

    def shadow_check(self, ledger: str, row: Mapping[str, Any]) -> dict[str, Any] | None:
        """shadow 模式在线写入后的双读校验：旧行口径 vs 周期统一裁决。

        以当前三处台账事实即时构造一次只读计划，不落周期数据；差异内容键含
        当前行状态，状态推进后会自然形成新的留痕。
        """
        shadow_id = "shadow"
        # 按引用把三处关联行收集到一个临时周期里（只读计算）。
        from app.domain.sample_cycle.cycle_store import CycleRepository

        ref = sample_key_of(ledger, row)
        key = ref if ref is not None else CycleRepository.synthetic_key(ledger, int(row["id"]))
        cycle = SampleCycle(key)
        for name in CYCLE_LEDGERS:
            for candidate in sorted(self.store.rows(name), key=lambda r: int(r.get("id", 0))):
                candidate_key = sample_key_of(name, candidate)
                if candidate_key is None and name == "core":
                    continue
                candidate_key = candidate_key or CycleRepository.synthetic_key(
                    name, int(candidate["id"])
                )
                if candidate_key == key:
                    status = str(candidate.get("status") or "")
                    stage = STAGE_BY_LEDGER_STATUS[name].get(status)
                    cycle.source_rows[name] = int(candidate["id"])
                    if stage is not None:
                        cycle.append(
                            CycleEvent(
                                id=f"shadow:{name}:{candidate['id']}:{status}",
                                sample_key=key,
                                kind=EVENT_KIND_LEGACY,
                                action="历史裁决",
                                from_stage=stage,
                                to_stage=stage,
                                negative=status in NEGATIVE_STATUS[name],
                                ledger=name,
                                batch_id=shadow_id,
                                seq=0,
                                occurred_at=MIGRATION_EPOCH,
                            )
                        )
        drift = self._compare_row(shadow_id, cycle, ledger, row, str(row.get("status") or ""))
        if drift is not None:
            drift["batch_id"] = shadow_id
            drift["observed_at"] = utc_now_iso()
        return drift


class CycleMigrator:
    """回填/回滚批次的执行器；每个公开方法自身是一个完整事务。"""

    def __init__(self, data_store: Store, repository: CycleRepository) -> None:
        self.store = data_store
        self.repo = repository
        self.planner = MigrationPlanner(data_store)

    def run(self, *, project: bool = False) -> dict[str, Any]:
        """执行回填。已存在的相同批次直接幂等返回，不产生任何写入。"""
        batch_id, cycles, planned, drifts, stats = self.planner.plan()

        existing = self.repo.find_batch(batch_id)
        if existing is not None:
            return {"rerun": True, **existing}

        # 全部计划与校验完成后才进入事务：事务内只做落库，不做可能失败的计算。
        with self.store.transaction():
            for cycle in cycles:
                self.repo.save_cycle(cycle)
            for drift in drifts:
                self.repo.record_drift(drift)
            projected: list[dict[str, Any]] = []
            snapshots: dict[str, dict[int, dict[str, Any]]] = {}
            if project:
                projected, snapshots = self._project_all(cycles, batch_id)
            batch = {
                "batch_id": batch_id,
                "status": "completed",
                "ran_at": MIGRATION_EPOCH,
                "mode": "backfill_and_project" if project else "backfill",
                "stats": stats,
                "drift_keys": [drift["drift_key"] for drift in drifts],
                "projected": projected,
                "projection_snapshots": snapshots,
            }
            self.repo.save_batch(batch)
        return {"rerun": False, **batch}

    def project_batch(self, batch_id: str) -> dict[str, Any]:
        """把某回填批次的统一裁决回写三处台账（切流时使用，幂等）。

        回写前快照三处行的可投影字段，回滚批次时据此逐字段还原。
        """
        batch = self.repo.find_batch(batch_id)
        if batch is None:
            raise CommandRejected(f"回填批次 {batch_id} 不存在，无法回写")
        with self.store.transaction():
            cycles = [
                cycle for cycle in self.repo.list_cycles()
                if any(event.batch_id == batch_id for event in cycle.events)
            ]
            projected, snapshots = self._project_all(cycles, batch_id)
            batch["projected"] = projected
            batch["projection_snapshots"] = snapshots
            batch["mode"] = "backfill_and_project"
            self.repo.save_batch(batch)
        return batch

    def rollback(self, batch_id: str) -> dict[str, Any]:
        """回滚批次：摘除投影并删除其产生的周期/事件。

        - 只被该批次事件覆盖的周期整体删除；
        - 周期上若有在线 COMMAND 事件则拒绝回滚（避免抹掉切换后的真实业务）；
        - 投影字段按回写前快照逐行还原；
        - 旧行原始业务字段在回填阶段从未被改写，这里只还原投影过的键。
        """
        batch = self.repo.find_batch(batch_id)
        if batch is None:
            raise CommandRejected(f"回填批次 {batch_id} 不存在，无需回滚")

        with self.store.transaction():
            blocked: list[str] = []
            for cycle in self.repo.list_cycles():
                batch_events = [event for event in cycle.events if event.batch_id == batch_id]
                if not batch_events:
                    continue
                online = [
                    event for event in cycle.events
                    if event.batch_id is None and event.kind != "LEGACY"
                ]
                if online:
                    blocked.append(cycle.sample_key)
            if blocked:
                raise CommandRejected(
                    "批次已有在线业务事件，拒绝回滚：" + "、".join(sorted(blocked)[:10])
                )

            restored = self._restore_snapshots(batch.get("projection_snapshots") or {})

            for cycle in self.repo.list_cycles():
                if any(event.batch_id == batch_id for event in cycle.events):
                    self.repo.delete_cycle(cycle.sample_key)
            drift_table = self.store.rows("_sample_cycle_drifts")
            drift_table[:] = [row for row in drift_table if row.get("batch_id") != batch_id]
            self.repo.delete_batch(batch_id)
            result = {"rollback": batch_id, "restored": restored, "status": "rolled_back"}
        return result

    # ----- 内部工具 -------------------------------------------------------

    def _project_all(
        self, cycles: list[SampleCycle], batch_id: str
    ) -> tuple[list[dict[str, Any]], dict[str, dict[int, dict[str, Any]]]]:
        # 快照必须在投影前逐行抓取（回滚按此还原）；已抓过的行保留首次快照。
        snapshots: dict[str, dict[int, dict[str, Any]]] = {}
        restore_keys = ADDITIVE_KEYS + ("status", "pending", "abnormal")
        for cycle in sorted(cycles, key=lambda item: item.sample_key):
            facts = self.repo.load_fact_rows(cycle)
            for ledger, fact in facts.items():
                if fact is None or int(fact["id"]) in snapshots.setdefault(ledger, {}):
                    continue
                snapshots[ledger][int(fact["id"])] = {
                    key: fact.get(key) for key in restore_keys
                }

        projected: list[dict[str, Any]] = []
        for cycle in sorted(cycles, key=lambda item: item.sample_key):
            outcome = self.repo.project_cycle(cycle, synced_at=MIGRATION_EPOCH)
            outcome["batch_id"] = batch_id
            projected.append(outcome)
        return projected, snapshots

    def _restore_snapshots(
        self, snapshots: Mapping[str, Mapping[str, Mapping[str, Any]]]
    ) -> list[dict[str, Any]]:
        restored: list[dict[str, Any]] = []
        for ledger, by_id in snapshots.items():
            for row_id, values in by_id.items():
                row = self.store.find(ledger, int(row_id))
                if row is None:
                    continue
                for key, value in values.items():
                    if value is None:
                        row.pop(key, None)
                    else:
                        row[key] = value
                restored.append({"ledger": ledger, "row_id": int(row_id)})
        return restored
