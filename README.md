# 分布式时钟约束诊断 API

提交形如 **“B 时钟 − A 时钟位于 `[min, max]` 毫秒”** 的有向观测，服务将其转换为
[差分约束系统](https://en.wikipedia.org/wiki/Difference_constraints) 并完成：

1. **可行性判定**：是否存在一组自洽的节点时钟偏移；
2. **可行时**：给出每个节点相对指定 **参考节点** 的最紧上下界，并附带
   **证据链**（`lower_proof` / `upper_proof`）解释每个界的来源；同时标出
   无法从参考节点双向约束的节点（孤岛 / 单向可达）；
3. **不可行时**：返回一条由观测 ID 组成的 **矛盾环**、环上每条约束边及累计界限
   （形如 `0 <= -3.0`），环的起点与顺序经过规范化，多次调用结果稳定；
4. **修复建议**：不可行时在不修改任何原观测的前提下，搜索使剩余系统恢复可行的
   **最小观测删除集合**（按删除数量、观测 ID 字典序唯一确定），返回待删观测 ID、
   修复后的诊断摘要，以及是否触及删除上限；
5. **校准方案**：基础观测可行时，从至多 12 条带非负整数成本的候选校准观测中，
   选择使各目标节点相对参考节点的界宽达标的**总成本最小子集**
   （同成本先比条数、再比 ID 序列字典序），返回选中 ID、总成本与加入后的诊断；
6. **窗口峰值**：在「真实时间 = 本地时间戳 − 节点偏移、参考节点偏移为 0」的
   约定下，求**可同时落入给定真实时间窗口的最大事件集合**（至多 12 条事件，
   并列时选排序后事件 ID 序列字典序最小者），并返回一组覆盖全部节点、
   满足全部观测的**可行偏移见证**——该见证下入窗事件恰为所选集合；
7. 提供 **批量诊断接口**，结果按提交下标稳定返回。

算法：差分约束图 + Bellman-Ford（超源初始化检测负环、前驱指针回卷提取矛盾环），
纯 Python 标准实现，无外部服务依赖。

---

## 一键启动（单条命令）

需要 Docker 与 Docker Compose v2：

```bash
docker compose up --build -d
```

服务监听并对外暴露 **8000 端口**：

- 健康检查：<http://localhost:8000/health>
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- OpenAPI Schema：<http://localhost:8000/openapi.json>

停止：

```bash
docker compose down
```

不用 Compose 时：

```bash
docker build -t clock-constraint-diagnosis .
docker run --rm -p 8000:8000 clock-constraint-diagnosis
```

---

## 配置

配置全部通过环境变量完成（见 `docker-compose.yml`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST` | `0.0.0.0` | 容器内 uvicorn 监听地址 |
| `PORT` | `8000` | 容器内监听端口；compose 同时用它映射宿主机端口 |

例如改用宿主机 18000 端口：

```bash
PORT=18000 docker compose up --build -d   # 访问 http://localhost:18000
```

---

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

---

## 接口

### `POST /api/v1/diagnose` — 单条诊断

请求体：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `reference` | string | 是 | 参考时钟节点（计算界的基准） |
| `observations` | array | 是 | 观测列表，可为空 |
| `observations[].id` | string | 是 | 观测唯一 ID（矛盾环按它回指） |
| `observations[].source` | string | 是 | 被减时钟节点 A |
| `observations[].target` | string | 是 | 被减时钟节点 B |
| `observations[].min` | number | 是 | 区间下界（毫秒），须 ≤ `max` |
| `observations[].max` | number | 是 | 区间上界（毫秒） |
| `nodes` | string[] | 否 | 显式节点列表；缺省取观测端点与 `reference` 的并集 |
| `name` | string | 否 | 可选名称，响应原样回显 |

语义：每条观测表示 `x_target - x_source ∈ [min, max]`（`x_v` 为节点 v 的时钟偏移，
单位毫秒）。

#### 可行响应（示例）

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
      {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4}
    ]
  }'
```

```json
{
  "feasible": true,
  "reference": "A",
  "name": null,
  "node_bounds": [
    {"node": "A", "lower": 0.0, "upper": 0.0,
     "lower_proof": [], "upper_proof": [],
     "reachable_from_reference": true, "can_reach_reference": true, "constrained": true},
    {"node": "B", "lower": 1.0, "upper": 5.0,
     "lower_proof": [
       {"observation_id": "o1", "constraint": "lower", "from": "B", "to": "A", "weight": -1.0}
     ],
     "upper_proof": [
       {"observation_id": "o1", "constraint": "upper", "from": "A", "to": "B", "weight": 5.0}
     ],
     "reachable_from_reference": true, "can_reach_reference": true, "constrained": true},
    {"node": "C", "lower": 3.0, "upper": 9.0,
     "lower_proof": [
       {"observation_id": "o2", "constraint": "lower", "from": "C", "to": "B", "weight": -2.0},
       {"observation_id": "o1", "constraint": "lower", "from": "B", "to": "A", "weight": -1.0}
     ],
     "upper_proof": [
       {"observation_id": "o1", "constraint": "upper", "from": "A", "to": "B", "weight": 5.0},
       {"observation_id": "o2", "constraint": "upper", "from": "B", "to": "C", "weight": 4.0}
     ],
     "reachable_from_reference": true, "can_reach_reference": true, "constrained": true}
  ],
  "unconstrained_nodes": []
}
```

`lower` / `upper` 即 `x_node - x_reference` 的最紧上下界（毫秒）。
无法从参考节点方向到达（上界缺失）或无法回到参考节点（下界缺失）的节点，对应界为
`null`，并被收入 `unconstrained_nodes`。

#### 界的证据链（`lower_proof` / `upper_proof`）

每个非 `null` 的界都附带一条证据链，解释该界由哪些观测的哪条约束边推出：

- `upper_proof`：从 **参考节点走到该节点** 的约束边序列，各步 `weight` 之和恰等于
  `upper`；
- `lower_proof`：从 **该节点走回参考节点** 的约束边序列，各步 `weight` 之和的
  **相反数** 恰等于 `lower`；
- 每一步包含 `observation_id`（来源观测）、`constraint`（`upper` = 该观测的 `max`
  约束边，`lower` = `-min` 约束边）、`from` / `to`（沿链方向的端点）与 `weight`；
- **参考节点** 到自身使用 **空链**（`[]`）；某方向不可达时，对应界与证据链
  **均为 `null`**；
- 等权最短链可能有多条：规范选择 **步骤四元组
  `(observation_id, constraint, from, to)` 序列字典序最小** 的简单链，
  结果与观测的提交顺序无关。

#### 不可行响应（矛盾环，示例）

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
      {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4}
    ]
  }'
```

```json
{
  "feasible": false,
  "name": null,
  "cycle": {
    "steps": [
      {"observation_id": "o2", "from": "A", "to": "B",
       "constraint": "lower", "weight": -3.0, "bound": 3.0},
      {"observation_id": "o1", "from": "B", "to": "A",
       "constraint": "lower", "weight": 0.0, "bound": 0.0}
    ],
    "accumulated_bound": -3.0,
    "contradiction": "0 <= -3.0"
  }
}
```

- `steps` 沿环方向闭合排列：上一步的 `to` 等于下一步的 `from`；
- `constraint`：`upper` 表示该边对应观测的 `max`（边 `source -> target`，权 `max`），
  `lower` 表示对应 `-min`（边 `target -> source`，权 `-min`）；
- `weight` 为差分约束图中的边权，`accumulated_bound` 为环上权值之和（负数），
  沿环累加推出 `0 <= accumulated_bound`，即矛盾式。

### `POST /api/v1/diagnose/repair` — 修复建议

请求体沿用单条诊断请求的全部字段，并增加一个可选字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `max_repairs` | integer | 否 | 最多允许删除的观测条数，默认 `3`，须 ≥ 0 |

语义：**不修改任何原观测**，仅搜索「删除哪些观测可让剩余系统恢复可行」。
候选集合按 **删除数量最少** 优先、同数量时按 **观测 ID 字典序** 最小枚举，
首个可行集合即唯一答案（与观测提交顺序无关）。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string/null | 请求名称回显 |
| `max_repairs` | integer | 本次生效的删除上限 |
| `removed_observation_ids` | string[] | 建议删除的观测 ID（字典序）；空数组表示无需删除或上限内无解 |
| `limit_reached` | boolean | `true` 表示在 `max_repairs` 上限内不存在可行修复（此时建议为空） |
| `diagnosis` | object | 应用建议后的诊断摘要（结构同单条诊断响应） |

三种情形：

- **原系统本就可行**：`removed_observation_ids` 为空、`limit_reached=false`，
  `diagnosis` 即原系统的可行诊断；
- **修复成功**：`removed_observation_ids` 给出待删集合，`diagnosis` 为删除后
  剩余系统的可行诊断。其节点集合按「显式 `nodes` ∪ `reference` ∪ 剩余观测端点」
  重新计算——仅出现在被删观测中的节点随之消失；显式声明的节点保留，
  并可能落入 `unconstrained_nodes`；
- **达到上限**：`removed_observation_ids` 为空、`limit_reached=true`，
  `diagnosis` 为原系统的不可行诊断（含矛盾环），可调大 `max_repairs` 重试。
  注意搜索是穷举的，开销随上限组合式增长，请勿设置过大的上限。

#### 修复成功（示例）

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose/repair \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
      {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4}
    ]
  }'
```

```json
{
  "name": null,
  "max_repairs": 3,
  "removed_observation_ids": ["o1"],
  "limit_reached": false,
  "diagnosis": {
    "feasible": true,
    "reference": "A",
    "name": null,
    "node_bounds": [
      {"node": "A", "lower": 0.0, "upper": 0.0,
       "lower_proof": [], "upper_proof": [],
       "reachable_from_reference": true, "can_reach_reference": true, "constrained": true},
      {"node": "B", "lower": -4.0, "upper": -3.0,
       "lower_proof": [
         {"observation_id": "o2", "constraint": "upper", "from": "B", "to": "A", "weight": 4.0}
       ],
       "upper_proof": [
         {"observation_id": "o2", "constraint": "lower", "from": "A", "to": "B", "weight": -3.0}
       ],
       "reachable_from_reference": true, "can_reach_reference": true, "constrained": true}
    ],
    "unconstrained_nodes": []
  }
}
```

删除 `o1` 后仅剩 `o2`（`x_A - x_B ∈ [3, 4]`），故 `x_B - x_A ∈ [-4, -3]`。

#### 达到上限（示例）

两对独立矛盾至少需要删除 2 条观测，`max_repairs=1` 时：

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose/repair \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "max_repairs": 1,
    "observations": [
      {"id": "a1", "source": "A", "target": "B", "min": 0, "max": 10},
      {"id": "a2", "source": "B", "target": "A", "min": 3, "max": 4},
      {"id": "b1", "source": "C", "target": "D", "min": 0, "max": 10},
      {"id": "b2", "source": "D", "target": "C", "min": 3, "max": 4}
    ]
  }'
```

```json
{
  "name": null,
  "max_repairs": 1,
  "removed_observation_ids": [],
  "limit_reached": true,
  "diagnosis": {
    "feasible": false,
    "name": null,
    "cycle": {
      "steps": [
        {"observation_id": "a2", "from": "A", "to": "B",
         "constraint": "lower", "weight": -3.0, "bound": 3.0},
        {"observation_id": "a1", "from": "B", "to": "A",
         "constraint": "lower", "weight": 0.0, "bound": 0.0}
      ],
      "accumulated_bound": -3.0,
      "contradiction": "0 <= -3.0"
    }
  }
}
```

### `POST /api/v1/diagnose/calibration-plan` — 校准方案

在基础观测之外，给定若干**目标节点允许的最大偏移区间宽度**，以及**至多 12 条**
带唯一 ID、**非负整数成本**的候选校准观测；服务从候选中选择满足要求的
**总成本最小**子集。

请求体沿用单条诊断的全部字段（`reference` / `observations` / `nodes` / `name`），
另增加：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `targets` | array | 是 | 至少 1 个校准目标，节点不可重复 |
| `targets[].node` | string | 是 | 目标时钟节点（自动纳入节点集合） |
| `targets[].max_width` | number | 是 | 允许的最大界宽 `upper - lower`（毫秒），须 ≥ 0 |
| `candidates` | array | 否 | 候选校准观测，至多 12 条，默认空数组 |
| `candidates[].id` | string | 是 | 候选唯一 ID；候选间、候选与基础观测间均不可重复 |
| `candidates[].source` / `target` / `min` / `max` | - | 是 | 与普通观测语义相同，`min ≤ max` |
| `candidates[].cost` | integer | 是 | 选用该观测的成本，须为 **非负整数** |

合格子集必须同时满足：

1. 基础观测 + 该子集**仍然可行**（无矛盾环）——任何引入矛盾的组合都不可选
   （包括与基础观测冲突或候选彼此冲突的情况，即使矛盾出现在与参考节点
   不连通的分量上）；
2. 加入后每个目标节点相对参考节点的**上、下界均有限**（双向可达），
   且界宽 `upper - lower ≤ max_width`。

择优顺序唯一确定：**总成本最小 → 条数最少 → 选中 ID（字典序排列后）序列
字典序最小**。注意总成本是第一关键字，因此两条低成本组合可以胜过一条
高成本单观测。

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | `optimal` / `base_contradiction` / `no_solution` |
| `selected_observation_ids` | string[] | 选中的候选 ID（字典序）；非 optimal 时为空 |
| `total_cost` | integer | 选中观测成本之和；非 optimal 时为 0 |
| `diagnosis` | object/null | `optimal`：加入选中观测后的完整诊断（含证据链）；`base_contradiction`：基础观测的矛盾诊断；`no_solution`：`null` |

三种状态：

- **`base_contradiction`**：基础观测本身矛盾，直接返回与单条诊断一致的
  矛盾环，不进行候选搜索；
- **`optimal`**：存在合格子集（空集也算，此时 `selected_observation_ids` 为
  空、`total_cost=0`）；`diagnosis` 的节点集合按「显式 `nodes` ∪ `reference`
  ∪ 基础观测端点 ∪ **被选**候选端点」计算——未选中候选的端点不会出现；
- **`no_solution`**：基础观测可行，但任何候选组合要么引入矛盾、要么无法让
  所有目标界有限且宽度达标（候选为空时也可能出现）。

#### 校准成功（示例）

基础观测 `B-A ∈ [0,10]`（宽 10），要求宽度 ≤ 3：精确点观测成本 10；
另两条各成本 1 的观测组合后宽度为 2，总成本 2 更低，故选择组合：

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose/calibration-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
    ],
    "targets": [{"node": "B", "max_width": 3}],
    "candidates": [
      {"id": "exact", "source": "A", "target": "B", "min": 2, "max": 2, "cost": 10},
      {"id": "lo",    "source": "A", "target": "B", "min": 0, "max": 4, "cost": 1},
      {"id": "hi",    "source": "B", "target": "A", "min": -6, "max": -2, "cost": 1}
    ]
  }'
```

```json
{
  "name": null,
  "status": "optimal",
  "selected_observation_ids": ["hi", "lo"],
  "total_cost": 2,
  "diagnosis": {
    "feasible": true,
    "reference": "A",
    "name": null,
    "node_bounds": [
      {"node": "A", "lower": 0.0, "upper": 0.0,
       "lower_proof": [], "upper_proof": [],
       "reachable_from_reference": true, "can_reach_reference": true, "constrained": true},
      {"node": "B", "lower": 2.0, "upper": 4.0,
       "lower_proof": [
         {"observation_id": "hi", "constraint": "upper", "from": "B", "to": "A", "weight": -2.0}
       ],
       "upper_proof": [
         {"observation_id": "lo", "constraint": "upper", "from": "A", "to": "B", "weight": 4.0}
       ],
       "reachable_from_reference": true, "can_reach_reference": true, "constrained": true}
    ],
    "unconstrained_nodes": []
  }
}
```

加入 `lo`（`B-A ∈ [0,4]`）与 `hi`（`B-A ∈ [2,6]`）后交集为 `[2,4]`，宽 2 ≤ 3。

#### 基础矛盾（示例）

基础观测已构成矛盾环时直接回传矛盾，`status="base_contradiction"`
（`diagnosis` 结构与单条诊断的不可行响应完全一致）：

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose/calibration-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
      {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4}
    ],
    "targets": [{"node": "B", "max_width": 1}],
    "candidates": []
  }'
```

```json
{
  "name": null,
  "status": "base_contradiction",
  "selected_observation_ids": [],
  "total_cost": 0,
  "diagnosis": {
    "feasible": false,
    "name": null,
    "cycle": {
      "steps": [
        {"observation_id": "o2", "from": "A", "to": "B",
         "constraint": "lower", "weight": -3.0, "bound": 3.0},
        {"observation_id": "o1", "from": "B", "to": "A",
         "constraint": "lower", "weight": 0.0, "bound": 0.0}
      ],
      "accumulated_bound": -3.0,
      "contradiction": "0 <= -3.0"
    }
  }
}
```

#### 无解（示例）

目标节点 `C` 无法通过任何候选与参考节点连通（上下界不可能同时有限）：

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose/calibration-plan \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
    ],
    "targets": [{"node": "C", "max_width": 1}],
    "candidates": []
  }'
```

```json
{
  "name": null,
  "status": "no_solution",
  "selected_observation_ids": [],
  "total_cost": 0,
  "diagnosis": null
}
```

### `POST /api/v1/events/window-peak` — 窗口峰值

在诊断请求的基础上另收**至多 12 条事件**与一个**有限闭区间窗口**，求在
满足全部观测的**同一组偏移**下，可同时落入窗口的最大事件数及对应见证。

约定：**真实时间 = 本地时间戳 − 节点偏移**，**参考节点偏移恒为 0**。事件
`e`（节点 `n`、本地时间戳 `t`）入窗当且仅当
`start ≤ t − x_n ≤ end`，即 `x_n ∈ [t − end, t − start]`。

请求体沿用单条诊断的全部字段（`reference` / `observations` / `nodes` / `name`），
另增加：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `events` | array | 否 | 事件列表，至多 12 条，默认空数组 |
| `events[].id` | string | 是 | 事件唯一 ID（事件间不可重复） |
| `events[].node` | string | 是 | 事件发生的时钟节点（自动并入求解节点集） |
| `events[].timestamp` | number | 是 | 节点本地时间戳（毫秒），须为有限数值 |
| `window` | object | 是 | 真实时间窗口 |
| `window.start` / `window.end` | number | 是 | 有限闭区间端点，须满足 `start ≤ end` |

响应字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `status` | string | `ok` / `base_contradiction` |
| `max_in_window` | integer/null | 可同时入窗的最大事件数；`base_contradiction` 时为 `null` |
| `selected_event_ids` | string[] | 选中事件 ID（字典序）；`base_contradiction` 时为空 |
| `offsets` | array/null | 覆盖全部求解节点的可行偏移见证（`{node, offset}`，按节点字典序，参考节点恒为 0）；`base_contradiction` 时为 `null` |
| `diagnosis` | object/null | `base_contradiction` 时为基础观测的矛盾诊断（含矛盾环）；`ok` 时为 `null` |

语义与保证：

- 选中集合按 **(事件数最多, 排序后事件 ID 序列字典序最小)** 唯一确定，
  与事件的提交顺序无关；
- `offsets` 满足全部观测（含参考节点偏移为 0），且该见证下**入窗事件
  恰为所选集合**——由最大性保证：若某未选事件也能入窗，它本可加入
  选中集合，与事件数最多矛盾；
- 空事件列表直接返回 `max_in_window = 0`（`offsets` 仍给出覆盖全部
  节点的可行偏移）；
- 基础观测本身矛盾时不做事件求解，返回 `status="base_contradiction"`
  及与单条诊断一致的矛盾环。

#### 共享时钟约束导致事件不能同时入窗（示例）

两条事件都发生在节点 `B` 上，共享同一个时钟偏移 `x_B`（`x_B − x_A ∈ [0,100]`）：
`e1` 入窗 ⇔ `x_B ∈ [5,10]`，`e2` 入窗 ⇔ `x_B ∈ [15,20]`，两个区间不相交，
因此二者**各自都能入窗、却不能同时入窗**，最多只能选 1 条：

```bash
curl -s -X POST http://localhost:8000/api/v1/events/window-peak \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "sync", "source": "A", "target": "B", "min": 0, "max": 100}
    ],
    "events": [
      {"id": "e1", "node": "B", "timestamp": 1000},
      {"id": "e2", "node": "B", "timestamp": 1010}
    ],
    "window": {"start": 990, "end": 995}
  }'
```

```json
{
  "name": null,
  "status": "ok",
  "max_in_window": 1,
  "selected_event_ids": ["e1"],
  "offsets": [
    {"node": "A", "offset": 0.0},
    {"node": "B", "offset": 5.0}
  ],
  "diagnosis": null
}
```

并列的 `{e1}` 与 `{e2}` 中选择 ID 字典序最小的 `e1`。见证 `x_B = 5` 下：
`e1` 真实时间 `1000 − 5 = 995` 落入 `[990, 995]`，`e2` 真实时间
`1010 − 5 = 1005` 在窗口之外——入窗事件恰为所选集合。

#### 基础观测矛盾（示例）

```bash
curl -s -X POST http://localhost:8000/api/v1/events/window-peak \
  -H 'Content-Type: application/json' \
  -d '{
    "reference": "A",
    "observations": [
      {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
      {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4}
    ],
    "events": [{"id": "e1", "node": "B", "timestamp": 100}],
    "window": {"start": 90, "end": 110}
  }'
```

```json
{
  "name": null,
  "status": "base_contradiction",
  "max_in_window": null,
  "selected_event_ids": [],
  "offsets": null,
  "diagnosis": {
    "feasible": false,
    "name": null,
    "cycle": {
      "steps": [
        {"observation_id": "o2", "from": "A", "to": "B",
         "constraint": "lower", "weight": -3.0, "bound": 3.0},
        {"observation_id": "o1", "from": "B", "to": "A",
         "constraint": "lower", "weight": 0.0, "bound": 0.0}
      ],
      "accumulated_bound": -3.0,
      "contradiction": "0 <= -3.0"
    }
  }
}
```

### `POST /api/v1/diagnose/batch` — 批量诊断

请求体 `{"diagnoses": [ <单条请求>, ... ]}`（至少 1 条）。每条可带 `name`，
响应按提交顺序返回 `index`，互不影响：

```bash
curl -s -X POST http://localhost:8000/api/v1/diagnose/batch \
  -H 'Content-Type: application/json' \
  -d '{"diagnoses":[
    {"name":"ok", "reference":"A",
     "observations":[{"id":"x","source":"A","target":"B","min":0,"max":2}]},
    {"name":"bad", "reference":"A",
     "observations":[
       {"id":"o1","source":"A","target":"B","min":0,"max":10},
       {"id":"o2","source":"B","target":"A","min":3,"max":4}
     ]}
  ]}'
```

### `GET /health`

返回 `{"status": "ok"}`，compose 已配置健康检查。

### 校验错误

非法区间（`min > max`）、重复观测 ID、`reference` 不在 `nodes` 中等请求问题返回
HTTP `422` 及详细错误信息。校准方案接口还会拒绝：`targets` 为空或目标重复、
`max_width < 0`、候选超过 12 条、候选成本为负或非整数、候选 ID 彼此重复或与
基础观测 ID 冲突、候选区间非法等。窗口峰值接口还会拒绝：`events` 超过 12 条、
事件 ID 重复、事件时间戳非有限数值（NaN/±inf）、窗口端点非有限数值或
`start > end`、缺少 `window` 等。

---

## 建模与稳定性说明

每条观测 `x_B - x_A ∈ [min, max]` 展开为两条边：

- `A -> B`，权 `max`（上界约束 `x_B - x_A <= max`）
- `B -> A`，权 `-min`（下界约束 `x_A - x_B <= -min`）

性质：

- 参考节点 `r` 到 `v` 的**最短路** = `x_v - x_r` 的最紧上界；
- 反图中 `r` 到 `v` 的最短路取负 = 最紧下界；
- **负环 ⇔ 不可行**；负环即矛盾环，环上权值之和恒为负；
- **证据链**：上界链为 `r` 到 `v` 的最短路本身（沿原图方向），下界链为 `v` 到 `r`
  的最短路本身；链即界的可审计解释；
- **修复**：穷举删除子集（按删除数量、观测 ID 字典序），首个使剩余图无负环的
  集合即唯一修复建议；删除全部观测必然可行，因此上限不小于观测总数时必有解；
- **校准**：候选至多 12 条，穷举至多 2^12 = 4096 个子集；每个子集先做负环
  检测（矛盾组合不可选），可行时再跑双向最短路核对各目标界宽；按
  (总成本, 条数, ID 序列字典序) 比较，答案与候选提交顺序无关；
- **窗口峰值**：事件 `e`（节点 `n`、本地时间戳 `t`）入窗 ⇔
  `x_n ∈ [t − end, t − start]`（参考节点偏移为 0），展开为
  `reference -> n`（权 `t − start`）与 `n -> reference`（权 `end − t`）两条
  差分约束边，与观测边合并后复用同一负环检测判定一组事件能否同时入窗；
  按 (事件数最多, ID 序列字典序最小) 枚举至多 2^12 个子集确定选中集合；
  见证偏移由零超源最短路（可行势）整体平移至参考节点为 0 得到，
  覆盖「显式 `nodes` ∪ `reference` ∪ 观测端点 ∪ 事件节点」，
  且由最大性保证入窗事件恰为所选集合。

结果顺序的确定性保证：

- 节点一律按字典序输出；
- Bellman-Ford 的边按 `(观测 ID, 约束类型 upper<lower)` 排序后松弛，先到先松弛；
- 矛盾环先沿前驱回卷，再旋转到 `(起点节点, 观测 ID, 约束类型)` 最小的边作为唯一起点；
- 证据链在等权最短链中取步骤四元组 `(observation_id, constraint, from, to)`
  序列字典序最小的简单链（逐位贪心选择，四元组在边集合内唯一）；
- 因此相同的观测集合无论提交顺序如何，上下界、证据链、矛盾环与累计界限完全一致。

## 项目结构

```
app/
  main.py      # FastAPI 路由：/api/v1/diagnose、/api/v1/diagnose/batch、/api/v1/diagnose/repair、/api/v1/diagnose/calibration-plan、/api/v1/events/window-peak、/health
  models.py    # 请求/响应 Pydantic 模型与入参校验
  solver.py    # 差分约束建模、Bellman-Ford、证据链规范选择、负环提取与规范化、最小删除集合搜索、最小成本校准方案、窗口峰值求解与可行偏移见证
tests/         # 算法层与 HTTP 接口测试
Dockerfile
docker-compose.yml
```
