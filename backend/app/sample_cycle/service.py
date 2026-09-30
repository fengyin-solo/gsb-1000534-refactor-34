"""样本周期应用服务：领域能力对外的唯一入口。

事务边界（明确划定，避免事件库与三台账出现跨存储不一致）：

* 一条命令 = 一个事务：合法性校验 → 追加事件 → 归约阶段 → 三台账投影回写，
  任一步失败整体回滚，调用方看不到半成品；
* 一个迁移批次 = 一个事务：先在事务外完成确定性推导（plan），事务内只做
  幂等检查、事件追加与回写；任一周期失败整批回滚，绝不部分写入；
* 回滚批次 = 一个事务：仅删除该批次写入的事件并反向投影，要求事件链
  未在批次之后继续推进，否则拒绝（需要补偿事件而非删历史）。

灰度（可灰度、可回滚）：

* MODE_LEGACY  —— 旧路径独占，领域不介入（默认，上线即兼容）；
* MODE_SHADOW  —— 旧路径照常写；命令同步在领域侧试跑，只记录双读差异，
  不回写台账；迁移回填可在该模式下做，回写仅投影到已接管周期；
* MODE_DUAL    —— 新路径为主：事件落库 + 三台账回写；旧字段同步维护，
  双读持续校验；
* MODE_ROLLBACK —— 退回旧路径独占，并按批次撤销领域写入。

所有模式切换都有审计事件留痕（sample_cycle_meta 表）。
"""
from __future__ import annotations

from typing import Any

from app.sample_cycle import projection
from app.sample_cycle.events import (
    BATCH_BACKFILL,
    COMMAND_EVENTS,
    LEDGER_ASSAY,
    LEDGER_CORE,
    LEDGER_DISPATCH,
    LEDGERS,
    MIGRATION_BASELINE,
    MIGRATION_EVENTS,
    ROLLBACK_BATCH,
    SET_MODE,
    CycleEvent,
    make_event_id,
)
from app.sample_cycle.migration import build_batch_plan, migration_event_specs
from app.sample_cycle.policy import (
    FIRST_EVENT,
    PolicyViolation,
    availability_triple,
    legacy_ruling,
    resolve_transition,
)
from app.store import Store

# 灰度模式
MODE_LEGACY = "legacy"
MODE_SHADOW = "shadow"
MODE_DUAL = "dual"
MODES = (MODE_LEGACY, MODE_SHADOW, MODE_DUAL)

# 领域使用的表名
T_CYCLES = "sample_cycle"
T_EVENTS = "sample_cycle_event"
T_PROJECTIONS = "sample_cycle_projection"   # 周期 -> 三台账行的回写绑定
T_META = "sample_cycle_meta"                # 灰度模式与批次审计
T_RECONCILE = "sample_cycle_reconcile"      # 双读校验结果留痕

# 既有三台账
LEGACY_TABLES = {
    LEDGER_CORE: "core",
    LEDGER_DISPATCH: "sample_registry",
    LEDGER_ASSAY: "assay",
}

DEFAULT_BATCH_ID = "baseline-2026-09-30"


class SampleCycleService:
    def __init__(self, store: Store) -> None:
        self._store = store

    # ------------------------------------------------------------------ 模式

    def get_mode(self) -> str:
        rows = self._store.rows(T_META)
        for row in reversed(rows):
            if row.get("key") == "mode":
                return str(row["value"])
        return MODE_LEGACY

    def set_mode(self, mode: str, *, actor: str = "system") -> dict[str, Any]:
        if mode not in MODES:
            raise ValueError(f"未知灰度模式：{mode}")
        with self._store.transaction():
            previous = self.get_mode()
            if mode == previous:
                return {"ok": True, "mode": mode, "changed": False}
            self._store.rows(T_META).append({
                "id": self._store.next_id(T_META),
                "key": "mode",
                "value": mode,
                "previous": previous,
                "actor": actor,
                "audit_event": SET_MODE,
            })
        return {"ok": True, "mode": mode, "previous": previous, "changed": True}

    def mode_history(self) -> list[dict[str, Any]]:
        return list(self._store.rows(T_META))

    # ------------------------------------------------------------ shadow 试跑

    def dry_run_command(self, cycle_id: int, event_type: str) -> dict[str, Any]:
        """不落库、不回写地验证命令在当前阶段是否合法（shadow 模式用）。"""
        cycle = self.get_cycle(cycle_id)
        if cycle is None:
            return {"allowed": False, "reason": f"样本周期 {cycle_id} 不存在"}
        from_stage = str(cycle["stage"])
        try:
            to_stage, group, note = resolve_transition(from_stage, event_type)
        except PolicyViolation as exc:
            return {"allowed": False, "reason": str(exc)}
        return {
            "allowed": True,
            "from_stage": from_stage,
            "to_stage": to_stage,
            "owner_group": group,
            "note": note,
        }

    # -------------------------------------------------------------- 查询

    def list_cycles(self) -> list[dict[str, Any]]:
        return list(self._store.rows(T_CYCLES))

    def get_cycle(self, cycle_id: int) -> dict[str, Any] | None:
        return self._store.find(T_CYCLES, cycle_id)

    def current_stage(self, cycle_id: int) -> str | None:
        cycle = self.get_cycle(cycle_id)
        return None if cycle is None else str(cycle["stage"])

    def events(self, cycle_id: int) -> list[dict[str, Any]]:
        rows = [e for e in self._store.rows(T_EVENTS) if int(e["cycle_id"]) == cycle_id]
        return sorted(rows, key=lambda event: int(event["seq"]))

    def availability(self, cycle_id: int) -> dict[str, Any] | None:
        stage = self.current_stage(cycle_id)
        if stage is None:
            return None
        return {"cycle_id": cycle_id, "stage": stage, **availability_triple(stage)}

    def find_cycle_by_ledger_row(self, ledger: str, row_id: int) -> dict[str, Any] | None:
        """供旧服务适配：按台账行 id 找到它归属的周期。"""
        for binding in self._store.rows(T_PROJECTIONS):
            if binding.get(ledger) == row_id:
                return self.get_cycle(int(binding["cycle_id"]))
        return None

    def attach_ledger_row(self, cycle_id: int, ledger: str, row_id: int) -> None:
        """把既有送样清单行/化验待办行挂到周期上，并立即按当前阶段投影一次。

        迁移期挂接来自编号关联（migration 模块）；运行期挂接来自送样清单/
        化验待办新建登记行。挂接是纯绑定，不制造业务事件。
        """
        if ledger not in LEDGERS:
            raise PolicyViolation(f"未知台账：{ledger}")
        with self._store.transaction():
            cycle = self.get_cycle(cycle_id)
            if cycle is None:
                raise PolicyViolation(f"样本周期 {cycle_id} 不存在")
            self._bind_rows(cycle_id, {ledger: row_id})
            self._project_all(cycle_id, str(cycle["stage"]))

    # ------------------------------------------------------------------ 注册

    def register_from_core(
        self,
        core_row_id: int,
        sample_code: str,
        *,
        borehole: str = "",
        depth_from: Any = None,
        depth_to: Any = None,
    ) -> dict[str, Any]:
        """岩心台账新建样本时建立周期（首事件：已登记）。幂等：同岩心行只建一次。"""
        with self._store.transaction():
            existing = self.find_cycle_by_ledger_row(LEDGER_CORE, core_row_id)
            if existing is not None:
                return existing
            cycle_id = self._store.next_id(T_CYCLES)
            event_type, stage, group = FIRST_EVENT
            self._append_event(
                cycle_id=cycle_id,
                event_type=event_type,
                stage=stage,
                ledger=LEDGER_CORE,
                payload={
                    "sample_code": sample_code,
                    "core_row_id": core_row_id,
                    "所属钻孔": borehole,
                    "取样深度起": depth_from,
                    "取样深度止": depth_to,
                    "owner_group": group,
                },
            )
            cycle = {
                "id": cycle_id,
                "sample_code": sample_code,
                "stage": stage,
                "core_row_id": core_row_id,
            }
            self._store.upsert(T_CYCLES, cycle)
            self._bind_rows(cycle_id, {LEDGER_CORE: core_row_id})
            self._project_all(cycle_id, stage)
            return dict(cycle)

    # -------------------------------------------------------------- 命令入口

    def apply_command(
        self,
        cycle_id: int,
        event_type: str,
        *,
        ledger: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """在周期上追加一个业务事件（幂等），并原子回写三台账。

        成功返回 ``{ok, cycle, event, stage}``；阶段不合法抛
        :class:`PolicyViolation`，事务回滚，三台账保持原状。
        """
        if event_type not in COMMAND_EVENTS:
            raise PolicyViolation(f"「{event_type}」不是样本周期可识别的业务事件")
        with self._store.transaction():
            cycle = self.get_cycle(cycle_id)
            if cycle is None:
                raise PolicyViolation(f"样本周期 {cycle_id} 不存在")
            from_stage = str(cycle["stage"])
            candidate = self._new_event(
                cycle_id=cycle_id,
                event_type=event_type,
                stage="",  # 占位；下面按幂等键预演目标阶段用于自然键匹配
                ledger=ledger,
                payload={**(payload or {}), "from_stage": from_stage},
                idempotency_key=idempotency_key,
            )
            # 幂等查重必须先于阶段合法性：同业务意图重放时阶段可能已经前进，
            # 此时应返回既有事件，而不是报「当前阶段不能执行」。
            # 带幂等号的请求按 (幂等号, 事件) 直接匹配首次事件；
            # 不带幂等号的重复请求按 (事件, 当前阶段) 匹配同一裁决。
            if idempotency_key is not None:
                existing = self._find_by_idempotency_key(cycle_id, event_type, idempotency_key)
            else:
                candidate.stage = from_stage
                existing = self._find_by_natural_key(cycle_id, candidate.natural_key())
            if existing is not None:
                return {
                    "ok": True,
                    "idempotent": True,
                    "cycle": dict(cycle),
                    "event": existing.to_dict(),
                    "stage": str(cycle["stage"]),
                }
            to_stage, group, note = resolve_transition(from_stage, event_type)
            candidate.stage = to_stage
            candidate.payload["owner_group"] = group
            candidate.payload["note"] = note
            candidate.event_id = make_event_id(candidate)
            self._store.rows(T_EVENTS).append(candidate.to_dict())
            cycle["stage"] = to_stage
            self._project_all(cycle_id, to_stage)
            return {
                "ok": True,
                "idempotent": False,
                "cycle": dict(cycle),
                "event": candidate.to_dict(),
                "stage": to_stage,
            }

    # ------------------------------------------------------------ 迁移回填

    def preview_backfill(self, batch_id: str = DEFAULT_BATCH_ID) -> dict[str, Any]:
        """只推导不落库：灰度前人工核对矛盾样本与未关联台账行。"""
        plan = self._build_plan(batch_id)
        return plan.to_dict()

    def run_backfill(
        self,
        batch_id: str = DEFAULT_BATCH_ID,
        *,
        strict: bool = True,
        actor: str = "migration",
    ) -> dict[str, Any]:
        """执行迁移回填。

        * 一个批次一个事务，任何周期失败 → 整批回滚（strict 下连矛盾样本
          也算失败，必须先在 preview 里处置；strict=False 允许带矛盾迁移，
          矛盾会在双读校验里持续暴露）；
        * 批次幂等：已含该批次事件的周期整体跳过，重跑同一批次输出一致。
        """
        plan = self._build_plan(batch_id)
        result: dict[str, Any] = {
            "batch_id": batch_id,
            "planned": len(plan.cycles),
            "migrated": 0,
            "skipped": 0,
            "divergent": [],
            "unlinked": plan.unlinked,
            "cycles": [],
        }
        if strict and any(cycle.divergent for cycle in plan.cycles):
            result["ok"] = False
            result["reason"] = "存在三台账确认阶段互相矛盾的样本，strict 模式拒绝迁移"
            result["divergent"] = [
                {"core_row_id": c.core_row_id, "detail": c.divergence_detail}
                for c in plan.cycles if c.divergent
            ]
            return result

        with self._store.transaction():
            for cycle_plan in plan.cycles:
                outcome = self._migrate_one(cycle_plan, batch_id, strict=strict)
                result["cycles"].append(outcome)
                result["migrated" if outcome["migrated"] else "skipped"] += 1
                if outcome.get("divergent"):
                    result["divergent"].append({
                        "cycle_id": outcome.get("cycle_id"),
                        "core_row_id": cycle_plan.core_row_id,
                        "detail": cycle_plan.divergence_detail,
                    })
            self._store.rows(T_META).append({
                "id": self._store.next_id(T_META),
                "key": "batch",
                "value": batch_id,
                "audit_event": BATCH_BACKFILL,
                "actor": actor,
                "migrated": result["migrated"],
                "skipped": result["skipped"],
            })
        result["ok"] = True
        return result

    def rollback_backfill(self, batch_id: str, *, actor: str = "migration") -> dict[str, Any]:
        """撤销一个迁移批次：删除其事件、解除绑定并把台账行回滚到批次前镜像。

        批次之后若已有业务事件推进，拒绝回滚（返回 ok=False），
        因为业务事实不允许删除，应改用补偿命令。
        """
        with self._store.transaction():
            event_rows = self._store.rows(T_EVENTS)
            batch_events = [e for e in event_rows if e.get("batch_id") == batch_id]
            if not batch_events:
                return {"ok": False, "reason": f"批次 {batch_id} 不存在或已回滚"}
            cycle_ids = {int(e["cycle_id"]) for e in batch_events}
            # seq 按周期独立分配，后续业务事件检查也要按周期比较：
            # 某周期在该批次事件之后是否又出现业务事件（或更新批次的迁移事件）。
            blocking: list[str] = []
            for cid in cycle_ids:
                cycle_events = sorted(
                    (e for e in event_rows if int(e["cycle_id"]) == cid),
                    key=lambda e: int(e["seq"]),
                )
                last_batch_seq = max(
                    int(e["seq"]) for e in cycle_events if e.get("batch_id") == batch_id
                )
                blocking.extend(
                    e["event_id"] for e in cycle_events
                    if e.get("batch_id") != batch_id and int(e["seq"]) > last_batch_seq
                )
            if blocking:
                return {
                    "ok": False,
                    "reason": "批次之后已有业务事件推进，不能删除历史，请改用补偿命令",
                    "blocking_events": blocking,
                }
            # 恢复回写前镜像（镜像按周期绑定，与批次无关）
            for binding in list(self._store.rows(T_PROJECTIONS)):
                if int(binding["cycle_id"]) not in cycle_ids:
                    continue
                for ledger in LEDGERS:
                    row_id = binding.get(ledger)
                    before = (binding.get("before") or {}).get(ledger)
                    if row_id is None or before is None:
                        continue
                    table = LEGACY_TABLES[ledger]
                    target = self._store.find(table, int(row_id))
                    if target is not None:
                        target.clear()
                        target.update(before)
            # 先删本批次事件
            event_rows[:] = [e for e in event_rows if e.get("batch_id") != batch_id]
            # 再判定本批次是否拥有这些周期（首个基线批次）；后续批次仅留痕
            remaining = event_rows
            owning = set()
            for e in batch_events:
                if e["event_type"] != MIGRATION_BASELINE:
                    continue
                cid = int(e["cycle_id"])
                has_earlier_baseline = any(
                    r.get("batch_id") is not None
                    and r.get("event_type") == MIGRATION_BASELINE
                    and int(r["cycle_id"]) == cid
                    for r in remaining
                )
                if not has_earlier_baseline:
                    owning.add(cid)
            if owning:
                self._store.rows(T_PROJECTIONS)[:] = [
                    b for b in self._store.rows(T_PROJECTIONS)
                    if int(b["cycle_id"]) not in owning
                ]
                self._store.rows(T_CYCLES)[:] = [
                    c for c in self._store.rows(T_CYCLES) if int(c["id"]) not in owning
                ]
            self._store.rows(T_META).append({
                "id": self._store.next_id(T_META),
                "key": "rollback",
                "value": batch_id,
                "audit_event": ROLLBACK_BATCH,
                "actor": actor,
            })
        return {"ok": True, "batch_id": batch_id, "rolled_back_cycles": sorted(cycle_ids)}

    # -------------------------------------------------------------- 双读校验

    def reconcile(self, *, persist: bool = True) -> dict[str, Any]:
        """双读校验：同一周期上比对「领域结论」与「三台账当前裁决」。

        shadow / dual 模式回填期间都用它发现分歧；只有读、不修正数据。
        判定标准：三台账行的可用性字段（新模式回写）或旧口径推导
        （未接管行）必须与领域 ``availability_triple`` 完全一致。
        """
        findings: list[dict[str, Any]] = []
        for cycle in self.list_cycles():
            stage = str(cycle["stage"])
            expected = availability_triple(stage)
            binding = self._binding(int(cycle["id"]))
            for ledger in LEDGERS:
                row_id = (binding or {}).get(ledger)
                if row_id is None:
                    continue
                row = self._store.find(LEGACY_TABLES[ledger], int(row_id))
                if row is None:
                    findings.append({
                        "cycle_id": cycle["id"], "ledger": ledger,
                        "kind": "missing_row", "expected": expected[ledger],
                    })
                    continue
                actual = row.get("样本可用性") or legacy_ruling(
                    ledger, str(row.get("status", ""))
                )["legacy_availability"]
                if actual != expected[ledger]:
                    findings.append({
                        "cycle_id": cycle["id"],
                        "ledger": ledger,
                        "kind": "verdict_mismatch",
                        "stage": stage,
                        "expected": expected[ledger],
                        "actual": actual,
                    })
            # 三台账之间互相也要一致地「同源」：旧确认阶段反推的阶段若走在
            # 领域阶段前面/后面，同样是分歧（迁移矛盾样本会在这里持续报红）。
            if binding and binding.get("divergent"):
                findings.append({
                    "cycle_id": cycle["id"],
                    "kind": "legacy_divergence",
                    "detail": binding.get("divergence_detail"),
                })
        report = {
            "mode": self.get_mode(),
            "checked_cycles": len(self.list_cycles()),
            "mismatch_count": len(findings),
            "consistent": not findings,
            "findings": findings,
        }
        if persist:
            self._store.rows(T_RECONCILE).append({
                "id": self._store.next_id(T_RECONCILE),
                **report,
            })
        return report

    # -------------------------------------------------------------- 内部实现

    def _build_plan(self, batch_id: str):
        return build_batch_plan(
            list(self._store.rows(LEGACY_TABLES[LEDGER_CORE])),
            list(self._store.rows(LEGACY_TABLES[LEDGER_DISPATCH])),
            list(self._store.rows(LEGACY_TABLES[LEDGER_ASSAY])),
            batch_id,
        )

    def _migrate_one(self, cycle_plan, batch_id: str, *, strict: bool = True) -> dict[str, Any]:
        """单周期迁移。周期以岩心主档为锚点唯一；批次按 batch_id 幂等。

        * 已含同批次事件：整体跳过；
        * 已建周期但属于新批次：只补本批次的旧裁决/基线留痕，不重建周期、
          不改变阶段（基线永远以第一次确认为准）。
        """
        bindings = self._store.rows(T_PROJECTIONS)
        binding = next(
            (b for b in bindings if b.get(LEDGER_CORE) == cycle_plan.core_row_id),
            None,
        )
        # 无论周期归属哪个批次，只要本批次已经为该周期写过基线事件即跳过
        already_in_batch = any(
            int(e.get("cycle_id", -1)) == int(binding["cycle_id"])
            and e.get("batch_id") == batch_id
            for e in self._store.rows(T_EVENTS)
        ) if binding is not None else False
        if already_in_batch:
            return {
                "core_row_id": cycle_plan.core_row_id,
                "migrated": False,
                "skipped": True,
                "cycle_id": int(binding["cycle_id"]),
                "divergent": bool(binding.get("divergent")),
            }

        binding_rows: dict[str, int] = {}
        before: dict[str, dict[str, Any]] = {}
        for link in cycle_plan.links:
            binding_rows[link.ledger] = link.row_id
            target = self._store.find(LEGACY_TABLES[link.ledger], link.row_id)
            if target is not None:
                before[link.ledger] = dict(target)

        if binding is None:
            cycle_id = self._store.next_id(T_CYCLES)
            self._store.upsert(T_CYCLES, {
                "id": cycle_id,
                "sample_code": cycle_plan.sample_code,
                "stage": cycle_plan.baseline_stage,
                "core_row_id": cycle_plan.core_row_id,
            })
            binding = {"cycle_id": cycle_id, "batch_id": batch_id, **binding_rows, "before": before}
            bindings.append(binding)
            if not strict:
                binding["divergent"] = cycle_plan.divergent
                binding["divergence_detail"] = cycle_plan.divergence_detail
            baseline_stage = cycle_plan.baseline_stage
            created = True
        else:
            # 已存在周期：新批次仅留痕，阶段与绑定维持首次迁移结论
            cycle_id = int(binding["cycle_id"])
            baseline_stage = str(self.get_cycle(cycle_id)["stage"])
            created = False

        specs = migration_event_specs(
            cycle_id,
            cycle_plan,
            batch_id,
            stage_override=baseline_stage,
        )
        for spec in specs:
            self._append_event(**spec)

        if created:
            self._project_all(cycle_id, baseline_stage)
        return {
            "core_row_id": cycle_plan.core_row_id,
            "migrated": True,
            "skipped": False,
            "created": created,
            "cycle_id": cycle_id,
            "stage": baseline_stage,
            "divergent": cycle_plan.divergent if not strict else False,
        }

    def _append_event(self, **spec: Any) -> dict[str, Any]:
        cycle_id = int(spec["cycle_id"])
        event = self._new_event(**spec)
        if event.event_type in MIGRATION_EVENTS:
            event.event_id = make_event_id(event)
            duplicate = next(
                (e for e in self._store.rows(T_EVENTS) if e["event_id"] == event.event_id),
                None,
            )
            if duplicate is not None:
                return duplicate
        else:
            event.event_id = make_event_id(event)
        self._store.rows(T_EVENTS).append(event.to_dict())
        return event.to_dict()

    def _new_event(self, **spec: Any) -> CycleEvent:
        events = self._store.rows(T_EVENTS)
        cycle_id = int(spec["cycle_id"])
        seq = max(
            (int(e["seq"]) for e in events if int(e["cycle_id"]) == cycle_id),
            default=0,
        ) + 1
        return CycleEvent(
            seq=seq,
            cycle_id=cycle_id,
            event_type=str(spec["event_type"]),
            stage=str(spec.get("stage", "")),
            ledger=str(spec.get("ledger", "")),
            payload=dict(spec.get("payload") or {}),
            idempotency_key=spec.get("idempotency_key"),
            batch_id=spec.get("batch_id"),
            occurred_at=len(events) + 1,
        )

    def _find_by_natural_key(
        self, cycle_id: int, natural_key: str
    ) -> CycleEvent | None:
        for row in self.events(cycle_id):
            event = CycleEvent.from_dict(row)
            # 无显式幂等号的命令重放只匹配同样无号的事件，避免误伤带号请求
            if event.idempotency_key is None and event.natural_key() == natural_key:
                return event
        return None

    def _find_by_idempotency_key(
        self, cycle_id: int, event_type: str, key: str
    ) -> CycleEvent | None:
        for row in self.events(cycle_id):
            if row.get("event_type") == event_type and row.get("idempotency_key") == key:
                return CycleEvent.from_dict(row)
        return None

    def _binding(self, cycle_id: int) -> dict[str, Any] | None:
        return next(
            (b for b in self._store.rows(T_PROJECTIONS) if int(b["cycle_id"]) == cycle_id),
            None,
        )

    def _bind_rows(self, cycle_id: int, rows: dict[str, int]) -> None:
        bindings = self._store.rows(T_PROJECTIONS)
        binding = self._binding(cycle_id) or {"cycle_id": cycle_id, "before": {}}
        for ledger, row_id in rows.items():
            binding[ledger] = row_id
            table = self._store.rows(LEGACY_TABLES[ledger])
            target = next((r for r in table if int(r.get("id", 0)) == row_id), None)
            if target is not None and ledger not in binding["before"]:
                binding["before"][ledger] = dict(target)
        if binding not in bindings:
            bindings.append(binding)

    def _project_all(self, cycle_id: int, stage: str) -> None:
        """把阶段结论回写到已绑定的三台账行；未绑定（未物化）的行不创建。"""
        binding = self._binding(cycle_id)
        if binding is None:
            return
        core_id = binding.get(LEDGER_CORE)
        if core_id is not None:
            row = self._store.find(LEGACY_TABLES[LEDGER_CORE], int(core_id))
            if row is not None:
                projection.project_core(row, stage)
        dispatch_id = binding.get(LEDGER_DISPATCH)
        if dispatch_id is not None:
            row = self._store.find(LEGACY_TABLES[LEDGER_DISPATCH], int(dispatch_id))
            if row is not None:
                projection.project_dispatch(row, stage)
        assay_id = binding.get(LEDGER_ASSAY)
        if assay_id is not None:
            row = self._store.find(LEGACY_TABLES[LEDGER_ASSAY], int(assay_id))
            if row is not None:
                projection.project_assay(row, stage)


from app.store import store


# 单例：与 store 单例绑定，旧服务通过它接入统一领域
sample_cycle_service = SampleCycleService(store)
