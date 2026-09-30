# 样本周期（Sample Cycle）领域重构说明

## 1. 背景与目标

岩心样本的三处台账此前各自维护一套“可用性/状态”判断：

| 台账 | 模块 | 旧判断位置 |
| --- | --- | --- |
| 岩心台账（所属钻孔、取样深度复核） | `core` | `app/services/core.py` |
| 送样清单（送样、收样） | `sample_registry` | `app/services/sample_registry.py` |
| 化验待办（录入、审核、退回） | `assay` | `app/services/assay.py` |

三处状态机、`pending/abnormal` 口径、允许动作完全独立，同一样本在三处可能得出
互相矛盾的结论（种子数据里 `CORE-0003` 就存在：岩心 `送样中`、送样清单 `检测中`、
化验 `已审核`，且 `pending` 标记互不一致）。

重构目标：

1. 抽出统一的**样本周期**领域能力，可用性判断只有一个事实源；
2. 判断结果回写岩心台账、送样清单、化验待办，三处结论必然一致；
3. **既有接口形态不变**：路径、出入参、动作名、报错文案保持兼容，新增字段一律
   用 `cycle_` 前缀，只增不改；
4. 迁移以**既有确认阶段**为初始基准，历史按**原裁决**留痕；
5. 新旧读写路径**可灰度（off/shadow/on）、可回滚**；
6. 回填期间**双读校验**，转换不成功不允许部分写入；
7. 重跑同一批次得到相同结果（确定性 + 幂等）。

## 2. 分层结构

```
app/domain/sample_cycle/
├── stages.py       阶段、责任组、台账映射、送样闸口（纯事实表）
├── events.py       幂等事件与样本周期聚合（append-only）
├── projection.py   统一可用性裁决 assess_cycle + 三处台账投影（纯函数）
├── cycle_store.py  周期仓储：在线命令、物化下游行、投影回写
├── migration.py    确定性回填计划、双读比对、批次执行与回滚
├── legacy.py       旧路径（逐行等价保留，灰度基线与回滚兜底）
└── service.py      灰度门面：按 off/shadow/on 路由新旧路径
```

三个旧服务文件（`app/services/{core,sample_registry,assay}.py`）变为门面薄封装，
router 与前端零改动即可获得统一能力。管理接口集中在
`app/routers/sample_cycle.py`（全部为新增路径，不改老接口）。

## 3. 阶段责任组

统一阶段序列与责任组（`stages.py`）：

| 阶段 | 中文名 | 责任组 |
| --- | --- | --- |
| `REGISTERED` | 已登记 | 岩心保管组 |
| `LOGGED` | 已编录 | 岩心保管组 |
| `DEPTH_RECHECKED` | 深度已复核 | 岩心保管组 |
| `TO_SAMPLE` | 已送样 | 送样流转组 |
| `SAMPLE_RECEIVED` | 已收样 | 送样流转组 |
| `ASSAY_ENTERED` | 结果已录入 | 化验检测组 |
| `ASSAY_APPROVED` | 结果已审核 | 化验检测组 |
| `RETURNED` | 已归还 | 归档组 |

责任划分规则：

- **岩心保管组**：样本身份（所属钻孔）与取样深度复核。进入送样前必须通过深度闸口
  （起/止深度可解析为数值、止深 ≥ 起深），闸口不过，`送样分析` 命令被拒绝。
- **送样流转组**：送样后送样清单必须出现该样本，收样后流转到化验检测组。
- **化验检测组**：收样后化验待办打开；`退回修改` 是显式负向事件，待办重新可用，
  不删历史。
- **归档组**：归还原箱后周期闭合，岩心台账 `available=false`。

旧动作到周期命令的映射（动作名保持不变）：

| 入口台账 | 旧动作 | 目标阶段 | 负向 |
| --- | --- | --- | --- |
| core | 地质编录 | LOGGED | |
| core | 送样分析 | TO_SAMPLE（隐含 DEPTH_RECHECKED 事件） | |
| core | 归还原箱 | RETURNED | |
| sample_registry | 确认收样 | SAMPLE_RECEIVED | |
| sample_registry | 登记报告 | ASSAY_APPROVED | |
| sample_registry | 退回样品 | TO_SAMPLE | 是 |
| assay | 录入结果 | ASSAY_ENTERED | |
| assay | 审核通过 | ASSAY_APPROVED | |
| assay | 退回修改 | SAMPLE_RECEIVED | 是 |

## 4. 幂等事件

事件是状态机的**唯一**写入方式（`events.py`），只追加、不可改、不可删：

- `COMMAND`：在线命令产生；
- `RECOVERY`：切到 on 后第一条在线命令时，以行上既有确认阶段补齐的基准事件；
- `LEGACY`：回填时从三处旧台账机械转写的历史裁决。

幂等保证：

- 事件 ID 为内容哈希。命令事件 = `(样本, 动作, from_stage, to_stage, 幂等键)`；
  在同一阶段重复提交同一动作归并为同一条事件。接口可选传 `idempotency_key`
  （`POST /api/<ledger>/{id}/actions` 的 `values.idempotency_key`），不传时
  默认也幂等。
- LEGACY 事件 ID = `(批次, 来源台账, 行 id, 旧状态)`；批次 ID = 来源数据规范化哈希。
  因此重跑同一批数据：批次相同、事件相同、投影相同，直接返回已有批次。
- 阶段未推进的重放不新增事件，只重新投影并回读。

## 5. 统一裁决与回写投影

`projection.assess_cycle(cycle, ledger_rows)` 是**纯函数**：输入事件流与三处事实行，
输出唯一一份 `CycleAssessment`（统一阶段、责任组、三个闸口、三处台账各自的
`status/pending/abnormal/available/阻断原因`）。任何进程、任何一次重跑结论一致。

投影回写分两类键：

- 既有键 `status / pending / abnormal`：投影为旧状态语义，老页面、老导出无感；
- 新增键 `cycle_sample_key / cycle_stage / cycle_stage_label / cycle_owner_group /
  cycle_available / cycle_unavailable_reasons / cycle_borehole_ready /
  cycle_depth_ready / cycle_delivery_ready / cycle_assay_ready / cycle_synced_at`。

在线命令路径在阶段到达时**幂等物化**下游台账行（送样清单、化验待办），保证
“判断结果回写送样清单和化验待办”；回填投影不造行，只在既有事实上回写。

## 6. 事务边界

`Store.transaction()`（UoW）：块内所有写入落在深拷贝工作副本上，异常整体回滚。

单条在线命令的事务包含：状态机校验 → 事件追加 → 下游行物化 → 三处投影回写 →
周期落库。任一步失败（如深度闸口拒绝、序列化异常）全部回滚，**不存在转换未成功
但部分写入的中间态**。

回填执行严格分两段：先在存储外完成全部计划构造与双读校验（纯计算，不碰数据），
校验完成后进入**单个事务**只做落库；事务内不做任何可能失败的计算。

## 7. 灰度与读写路径

运行时开关（内存态，可随时切换）：`SAMPLE_CYCLE_MODE=off|shadow|on` 环境变量
给初始值，默认 `off`。

| 模式 | 读 | 写 | 用途 |
| --- | --- | --- | --- |
| `off` | 旧路径，响应剥离 `cycle_*` 内部键 | 旧路径 | 回滚兜底态；行为与重构前逐字节等价 |
| `shadow` | 旧路径 | 旧路径 + 双读差异留痕 | 灰度观察：先看三处口径会在哪里打架 |
| `on` | 存储事实 + 统一投影合并视图 | 周期状态机 + 三处回写（单事务） | 周期为单一事实源 |

off 模式下内部投影键仍保留在存储里（视图层过滤），因此 on → off → on 不需要
重新回填。

## 8. 回填、双读与回滚（操作手册）

```
POST /api/sample-cycles/backfill
     {"values": {"project": false}}     # 只回填周期+双读，不动台账
POST /api/sample-cycles/batches/{id}/project   # 把统一裁决回写三处台账（幂等）
GET  /api/sample-cycles/drifts?batch_id=...    # 双读差异逐条留痕
POST /api/sample-cycles/batches/{id}/rollback  # 摘投影、删周期事件
```

- 初始基准：回填只做 `旧状态 -> 周期阶段` 的 1:1 机械映射，不重新裁决；
  负向旧状态（化验“已退回”）保留负向标记。
- 双读校验：逐行比对旧服务实际返回的 `status/pending/abnormal` 与统一投影，
  差异只登记、不阻断回填；内容键幂等。
- 确定性：遍历顺序固定（core → sample_registry → assay，行 id 升序），
  回填时间戳用固定常量 `2026-09-30T00:00:00Z`，批次/事件/快照均由数据内容决定。
- 回滚：投影前逐行快照，回滚按快照逐字段还原（新增键摘除、旧键恢复）；
  若批次周期上已经产生在线 `COMMAND/RECOVERY` 事件，拒绝回滚并列出样本，
  防止抹掉切流后的真实业务。

推荐切流顺序：`off` 回填 → 观察 `drifts` → `shadow` 在线双读观察 →
`project` 回写并核对 → `on` 切流；任何一步异常都可回 `off` 或回滚批次。

## 9. 验证

`backend/tests/test_sample_cycle.py`（16 个用例）覆盖：

- off 契约等价、未知动作文案、概览模块口径不变；
- shadow 只留痕不改返回；
- 回填确定性/幂等、投影幂等、投影→回滚往返；
- on 全生命周期三处联动、深度闸口拒绝且无中间态、负向事件留痕、
  幂等键重放不产生事件、模式可运行时回退；
- 回填后切 on 从既有确认阶段继续、有在线事件拒绝回滚、跨实例批次身份一致；
- 事务异常整体回滚。

```
cd backend
python -m pytest tests/ -q
```
