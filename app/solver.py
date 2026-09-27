"""差分约束求解核心。

观测语义
--------
一条观测 ``obs`` 表示有向关系（单位：毫秒）::

    obs.min <= clock[obs.target] - clock[obs.source] <= obs.max

等价于两条差分约束（令 x_v 为节点 v 的时钟偏移）::

    x_target - x_source <= obs.max   ->  边 source -> target，权 obs.max   (上界约束)
    x_source - x_target <= -obs.min  ->  边 target -> source，权 -obs.min  (下界约束)

在该有向图中：

* 从参考节点 r 到 v 的最短路长度即 x_v - x_r 的最紧上界；
* 在反图中从 r 到 v 的最短路长度（即原图 v -> r 的最短路）取负，
  得到最紧下界；
* 图中存在负环 ⇔ 约束系统不可行。负环上的边按方向累加其权值，
  总和为负，即形如 ``0 <= 负数`` 的矛盾。

所有结果的顺序均按 (观测 ID, 约束类型) / 节点字典序确定，保证稳定输出。

界的证据链
----------
可行时每个节点的界都附带一条证据链（最短路本身），便于审计界的来源：

* ``upper_proof``：从参考节点沿原图方向走到目标节点的约束边序列，
  各步权重之和恰等于 ``upper``；
* ``lower_proof``：从目标节点沿原图方向走回参考节点的约束边序列，
  各步权重之和的相反数恰等于 ``lower``；
* 参考节点到自身为**空链**；某方向不可达时对应界与证据链均为 ``None``。

等权最短路可能有多条，规范选择为「步骤四元组
``(observation_id, constraint, from, to)`` 序列字典序最小」的简单链：
逐位贪心，每一步在所有仍能延伸成最短链的候选边中取四元组最小者。
四元组唯一标识一条边，因此选择只依赖观测集合本身，
与观测的提交顺序无关。

修复建议
--------
``suggest_repair`` 在不修改任何观测的前提下，搜索使剩余系统恢复可行的
最小观测删除集合：按 (删除数量, 观测 ID 字典序) 递增枚举候选，首个可行
集合即唯一答案，与观测的提交顺序无关。

校准方案
--------
``plan_calibration`` 在基础观测之外，从至多 12 条带非负整数成本的候选
校准观测中选子集：组合必须保持系统可行（含矛盾的组合不可选），且使各
目标节点相对参考节点的上下界均有限、宽度不超过给定上限。答案按
(总成本, 条数, ID 序列字典序) 唯一确定。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Observation:
    """一条有向时钟观测：x_target - x_source ∈ [min, max]（毫秒）。"""

    id: str
    source: str
    target: str
    min: float
    max: float


@dataclass(frozen=True)
class Edge:
    u: str
    v: str
    weight: float
    observation_id: str
    kind: str  # "upper"（上界边）或 "lower"（下界边）
    bound: float  # 对应观测的原始端点（upper->max, lower->min）


@dataclass(frozen=True)
class CalibrationCandidate(Observation):
    """一条带成本的候选校准观测：成本为非负整数。"""

    cost: int = 0


def _norm(value: float) -> float:
    """归一化浮点输出：避免 -0.0，并保持有限数值。"""
    if value == 0:
        return 0.0
    return float(value)


def _build_edges(observations: Sequence[Observation]) -> List[Edge]:
    """把观测展开为确定性顺序的边列表。"""
    edges: List[Edge] = []
    for obs in observations:
        edges.append(
            Edge(
                u=obs.source,
                v=obs.target,
                weight=_norm(obs.max),
                observation_id=obs.id,
                kind="upper",
                bound=obs.max,
            )
        )
        edges.append(
            Edge(
                u=obs.target,
                v=obs.source,
                weight=_norm(-obs.min),
                observation_id=obs.id,
                kind="lower",
                bound=obs.min,
            )
        )
    # upper(0) 排在 lower(1) 之前；同观测同类型保持声明顺序，整体顺序确定。
    kind_rank = {"upper": 0, "lower": 1}
    edges.sort(key=lambda e: (e.observation_id, kind_rank[e.kind]))
    return edges


def _bellman_ford(edges: Sequence[Edge], nodes: Sequence[str], source: str):
    """从单源跑 Bellman-Ford（最多 V-1 轮）。

    返回 (dist, predecessor)；不可达节点距离为 None。
    边遍历顺序固定，因此松弛决策（先到先得）完全确定。
    """
    inf = float("inf")
    dist = {v: inf for v in nodes}
    prev: dict[str, Optional[Edge]] = {v: None for v in nodes}
    dist[source] = 0.0

    for _ in range(len(nodes) - 1):
        updated = False
        for e in edges:
            du = dist[e.u]
            if du == inf:
                continue
            nd = du + e.weight
            if nd < dist[e.v]:
                dist[e.v] = nd
                prev[e.v] = e
                updated = True
        if not updated:
            break
    return dist, prev


def _extract_negative_cycle(
    edges: Sequence[Edge], nodes: Sequence[str]
) -> Optional[List[Edge]]:
    """以零虚拟源运行 Bellman-Ford，检测并提取一个负环。

    步骤固定：先跑满 V 轮松弛，再额外扫描第一条仍可松弛的边，从该边的
    起点沿前驱指针走入环，回卷得到环上的边（按环方向排列）。
    """
    dist = {v: 0.0 for v in nodes}
    prev: dict[str, Optional[Edge]] = {v: None for v in nodes}

    # 无负环时所有最短路至多 V-1 轮收敛，因此这里跑满 V 轮不提前终止，
    # 再用一次额外扫描确认是否仍可松弛（不能以"第 V 轮有更新"直接判定，
    # 因为个别边可能需要第 V 轮才完成收敛性传播）。
    for _ in range(len(nodes)):
        for e in edges:
            nd = dist[e.u] + e.weight
            if nd < dist[e.v]:
                dist[e.v] = nd
                prev[e.v] = e

    # 从该松弛边的起点沿前驱指针走 V 步：起点已参与 V 轮松弛，其前驱链
    # 走至多 V-1 步必进入某个负环（从终点走则可能恰好走出一条长度为 V
    # 的传播链而得到空前驱）。随后记录节点直到再次相遇即得到简单环。
    relaxed_edge: Optional[Edge] = None
    for e in edges:
        if dist[e.u] + e.weight < dist[e.v]:
            relaxed_edge = e
            break
    if relaxed_edge is None:
        return None

    cur = relaxed_edge.u
    for _ in range(len(nodes)):
        pe = prev[cur]
        if pe is None:  # 理论上不会发生，防御性处理
            return None
        cur = pe.u

    path: List[str] = []
    on_path: dict[str, int] = {}
    while cur not in on_path:
        on_path[cur] = len(path)
        path.append(cur)
        pe = prev[cur]
        if pe is None:
            return None
        cur = pe.u

    cycle_nodes = path[on_path[cur] :]

    # 由环内节点 -> 前驱边，恢复成沿环方向排列的边序列。
    cycle_edges: List[Edge] = []
    for node in cycle_nodes:
        pe = prev[node]
        if pe is None:
            return None
        cycle_edges.append(pe)
    cycle_edges.reverse()

    # 闭环校验，防止任何异常状态下返回不自洽的结果。
    for i, edge in enumerate(cycle_edges):
        if edge.u != cycle_edges[i - 1].v:
            return None
    return cycle_edges


def _canonicalize_cycle(cycle_edges: List[Edge]) -> List[Edge]:
    """将环旋转为唯一表示：以 (起点节点, 观测 ID, 类型) 字典序最小的边开头。"""
    kind_rank = {"upper": 0, "lower": 1}
    keys = [
        (e.u, e.observation_id, kind_rank[e.kind]) for e in cycle_edges
    ]
    start_index = min(range(len(cycle_edges)), key=lambda i: keys[i])
    return cycle_edges[start_index:] + cycle_edges[:start_index]


def _chain_key(e: Edge) -> Tuple[str, str, str, str]:
    """证据链步骤的四元组排序键：(observation_id, constraint, from, to)。

    同一观测的 upper/lower 边端点互异（自环观测除外，但自环边不可能
    出现在简单链中），因此四元组在边集合内唯一，比较结果与输入顺序无关。
    """
    return (e.observation_id, e.kind, e.u, e.v)


def _distances_to_goal(
    nodes: Sequence[str],
    edges: Sequence[Edge],
    goal: str,
    excluded: set,
) -> dict:
    """各节点沿原图方向到 goal 的最短路长度（在删除 excluded 节点的图上）。

    通过在反图上从 goal 跑 Bellman-Ford 得到；不可达节点距离为 inf。
    """
    if goal in excluded:  # 调用方保证不发生，防御性处理
        return {}
    remaining = [n for n in nodes if n not in excluded]
    reverse_edges = [
        Edge(
            u=e.v,
            v=e.u,
            weight=e.weight,
            observation_id=e.observation_id,
            kind=e.kind,
            bound=e.bound,
        )
        for e in edges
        if e.u not in excluded and e.v not in excluded
    ]
    dist, _ = _bellman_ford(reverse_edges, remaining, goal)
    return dist


def _lex_min_chain(
    nodes: Sequence[str],
    edges: Sequence[Edge],
    start: str,
    goal: str,
    total: float,
) -> Optional[List[Edge]]:
    """从 start 到 goal、权重和为 total 的简单链中字典序最小者。

    字典序按步骤四元组 ``(observation_id, constraint, from, to)`` 的序列
    比较。前提：图中无负环、goal 从 start 可达、total 为最短路长度。

    算法：逐位贪心。维护不变式「已走权重 + 当前节点避开已访节点到 goal
    的最短路 = total」，每步在所有仍能把链延伸成最短简单链的候选边中
    选四元组最小者。候选可行性只依赖距离值与边集合，与边的输入顺序无关。

    浮点病态（精确相等失效导致无候选边）时返回 None，由调用方回退。
    """
    if start == goal:
        return []
    chain: List[Edge] = []
    visited = {start}
    cur = start
    acc = 0.0  # 已走链的权重和
    while cur != goal:
        # 剩余链必须避开 visited 中的节点（简单链），先算各节点到 goal 的距离
        d_rem = _distances_to_goal(nodes, edges, goal, visited)
        best: Optional[Edge] = None
        for e in edges:
            if e.u != cur or e.v in visited:
                continue
            if acc + e.weight + d_rem[e.v] != total:
                continue
            if best is None or _chain_key(e) < _chain_key(best):
                best = e
        if best is None:
            return None
        chain.append(best)
        visited.add(best.v)
        acc += best.weight
        cur = best.v
    return chain


def _predecessor_chain(
    prev: dict, start: str, goal: str
) -> Optional[List[Edge]]:
    """沿 Bellman-Ford 前驱指针从 goal 回溯到 start，恢复 start->goal 的边序列。"""
    chain: List[Edge] = []
    cur = goal
    while cur != start:
        e = prev.get(cur)
        if e is None:  # 理论上不会发生，防御性处理
            return None
        chain.append(e)
        cur = e.u
    chain.reverse()
    return chain


def _proof_steps(chain: Sequence[Edge]) -> List[dict]:
    """把边序列转换为证据链步骤字典。"""
    return [
        {
            "observation_id": e.observation_id,
            "constraint": e.kind,
            "from": e.u,
            "to": e.v,
            "weight": _norm(e.weight),
        }
        for e in chain
    ]


def _unreverse(e: Edge) -> Edge:
    """把反图边还原为原图方向的边。"""
    return Edge(
        u=e.v,
        v=e.u,
        weight=e.weight,
        observation_id=e.observation_id,
        kind=e.kind,
        bound=e.bound,
    )


def _is_feasible(nodes: Sequence[str], observations: Sequence[Observation]) -> bool:
    """判断约束系统是否可行（无负环）；供修复搜索做快速可行性检查。"""
    ordered_nodes = sorted(set(nodes))
    edges = _build_edges(observations)
    return _extract_negative_cycle(edges, ordered_nodes) is None


def diagnose(
    nodes: Sequence[str],
    reference: str,
    observations: Sequence[Observation],
):
    """执行一次完整诊断。

    返回字典：
      可行   -> {"feasible": True, "reference": ..., "node_bounds": [...],
                 "unconstrained_nodes": [...]}
                每个 node_bounds 条目附带 lower_proof / upper_proof 证据链
                （步骤为 observation_id/constraint/from/to/weight 字典）：
                上界链从参考节点走到该节点、权重和等于 upper；
                下界链从该节点走回参考节点、权重和的相反数等于 lower；
                参考节点为空链，不可达方向的界与证据链均为 None。
      不可行 -> {"feasible": False, "cycle": {"steps": [...],
                 "accumulated_bound": float, "contradiction": str}}
    """
    ordered_nodes = sorted(set(nodes))
    edges = _build_edges(observations)

    cycle_edges = _extract_negative_cycle(edges, ordered_nodes)
    if cycle_edges is not None:
        cycle_edges = _canonicalize_cycle(cycle_edges)
        steps = [
            {
                "observation_id": e.observation_id,
                "from": e.u,
                "to": e.v,
                "constraint": e.kind,
                "weight": _norm(e.weight),
                "bound": _norm(e.bound),
            }
            for e in cycle_edges
        ]
        total = _norm(sum(e.weight for e in cycle_edges))
        return {
            "feasible": False,
            "cycle": {
                "steps": steps,
                "accumulated_bound": total,
                "contradiction": f"0 <= {total}",
            },
        }

    # 可行：正向最短路给出最紧上界。
    dist, prev = _bellman_ford(edges, ordered_nodes, reference)

    # 反图最短路 dist_rev[v] = 原图 v -> reference 的最短路，
    # 于是最紧下界 x_v - x_r >= -dist_rev[v]。
    reverse_edges = [_unreverse(e) for e in edges]
    dist_rev, prev_rev = _bellman_ford(reverse_edges, ordered_nodes, reference)

    inf = float("inf")
    node_bounds = []
    unconstrained: List[str] = []
    for v in ordered_nodes:
        reachable_forward = dist[v] != inf
        reachable_reverse = dist_rev[v] != inf
        upper = _norm(dist[v]) if reachable_forward else None
        lower = _norm(-dist_rev[v]) if reachable_reverse else None
        constrained = reachable_forward and reachable_reverse
        if not constrained:
            unconstrained.append(v)

        # 证据链：规范（四元组序列字典序最小）的最短简单链；
        # 浮点病态下回退到前驱指针链（仍为最短路，仅字典序不保证）。
        if reachable_forward:
            chain = _lex_min_chain(ordered_nodes, edges, reference, v, dist[v])
            if chain is None:
                chain = _predecessor_chain(prev, reference, v) or []
            upper_proof = _proof_steps(chain)
        else:
            upper_proof = None

        if reachable_reverse:
            chain = _lex_min_chain(ordered_nodes, edges, v, reference, dist_rev[v])
            if chain is None:
                rev_chain = _predecessor_chain(prev_rev, reference, v) or []
                chain = [_unreverse(e) for e in reversed(rev_chain)]
            lower_proof = _proof_steps(chain)
        else:
            lower_proof = None

        node_bounds.append(
            {
                "node": v,
                "lower": lower,
                "upper": upper,
                "lower_proof": lower_proof,
                "upper_proof": upper_proof,
                "reachable_from_reference": reachable_forward,
                "can_reach_reference": reachable_reverse,
                "constrained": constrained,
            }
        )

    return {
        "feasible": True,
        "reference": reference,
        "node_bounds": node_bounds,
        "unconstrained_nodes": unconstrained,
    }


def suggest_repair(
    nodes: Sequence[str],
    reference: str,
    observations: Sequence[Observation],
    max_repairs: int,
) -> Tuple[Optional[List[str]], Optional[List[Observation]]]:
    """搜索使剩余系统恢复可行的最小观测删除集合（不修改任何观测）。

    按 (删除数量, 观测 ID 字典序) 递增枚举候选集合：
    ``itertools.combinations`` 在按 ID 排序的输入上即按字典序产出组合，
    因此第一个使剩余系统可行的集合就是唯一答案。

    返回 ``(removed_ids, remaining)``：

    * 系统本就可行 -> ``([], 按 ID 排序的全部观测)``；
    * 找到修复 -> ``removed_ids`` 为按字典序排列的待删观测 ID，
      ``remaining`` 为删除后的剩余观测（原观测对象不变）；
    * 上限内无解 -> ``(None, None)``。

    说明：搜索是穷举的，开销随 ``max_repairs`` 组合式增长，
    调用方应把删除上限控制在较小范围。
    """
    ordered = sorted(observations, key=lambda o: o.id)
    if _is_feasible(nodes, ordered):
        return [], list(ordered)

    budget = max(0, min(max_repairs, len(ordered)))
    for k in range(1, budget + 1):
        for combo in itertools.combinations(ordered, k):
            removed = {o.id for o in combo}
            remaining = [o for o in ordered if o.id not in removed]
            if _is_feasible(nodes, remaining):
                return [o.id for o in combo], remaining
    return None, None


# ---------------------------------------------------------------------------
# 校准方案
# ---------------------------------------------------------------------------

# 宽度达标比较使用的浮点容差（相对宽度 + 绝对误差），避免 2-1 != 1 之类的
# 表示误差把已达标的方案误判为不达标。
_WIDTH_EPS = 1e-9


def _width_ok(dist, dist_rev, targets) -> bool:
    """各目标节点相对参考节点的上下界是否均有限且宽度达标。

    ``dist`` / ``dist_rev`` 为正图 / 反图从参考节点出发的最短路距离；
    ``targets`` 为 ``(node, max_width)`` 序列。上界缺失（不可达）、
    下界缺失或 ``upper - lower > max_width`` 均视为不达标。
    """
    inf = float("inf")
    for node, max_width in targets:
        if dist[node] == inf or dist_rev[node] == inf:
            return False
        width = dist[node] + dist_rev[node]  # upper - lower（lower = -dist_rev）
        if width > max_width + _WIDTH_EPS * max(1.0, abs(max_width)):
            return False
    return True


def plan_calibration(
    nodes: Sequence[str],
    reference: str,
    observations: Sequence[Observation],
    targets: Sequence[Tuple[str, float]],
    candidates: Sequence[CalibrationCandidate],
) -> Optional[Tuple[List[str], int, List[Observation]]]:
    """选择总成本最小的候选校准观测子集（调用方须保证基础观测本身可行）。

    一个子集合格当且仅当：

    1. 基础观测 + 该子集组成的约束系统**可行**（无负环）——因此引入矛盾的
       候选组合不可选（包括与基础观测冲突、候选彼此冲突的情况）；
    2. 加入后每个目标节点相对参考节点的最紧上、下界**均有限**
       （两个方向都可达），且界宽 ``upper - lower`` 不超过该节点给定的
       最大允许宽度。

    合格子集按 **(总成本, 条数, ID 序列字典序)** 取最小者；ID 序列指
    选中候选 ID 按字典序排列后的列表。候选至多 12 条，故按
    ``(条数, ID 字典序)`` 分层枚举至多 2^12 = 4096 个子集即可穷举。

    返回 ``(selected_ids, total_cost, selected_observations)``；
    空集即合格时返回 ``([], 0, [])``；无合格子集时返回 ``None``。
    """
    # 候选端点即使最终未被选中，也必须在评估图的节点宇宙中，
    # 否则枚举到含该候选的组合时 Bellman-Ford 会遇到未知节点。
    all_nodes = set(nodes)
    for c in candidates:
        all_nodes.add(c.source)
        all_nodes.add(c.target)
    ordered_nodes = sorted(all_nodes)
    ordered = sorted(candidates, key=lambda c: c.id)

    base_edges = _build_edges(observations)
    # id(candidate) -> 该候选展开的两条边，评估时避免重复建边。
    edges_by_candidate = {id(c): _build_edges([c]) for c in ordered}

    def evaluate(chosen: Sequence[CalibrationCandidate]):
        """检查被选子集：先做负环检测，可行时跑双向 Bellman-Ford 验宽度。"""
        edges = list(base_edges)
        for c in chosen:
            edges.extend(edges_by_candidate[id(c)])
        if _extract_negative_cycle(edges, ordered_nodes) is not None:
            return False
        dist, _ = _bellman_ford(edges, ordered_nodes, reference)
        reverse_edges = [_unreverse(e) for e in edges]
        dist_rev, _ = _bellman_ford(reverse_edges, ordered_nodes, reference)
        return _width_ok(dist, dist_rev, targets)

    # 空集优先：基础观测已经使所有目标达标时无需任何校准。
    if evaluate([]):
        return [], 0, []

    best: Optional[Tuple[int, int, List[str], List[CalibrationCandidate]]] = None
    # 注意：总成本是第一排序键，因此低层（条数少）找到的方案总成本可能更高，
    # 不能因低层已有解而跳过高层；只能剪掉总成本严格更大的组合。
    # 等成本组合仍需评估，由 (cost, k, ids) 全键比较决定条数与字典序胜负。
    for k in range(1, len(ordered) + 1):
        for combo in itertools.combinations(ordered, k):
            cost = sum(c.cost for c in combo)
            if best is not None and cost > best[0]:
                continue
            if not evaluate(combo):
                continue
            ids = [c.id for c in combo]
            if best is None or (cost, k, ids) < (best[0], best[1], best[2]):
                best = (cost, k, ids, list(combo))

    if best is None:
        return None
    _, _, ids, chosen = best
    return ids, best[0], chosen
