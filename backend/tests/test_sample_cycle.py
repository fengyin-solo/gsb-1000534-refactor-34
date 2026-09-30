"""样本周期重构测试：灰度三模式、幂等事件、事务边界、确定性回填与回滚。

直接用 TestClient 打既有接口（接口形态必须不变），并对领域层做单元验证。
每个用例重建 store 与门面，避免内存状态串扰。
"""
from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    # 确保所有模块按干净的 store 与默认 off 模式重新装配。
    for name in list(sys.modules):
        if name.startswith("app."):
            del sys.modules[name]
    monkeypatch.delenv("SAMPLE_CYCLE_MODE", raising=False)
    from app.main import app  # noqa: E402

    with TestClient(app) as test_client:
        yield test_client


def _post_action(client: TestClient, module: str, entry_id: int, action: str, **extra):
    payload = {"action": action, **extra}
    return client.post(f"/api/{module}/{entry_id}/actions", json={"values": payload})


# ---------------------------------------------------------------------------
# off 模式：行为与重构前逐字节等价
# ---------------------------------------------------------------------------


def test_off_mode_keeps_legacy_behavior(client: TestClient):
    core = client.get("/api/core").json()
    assert [item["status"] for item in core["items"]] == ["待编录", "已编录", "送样中"]
    assert "cycle_stage" not in core["items"][0]

    resp = _post_action(client, "core", 1, "地质编录")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["message"] == "岩心样本已地质编录"
    assert client.get("/api/core/1").json()["status"] == "已编录"


def test_off_mode_unknown_action_message_unchanged(client: TestClient):
    body = _post_action(client, "core", 1, "审核通过").json()
    assert body["ok"] is False
    assert body["message"] == "动作「审核通过」不属于岩心管理可执行范围"


def test_overview_modules_unchanged(client: TestClient):
    overview = client.get("/api/overview").json()
    assert overview["cards"][0]["value"] == 18
    names = {item["name"] for item in overview["modules"]}
    assert not any(name.startswith("_sample") for name in names)


# ---------------------------------------------------------------------------
# shadow 模式：旧路径生效，双读差异留痕，返回不被改写
# ---------------------------------------------------------------------------


def test_shadow_mode_records_drift_without_changing_response(client: TestClient):
    client.post("/api/sample-cycles/mode", json={"values": {"mode": "shadow"}})
    body = _post_action(client, "assay", 3, "退回修改").json()
    assert body["ok"] is True
    assert body["entry"]["status"] == "已退回"

    drifts = client.get("/api/sample-cycles/drifts").json()["items"]
    assert any(d["ledger"] == "assay" and d["row_id"] == 3 for d in drifts)
    # shadow 不建立周期、不投影
    assert client.get("/api/sample-cycles").json()["total"] == 0


# ---------------------------------------------------------------------------
# 回填：确定性、幂等、双读校验
# ---------------------------------------------------------------------------


def test_backfill_is_deterministic_and_idempotent(client: TestClient):
    first = client.post("/api/sample-cycles/backfill", json={"values": {}}).json()
    assert first["ok"] is True
    batch_a = first["entry"]
    second = client.post("/api/sample-cycles/backfill", json={"values": {}}).json()
    assert second["entry"]["batch_id"] == batch_a["batch_id"]
    assert second["message"] == "回填批次已存在，重跑结果一致"

    cycles = client.get("/api/sample-cycles").json()["items"]
    linked = next(c for c in cycles if c["sample_key"] == "CORE-0003")
    assert linked["current_stage"] == "ASSAY_APPROVED"
    assert set(linked["source_rows"]) == {"core", "sample_registry", "assay"}
    # 历史按原裁决留痕：一条已确认阶段一条 LEGACY 事件，且不可变
    assert all(event["kind"] == "LEGACY" for event in linked["events"])

    # 三处已确认阶段机械转写后口径一致（历史按原裁决留痕），
    # 若有差异也会登记在 /drifts；当前种子数据下 CORE-0003 三处结论一致。
    drifts = client.get("/api/sample-cycles/drifts").json()["items"]
    linked_drifts = [d for d in drifts if d["sample_key"] == "CORE-0003"]
    assert linked_drifts == []


def test_backfill_then_project_and_rollback_roundtrip(client: TestClient):
    batch = client.post("/api/sample-cycles/backfill", json={"values": {}}).json()["entry"]
    _set_on(client)
    client.post(f"/api/sample-cycles/batches/{batch['batch_id']}/project")

    assay3 = client.get("/api/assay/3").json()
    assert assay3["cycle_stage"] == "ASSAY_APPROVED"
    assert assay3["pending"] is False  # 统一裁决覆盖旧口径

    # 回滚必须在旧路径下进行（回滚后 on 视图也失去周期来源）
    client.post("/api/sample-cycles/mode", json={"values": {"mode": "off"}})
    rolled = client.post(f"/api/sample-cycles/batches/{batch['batch_id']}/rollback").json()
    assert rolled["ok"] is True
    assay3_after = client.get("/api/assay/3").json()
    assert "cycle_stage" not in assay3_after
    assert assay3_after["pending"] is False  # 恢复旧口径（种子原值）
    assert client.get("/api/sample-cycles").json()["total"] == 0


# ---------------------------------------------------------------------------
# on 模式：统一状态机、闸口、跨台账回写、幂等
# ---------------------------------------------------------------------------


def _set_on(client: TestClient) -> None:
    client.post("/api/sample-cycles/mode", json={"values": {"mode": "on"}})


def test_on_mode_full_sample_lifecycle_propagates_to_three_ledgers(client: TestClient):
    _set_on(client)
    client.post(
        "/api/core",
        json={"values": {
            "岩心编号": "CORE-E2E",
            "所属钻孔": "BORE-E2E",
            "取样深度起": "5.0",
            "取样深度止": "9.5",
        }},
    )

    # 登记即建周期，初始阶段 REGISTERED
    cycle = next(c for c in client.get("/api/sample-cycles").json()["items"]
                 if c["sample_key"] == "CORE-E2E")
    assert cycle["current_stage"] == "REGISTERED"

    # 编录
    row = client.get("/api/core").json()["items"]
    core_id = next(r["id"] for r in row if r["岩心编号"] == "CORE-E2E")
    assert _post_action(client, "core", core_id, "地质编录").json()["ok"]

    # 送样闸口：深度复核通过后才能送样；送样后送样清单出现该样本行（投影回写）
    assert _post_action(client, "core", core_id, "送样分析").json()["ok"]
    sample_rows = client.get("/api/sample_registry").json()["items"]
    sample_row = next(r for r in sample_rows if r.get("cycle_sample_key") == "CORE-E2E")
    assert sample_row["status"] == "待收样"
    assert sample_row["cycle_depth_ready"] is True

    # 重复送样：幂等，不新增事件、结论不变
    again = _post_action(client, "core", core_id, "送样分析").json()
    assert again["ok"] is True
    cycle = next(c for c in client.get("/api/sample-cycles").json()["items"]
                 if c["sample_key"] == "CORE-E2E")
    assert cycle["current_stage"] == "TO_SAMPLE"

    # 收样：化验待办视角打开（present，但还没有物理行）
    sample_id = sample_row["id"]
    assert _post_action(client, "sample_registry", sample_id, "确认收样").json()["ok"]
    cycle = next(c for c in client.get("/api/sample-cycles").json()["items"]
                 if c["sample_key"] == "CORE-E2E")
    assert cycle["current_stage"] == "SAMPLE_RECEIVED"
    assert cycle["verdicts"]["assay"]["present"] is True
    assert cycle["verdicts"]["assay"]["available"] is True

    # 化验行通过既有登记接口挂接周期，结果三处同源
    client.post(
        "/api/assay",
        json={"values": {"化验编号": "AS-E2E", "样品编号": "CORE-E2E", "元素名称": "Cu",
                         "岩心编号": "CORE-E2E"}},
    )
    assay_id = next(r["id"] for r in client.get("/api/assay").json()["items"]
                    if r["化验编号"] == "AS-E2E")
    assert client.get(f"/api/assay/{assay_id}").json()["status"] == "待录入"

    assert _post_action(client, "assay", assay_id, "录入结果").json()["ok"]
    assert _post_action(client, "assay", assay_id, "审核通过").json()["ok"]

    cycle = next(c for c in client.get("/api/sample-cycles").json()["items"]
                 if c["sample_key"] == "CORE-E2E")
    assert cycle["current_stage"] == "ASSAY_APPROVED"
    assert cycle["verdicts"]["core"]["status"] == "送样中"
    assert cycle["verdicts"]["sample_registry"]["status"] == "已出报告"
    assert cycle["verdicts"]["assay"]["status"] == "已审核"

    # 同一动作带相同幂等键重放，事件数不变
    before = len(cycle["events"])
    _post_action(client, "assay", assay_id, "审核通过", idempotency_key="k1")
    _post_action(client, "assay", assay_id, "审核通过", idempotency_key="k1")
    after = next(c for c in client.get("/api/sample-cycles").json()["items"]
                 if c["sample_key"] == "CORE-E2E")
    assert len(after["events"]) == before  # 阶段未推进，重放不产生事件


def test_on_mode_depth_gate_blocks_delivery(client: TestClient):
    _set_on(client)
    client.post(
        "/api/core",
        json={"values": {
            "岩心编号": "CORE-BAD",
            "所属钻孔": "BORE-BAD",
            "取样深度起": "10",
            "取样深度止": "8",   # 止深小于起深，复核不过
        }},
    )
    core_id = next(r["id"] for r in client.get("/api/core").json()["items"]
                   if r["岩心编号"] == "CORE-BAD")
    _post_action(client, "core", core_id, "地质编录")
    body = _post_action(client, "core", core_id, "送样分析").json()
    assert body["ok"] is False
    assert "取样深度复核" in body["message"]
    # 闸口拒绝时无任何中间态写入：阶段仍是已编录
    cycle = next(c for c in client.get("/api/sample-cycles").json()["items"]
                 if c["sample_key"] == "CORE-BAD")
    assert cycle["current_stage"] == "LOGGED"


def test_on_mode_negative_events_keep_trace(client: TestClient):
    _set_on(client)
    client.post(
        "/api/core",
        json={"values": {
            "岩心编号": "CORE-NEG",
            "所属钻孔": "BORE-NEG",
            "取样深度起": "1",
            "取样深度止": "2",
        }},
    )
    core_id = next(r["id"] for r in client.get("/api/core").json()["items"]
                   if r["岩心编号"] == "CORE-NEG")
    _post_action(client, "core", core_id, "地质编录")
    _post_action(client, "core", core_id, "送样分析")
    sample_id = next(r["id"] for r in client.get("/api/sample_registry").json()["items"]
                     if r.get("cycle_sample_key") == "CORE-NEG")
    _post_action(client, "sample_registry", sample_id, "确认收样")
    client.post(
        "/api/assay",
        json={"values": {"化验编号": "AS-NEG", "样品编号": "CORE-NEG", "元素名称": "Au",
                         "岩心编号": "CORE-NEG"}},
    )
    assay_id = next(r["id"] for r in client.get("/api/assay").json()["items"]
                    if r["化验编号"] == "AS-NEG")
    _post_action(client, "assay", assay_id, "录入结果")
    result = _post_action(client, "assay", assay_id, "退回修改").json()
    assert result["ok"] is True

    cycle = next(c for c in client.get("/api/sample-cycles").json()["items"]
                 if c["sample_key"] == "CORE-NEG")
    assert cycle["current_stage"] == "SAMPLE_RECEIVED"
    assert any(event["negative"] for event in cycle["events"])
    verdict = cycle["verdicts"]["assay"]
    assert verdict["status"] == "已退回"
    assert verdict["abnormal"] is True
    assert verdict["available"] is True  # 退回后重新打开待办


def test_on_mode_mode_switch_is_runtime_rollback(client: TestClient):
    _set_on(client)
    assert client.get("/api/sample-cycles/mode").json()["mode"] == "on"
    # 随时回退到旧路径
    client.post("/api/sample-cycles/mode", json={"values": {"mode": "off"}})
    body = client.get("/api/core").json()["items"][0]
    assert "cycle_stage" not in body


def test_project_batch_is_idempotent(client: TestClient):
    batch = client.post("/api/sample-cycles/backfill", json={"values": {}}).json()["entry"]
    first = client.post(f"/api/sample-cycles/batches/{batch['batch_id']}/project").json()["entry"]
    projected_ids = [(item["sample_key"], item["stage"], item["touched"]) for item in first["projected"]]
    second = client.post(f"/api/sample-cycles/batches/{batch['batch_id']}/project").json()["entry"]
    second_ids = [(item["sample_key"], item["stage"], item["touched"]) for item in second["projected"]]
    assert projected_ids == second_ids
    # 行数不膨胀：物化只发生在在线命令路径，回填投影不造行
    ledgers = client.get("/api/overview").json()["modules"]
    counts = {item["name"]: item["created"] for item in ledgers}
    assert counts["core"] == 3
    assert counts["sample_registry"] == 3
    assert counts["assay"] == 3


# ---------------------------------------------------------------------------
# 回填 -> 切 on 衔接 / 回滚保护 / 跨进程确定性
# ---------------------------------------------------------------------------


def test_backfill_then_online_command_continues_from_confirmed_stage(client: TestClient):
    # CORE-0002 历史已确认“已编录”；回填后切 on，动作应从 LOGGED 继续推进，
    # 而不是要求重新编录（以既有确认阶段为初始基准）。
    batch = client.post("/api/sample-cycles/backfill", json={"values": {}}).json()["entry"]
    client.post(f"/api/sample-cycles/batches/{batch['batch_id']}/project")
    _set_on(client)

    # CORE-0002 深度字段是占位文本，复核不过，先补正事实再送样（经既有登记口径不影响）
    body = _post_action(client, "core", 2, "送样分析").json()
    assert body["ok"] is False  # 深度闸口拦截，且没有部分写入
    cycle = next(c for c in client.get("/api/sample-cycles").json()["items"]
                 if c["sample_key"] == "CORE-0002")
    assert cycle["current_stage"] == "LOGGED"

    # 编录动作重放：幂等，结论不变、不新增命令事件
    again = _post_action(client, "core", 2, "地质编录").json()
    assert again["ok"] is True
    cycle = next(c for c in client.get("/api/sample-cycles").json()["items"]
                 if c["sample_key"] == "CORE-0002")
    assert cycle["current_stage"] == "LOGGED"
    assert all(event["kind"] in ("LEGACY", "RECOVERY") for event in cycle["events"])


def test_rollback_refused_after_online_events(client: TestClient):
    batch = client.post("/api/sample-cycles/backfill", json={"values": {}}).json()["entry"]
    client.post(f"/api/sample-cycles/batches/{batch['batch_id']}/project")
    _set_on(client)
    # CORE-0003 已在送样中；执行“归还原箱”产生在线事件
    result = _post_action(client, "core", 3, "归还原箱").json()
    assert result["ok"] is True

    body = client.post(
        f"/api/sample-cycles/batches/{batch['batch_id']}/rollback"
    ).json()
    assert body["ok"] is False
    assert "在线业务事件" in body["message"]
    # 数据仍处于统一口径，未被回滚破坏
    assert client.get("/api/core/3").json()["cycle_stage"] == "RETURNED"


def test_batch_identity_is_stable_across_fresh_processes():
    def plan_once():
        for name in list(sys.modules):
            if name.startswith("app."):
                del sys.modules[name]
        from app.domain.sample_cycle.migration import MigrationPlanner
        from app.store import store as fresh_store

        planner = MigrationPlanner(fresh_store)
        batch_id, cycles, planned, drifts, stats = planner.plan()
        return batch_id, [c.to_snapshot() for c in cycles], stats

    first_id, first_cycles, first_stats = plan_once()
    second_id, second_cycles, second_stats = plan_once()
    # 重跑同一批数据（这里用两个全新 store 模拟两次执行）：身份、事件、统计完全一致
    assert first_id == second_id
    assert first_stats == second_stats
    assert first_cycles == second_cycles


def test_shadow_then_off_then_on_modes_never_change_legacy_off_contract(client: TestClient):
    # 经过 shadow 和 on 再回到 off，旧接口仍读存储原口径
    client.post("/api/sample-cycles/mode", json={"values": {"mode": "shadow"}})
    _post_action(client, "core", 1, "地质编录")
    _set_on(client)
    _post_action(client, "core", 1, "归还原箱")  # 未送样直接归还：状态机允许到终点
    client.post("/api/sample-cycles/mode", json={"values": {"mode": "off"}})
    row = client.get("/api/core/1").json()
    assert row["status"] == "已归还"
    assert "cycle_stage" not in row


# ---------------------------------------------------------------------------
# 事务边界
# ---------------------------------------------------------------------------


def test_transaction_rolls_back_on_error():
    for name in list(sys.modules):
        if name.startswith("app."):
            del sys.modules[name]
    from app.store import store  # noqa: E402

    before = len(store.rows("core"))
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.rows("core").append({"id": 999, "status": "x"})
            raise RuntimeError("boom")
    assert len(store.rows("core")) == before
