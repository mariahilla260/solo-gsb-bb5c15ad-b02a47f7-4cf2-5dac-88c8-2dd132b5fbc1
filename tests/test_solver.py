"""差分约束求解器测试。"""

import itertools

from app.solver import (
    CalibrationCandidate,
    Observation,
    WindowEvent,
    diagnose,
    plan_calibration,
    plan_window_peak,
    suggest_repair,
)


def _obs(id_, source, target, lo, hi):
    return Observation(id=id_, source=source, target=target, min=lo, max=hi)


def _cand(id_, source, target, lo, hi, cost):
    return CalibrationCandidate(
        id=id_, source=source, target=target, min=lo, max=hi, cost=cost
    )


def _bounds(result):
    return {b["node"]: (b["lower"], b["upper"]) for b in result["node_bounds"]}


def test_feasible_chain_tightest_bounds():
    # o1: x_B - x_A ∈ [1, 5]，o2: x_C - x_B ∈ [2, 4]
    result = diagnose(
        nodes=["A", "B", "C"],
        reference="A",
        observations=[_obs("o1", "A", "B", 1, 5), _obs("o2", "B", "C", 2, 4)],
    )
    assert result["feasible"] is True
    bounds = _bounds(result)
    assert bounds["A"] == (0.0, 0.0)
    assert bounds["B"] == (1.0, 5.0)
    # x_C - x_A：最紧上界 5+4=9，最紧下界 1+2=3
    assert bounds["C"] == (3.0, 9.0)
    assert result["unconstrained_nodes"] == []


def test_parallel_paths_sharpen_bounds():
    # A->B ∈ [1, 10]，A->C ∈ [0, 8]，B->C ∈ [-2, 2]
    result = diagnose(
        nodes=["A", "B", "C"],
        reference="A",
        observations=[
            _obs("o1", "A", "B", 1, 10),
            _obs("o2", "A", "C", 0, 8),
            _obs("o3", "B", "C", -2, 2),
        ],
    )
    assert result["feasible"] is True
    bounds = _bounds(result)
    # x_C - x_A 上界取 min(8, 10+2=12) = 8；下界取 max(0, 1-2=-1) = 0
    assert bounds["C"] == (0.0, 8.0)
    # x_B - x_A 上界取 min(10, 8-(-2)=10)=10；下界取 max(1, 0-2=-2)=1
    assert bounds["B"] == (1.0, 10.0)


def test_unreachable_node_marked_unconstrained():
    # D 与参考节点 A 不在同一弱连通分量
    result = diagnose(
        nodes=["A", "B", "D"],
        reference="A",
        observations=[_obs("o1", "A", "B", 0, 3), _obs("o2", "D", "D", -1, 1)],
    )
    assert result["feasible"] is True
    by_node = {b["node"]: b for b in result["node_bounds"]}
    assert by_node["A"]["constrained"] is True
    assert by_node["B"]["constrained"] is True
    assert by_node["D"]["constrained"] is False
    assert by_node["D"]["lower"] is None
    assert by_node["D"]["upper"] is None
    assert result["unconstrained_nodes"] == ["D"]


def test_single_observation_gives_both_bounds():
    # 普通 [lo, hi] 观测同时贡献上界边与下界边，因此参考节点的邻居双向可达。
    result = diagnose(
        nodes=["A", "B"],
        reference="A",
        observations=[_obs("o1", "A", "B", 0, 5)],
    )
    assert result["feasible"] is True
    assert _bounds(result)["B"] == (0.0, 5.0)


def test_simple_two_edge_contradiction_cycle():
    # o1: x_B - x_A ∈ [0, 10]；o2: x_A - x_B ∈ [3, 4]
    # o2 的下界边为 A->B 权 -3，与 o1 的下界边 B->A 权 0 构成和为 -3 的负环。
    result = diagnose(
        nodes=["A", "B"],
        reference="A",
        observations=[_obs("o1", "A", "B", 0, 10), _obs("o2", "B", "A", 3, 4)],
    )
    assert result["feasible"] is False
    cycle = result["cycle"]
    ids = [s["observation_id"] for s in cycle["steps"]]
    assert set(ids) == {"o1", "o2"}
    assert cycle["accumulated_bound"] == -3.0
    assert cycle["contradiction"] == "0 <= -3.0"
    # 每一步都是闭环上的有向边
    for i, step in enumerate(cycle["steps"]):
        assert step["from"] == cycle["steps"][i - 1]["to"]


def test_contradiction_cycle_is_canonical_and_stable():
    # 三节点矛盾环，多次运行与不同提交顺序结果一致
    obs_a = _obs("obs-a", "X", "Y", 0, 1)   # Y-X ∈ [0,1]
    obs_b = _obs("obs-b", "Y", "Z", 0, 1)   # Z-Y ∈ [0,1]
    obs_c = _obs("obs-c", "Z", "X", 2, 5)   # X-Z ∈ [2,5] => Z-X <= -2
    # 环 X->Y(1) + Y->Z(1) + Z->X(-2) = 0 恰可行；收紧下界
    obs_c = _obs("obs-c", "Z", "X", 3, 5)   # Z-X <= -3 => 下界边环和 -3

    def run(order):
        return diagnose(
            nodes=["X", "Y", "Z"],
            reference="X",
            observations=[obs_a, obs_b, obs_c][::order],
        )

    r1 = run(1)
    r2 = run(-1)
    assert r1["feasible"] is False and r2["feasible"] is False
    key1 = [(s["observation_id"], s["constraint"], s["from"], s["to"]) for s in r1["cycle"]["steps"]]
    key2 = [(s["observation_id"], s["constraint"], s["from"], s["to"]) for s in r2["cycle"]["steps"]]
    assert key1 == key2
    # 下界边环：X->Z(-3) + Z->Y(0) + Y->X(0) = -3
    assert r1["cycle"]["accumulated_bound"] == -3.0
    # 规范化起点：环从起点节点字典序最小的边开始
    start_nodes = [s["from"] for s in r1["cycle"]["steps"]]
    assert start_nodes[0] == min(start_nodes)


def test_self_loop_contradiction():
    # x_A - x_A ∈ [1, 2]：下界边 A->A 权 -1，自环负环
    result = diagnose(
        nodes=["A"],
        reference="A",
        observations=[_obs("bad", "A", "A", 1, 2)],
    )
    assert result["feasible"] is False
    assert [s["observation_id"] for s in result["cycle"]["steps"]] == ["bad"]
    assert result["cycle"]["accumulated_bound"] == -1.0


def test_no_observations_only_reference():
    result = diagnose(nodes=["A"], reference="A", observations=[])
    assert result["feasible"] is True
    assert result["node_bounds"][0]["lower"] == 0.0
    assert result["node_bounds"][0]["upper"] == 0.0
    assert result["unconstrained_nodes"] == []


def test_zero_interval_is_feasible():
    result = diagnose(
        nodes=["A", "B"],
        reference="A",
        observations=[_obs("eq", "A", "B", 3, 3)],
    )
    assert result["feasible"] is True
    assert _bounds(result)["B"] == (3.0, 3.0)


def test_nodes_sorted_in_output():
    result = diagnose(
        nodes=["C", "A", "B"],
        reference="A",
        observations=[_obs("o1", "A", "B", 0, 1), _obs("o2", "B", "C", 0, 1)],
    )
    assert [b["node"] for b in result["node_bounds"]] == ["A", "B", "C"]


def test_regression_negative_cycle_with_converging_parallel_edges():
    # 负环由两条同端点观测的不同边组成（id01 下界边 + id03 上界边），
    # 且环外尾巴的传播会在第 V 轮仍触发更新。
    result = diagnose(
        nodes=["N0", "N1", "N2", "N3", "N4"],
        reference="N4",
        observations=[
            _obs("id00", "N1", "N4", 3, 7),
            _obs("id01", "N1", "N0", 4, 4),
            _obs("id02", "N2", "N3", -6, -1),
            _obs("id03", "N1", "N0", -2, 3),
            _obs("id04", "N3", "N1", -3, -1),
        ],
    )
    assert result["feasible"] is False
    cycle = result["cycle"]
    assert cycle["accumulated_bound"] == -1.0  # N1->N0 权 3 + N0->N1 权 -4
    for i, step in enumerate(cycle["steps"]):
        assert step["from"] == cycle["steps"][i - 1]["to"]


def test_regression_relaxed_edge_endpoint_off_chain():
    # 额外扫描中第一条松弛边的终点位于传播链远端，前驱链长度恰为 V；
    # 负环提取必须从松弛边的起点回卷。
    result = diagnose(
        nodes=["N0", "N1", "N2", "N3"],
        reference="N1",
        observations=[
            _obs("id00", "N1", "N0", 4, 7),
            _obs("id01", "N1", "N3", -1, 1),
            _obs("id02", "N3", "N2", -2, 2),
            _obs("id03", "N2", "N3", -3, 0),
            _obs("id04", "N2", "N3", 5, 10),
        ],
    )
    assert result["feasible"] is False
    cycle = result["cycle"]
    assert cycle["accumulated_bound"] == -5.0  # N2->N3 权 0 + N3->N2 权 -5
    for i, step in enumerate(cycle["steps"]):
        assert step["from"] == cycle["steps"][i - 1]["to"]


def test_suggest_repair_already_feasible_returns_empty():
    obs = [_obs("o1", "A", "B", 0, 5)]
    removed, remaining = suggest_repair(
        nodes=["A", "B"], reference="A", observations=obs, max_repairs=3
    )
    assert removed == []
    assert remaining == obs


def test_suggest_repair_single_deletion_picks_lex_smallest():
    # o1/o2 互相矛盾，删除任意一条均可行 -> 选字典序最小的 o1
    obs = [_obs("o1", "A", "B", 0, 10), _obs("o2", "B", "A", 3, 4)]
    removed, remaining = suggest_repair(
        nodes=["A", "B"], reference="A", observations=obs, max_repairs=3
    )
    assert removed == ["o1"]
    assert [o.id for o in remaining] == ["o2"]


def test_suggest_repair_two_independent_contradictions():
    # 两对独立矛盾 a1/a2 与 b1/b2：需各删一条 -> 字典序最小组合 [a1, b1]
    obs = [
        _obs("a1", "A", "B", 0, 10),
        _obs("a2", "B", "A", 3, 4),
        _obs("b1", "C", "D", 0, 10),
        _obs("b2", "D", "C", 3, 4),
    ]
    removed, remaining = suggest_repair(
        nodes=["A", "B", "C", "D"], reference="A", observations=obs, max_repairs=3
    )
    assert removed == ["a1", "b1"]
    assert [o.id for o in remaining] == ["a2", "b2"]


def test_suggest_repair_self_loop_plus_pair():
    # bad 自环矛盾 + c1/c2 对矛盾：最小修复为删 bad 与 c1/c2 之一
    obs = [
        _obs("bad", "A", "A", 1, 2),
        _obs("c1", "A", "B", 0, 10),
        _obs("c2", "B", "A", 3, 4),
    ]
    removed, remaining = suggest_repair(
        nodes=["A", "B"], reference="A", observations=obs, max_repairs=3
    )
    assert removed == ["bad", "c1"]
    assert [o.id for o in remaining] == ["c2"]


def test_suggest_repair_limit_reached_returns_none():
    # 两处独立矛盾至少需要删 2 条，上限 1 时无解
    obs = [
        _obs("a1", "A", "B", 0, 10),
        _obs("a2", "B", "A", 3, 4),
        _obs("b1", "C", "D", 0, 10),
        _obs("b2", "D", "C", 3, 4),
    ]
    removed, remaining = suggest_repair(
        nodes=["A", "B", "C", "D"], reference="A", observations=obs, max_repairs=1
    )
    assert removed is None and remaining is None


def test_suggest_repair_zero_budget():
    obs = [_obs("bad", "A", "A", 1, 2)]
    removed, remaining = suggest_repair(
        nodes=["A"], reference="A", observations=obs, max_repairs=0
    )
    assert removed is None and remaining is None


def test_suggest_repair_does_not_mutate_input():
    obs = [_obs("o1", "A", "B", 0, 10), _obs("o2", "B", "A", 3, 4)]
    snapshot = list(obs)
    suggest_repair(nodes=["A", "B"], reference="A", observations=obs, max_repairs=3)
    assert obs == snapshot  # 原列表与观测对象均未改变


def _proof_keys(chain):
    return [(s["observation_id"], s["constraint"], s["from"], s["to"])
            for s in chain]


def test_reference_node_has_empty_proofs():
    result = diagnose(
        nodes=["A", "B"],
        reference="A",
        observations=[_obs("o1", "A", "B", 0, 5)],
    )
    a = next(b for b in result["node_bounds"] if b["node"] == "A")
    assert a["lower_proof"] == [] and a["upper_proof"] == []


def test_proof_chains_sum_to_bounds_and_walk():
    # o1: x_B - x_A ∈ [1, 5]，o2: x_C - x_B ∈ [2, 4]
    result = diagnose(
        nodes=["A", "B", "C"],
        reference="A",
        observations=[_obs("o1", "A", "B", 1, 5), _obs("o2", "B", "C", 2, 4)],
    )
    by_node = {b["node"]: b for b in result["node_bounds"]}
    c = by_node["C"]
    # 上界链：参考节点 -> 目标，权重和 = upper
    assert _proof_keys(c["upper_proof"]) == [
        ("o1", "upper", "A", "B"),
        ("o2", "upper", "B", "C"),
    ]
    assert sum(s["weight"] for s in c["upper_proof"]) == c["upper"] == 9.0
    # 下界链：目标 -> 参考节点，权重和的相反数 = lower
    assert _proof_keys(c["lower_proof"]) == [
        ("o2", "lower", "C", "B"),
        ("o1", "lower", "B", "A"),
    ]
    assert sum(s["weight"] for s in c["lower_proof"]) == -c["lower"] == -3.0


def test_unreachable_node_proofs_are_none():
    result = diagnose(
        nodes=["A", "D"],
        reference="A",
        observations=[_obs("solo", "D", "D", -1, 1)],
    )
    d = next(b for b in result["node_bounds"] if b["node"] == "D")
    assert d["lower"] is None and d["lower_proof"] is None
    assert d["upper"] is None and d["upper_proof"] is None


def test_proof_tie_break_lexicographic_and_order_independent():
    # A->B->D 与 A->C->D 等权（上界 4）；证据链取四元组序列字典序最小者
    obs = [
        _obs("o1", "A", "B", 0, 2),
        _obs("o2", "A", "C", 0, 1),
        _obs("o3", "B", "D", 0, 2),
        _obs("o4", "C", "D", 0, 3),
    ]
    results = [
        diagnose(nodes=["A", "B", "C", "D"], reference="A", observations=list(perm))
        for perm in itertools.permutations(obs)
    ]
    first = results[0]["node_bounds"]
    for r in results[1:]:
        assert r["node_bounds"] == first  # 与提交顺序无关
    d = next(b for b in first if b["node"] == "D")
    assert _proof_keys(d["upper_proof"]) == [
        ("o1", "upper", "A", "B"),
        ("o3", "upper", "B", "D"),
    ]
    assert _proof_keys(d["lower_proof"]) == [
        ("o3", "lower", "D", "B"),
        ("o1", "lower", "B", "A"),
    ]


def test_proof_tie_break_decided_at_later_step():
    # 两条等权链共享首步 o1，字典序在第二步决胜
    result = diagnose(
        nodes=["A", "B", "C", "D", "E"],
        reference="A",
        observations=[
            _obs("o1", "A", "B", 0, 1),
            _obs("o2", "B", "C", 0, 1),
            _obs("o3", "B", "D", 0, 1),
            _obs("o4", "C", "E", 0, 1),
            _obs("o5", "D", "E", 0, 1),
        ],
    )
    e = next(b for b in result["node_bounds"] if b["node"] == "E")
    assert _proof_keys(e["upper_proof"]) == [
        ("o1", "upper", "A", "B"),
        ("o2", "upper", "B", "C"),
        ("o4", "upper", "C", "E"),
    ]


def test_proof_chain_stays_simple_around_zero_weight_cycle():
    # 零权环 A<->B：非简单走法 R->A->B->A->V 四元组序列更小，
    # 但证据链必须是简单链 R->A->V
    result = diagnose(
        nodes=["R", "A", "B", "V"],
        reference="R",
        observations=[
            _obs("z", "R", "A", 0, 0),
            _obs("a", "A", "B", 0, 0),
            _obs("b", "A", "V", 0, 0),
        ],
    )
    v = next(b for b in result["node_bounds"] if b["node"] == "V")
    assert _proof_keys(v["upper_proof"]) == [
        ("z", "upper", "R", "A"),
        ("b", "upper", "A", "V"),
    ]


# ---------------------------------------------------------------------------
# 校准方案
# ---------------------------------------------------------------------------


def test_calibration_empty_when_base_already_meets_targets():
    # 基础观测 B-A ∈ [0,10]，目标宽度 10：无需任何候选
    base = [_obs("base", "A", "B", 0, 10)]
    cands = [_cand("cal-a", "A", "B", 2, 2, 10)]
    plan = plan_calibration(
        ["A", "B"], "A", base, [("B", 10)], cands
    )
    assert plan == ([], 0, [])


def test_calibration_picks_minimum_cost_single():
    # 目标宽度 3：cal-a（精确点）成本 10，cal-c 单独即可（宽度 0）成本 2
    base = [_obs("base", "A", "B", 0, 10)]
    cands = [
        _cand("cal-a", "A", "B", 2, 2, 10),
        _cand("cal-b", "A", "B", 0, 4, 1),
        _cand("cal-c", "B", "A", 0, 2, 2),  # B-A ∈ [-2, 0]，与 base 交集宽 0
    ]
    plan = plan_calibration(["A", "B"], "A", base, [("B", 3)], cands)
    ids, cost, chosen = plan
    assert ids == ["cal-c"] and cost == 2 and [o.id for o in chosen] == ["cal-c"]


def test_calibration_requires_combination():
    # 两个候选单独宽度均为 4（>3），组合后宽度为 2
    base = [_obs("base", "A", "B", 0, 10)]
    cands = [
        _cand("cal-a", "A", "B", 2, 2, 10),  # 精确点，成本 10
        _cand("cal-b", "A", "B", 0, 4, 1),
        _cand("cal-c", "B", "A", -6, -2, 1),  # B-A ∈ [2, 6]
    ]
    plan = plan_calibration(["A", "B"], "A", base, [("B", 3)], cands)
    ids, cost, _ = plan
    assert ids == ["cal-b", "cal-c"] and cost == 2


def test_calibration_cost_beats_count():
    # 单条精确观测成本 10；两条组合成本 1+1=2 -> 选组合（总成本优先于条数）
    base = [_obs("base", "A", "B", 0, 10)]
    cands = [
        _cand("single", "A", "B", 2, 2, 10),
        _cand("p", "A", "B", 0, 4, 1),
        _cand("q", "B", "A", -6, -2, 1),
    ]
    plan = plan_calibration(["A", "B"], "A", base, [("B", 3)], cands)
    ids, cost, _ = plan
    assert ids == ["p", "q"] and cost == 2


def test_calibration_equal_cost_prefers_fewer_then_lex():
    base = [_obs("base", "A", "B", 0, 10)]
    # 同成本 5：p、q 各自单独合格，单条优于多条，p < q
    cands = [
        _cand("q", "A", "B", 2, 2, 5),
        _cand("p", "A", "B", 2, 2, 5),
    ]
    plan = plan_calibration(["A", "B"], "A", base, [("B", 3)], cands)
    assert plan[0] == ["p"] and plan[1] == 5

    # 同成本同条数：两条等合格组合，取 ID 序列字典序小
    cands2 = [
        _cand("x", "A", "B", 0, 4, 2),
        _cand("y", "B", "A", -6, -2, 3),  # x+y 成本 5
        _cand("p", "A", "B", 0, 4, 2),
        _cand("q", "B", "A", -6, -2, 3),  # p+q 成本 5，字典序更小
    ]
    plan2 = plan_calibration(["A", "B"], "A", base, [("B", 3)], cands2)
    assert plan2[0] == ["p", "q"] and plan2[1] == 5


def test_calibration_contradictory_combination_unselectable():
    # bad 自身可行但与基础观测矛盾（B-A ∈ [11,20] ∩ [0,10] = 空）
    base = [_obs("base", "A", "B", 0, 10)]
    cands = [_cand("bad", "B", "A", 11, 20, 0)]
    assert plan_calibration(["A", "B"], "A", base, [("B", 3)], cands) is None


def test_calibration_candidate_pair_conflict_unselectable():
    # p 单独宽度不足（4 > 3）；q 与基础观测（B-A ∈ [0,100]）矛盾
    # （A-B ∈ [5,10] => B-A ∈ [-10,-5]）；任何含 q 的组合均不可选 -> 无解
    base = [_obs("base", "A", "B", 0, 100)]
    cands = [
        _cand("p", "A", "B", 0, 4, 1),
        _cand("q", "B", "A", 5, 10, 1),
    ]
    assert plan_calibration(["A", "B"], "A", base, [("B", 3)], cands) is None


def test_calibration_unreachable_target_no_solution():
    # C 与参考节点 A 不在同一分量，候选也未连通 -> 无解
    base = [_obs("base", "A", "B", 0, 10)]
    cands = [_cand("self", "D", "D", 0, 0, 0)]
    assert (
        plan_calibration(["A", "B", "C", "D"], "A", base, [("C", 5)], cands) is None
    )


def test_calibration_disconnected_self_loop_contradiction_blocks():
    # 候选在与参考节点不连通的分量上构成自环矛盾：组合不可选
    base = [_obs("base", "A", "B", 0, 10)]
    cands = [_cand("bad", "C", "C", 1, 2, 0)]
    assert plan_calibration(["A", "B", "C"], "A", base, [("B", 3)], cands) is None


def test_calibration_multiple_targets_must_all_hold():
    # B、C 两个目标：只校准 B 不够，必须同时收窄 C
    base = [
        _obs("b", "A", "B", 0, 10),
        _obs("c", "A", "C", 0, 10),
    ]
    cands = [
        _cand("cb", "A", "B", 0, 2, 1),
        _cand("cc", "A", "C", 0, 2, 1),
    ]
    partial = plan_calibration(
        ["A", "B", "C"], "A", base, [("B", 3), ("C", 3)], cands[:1]
    )
    assert partial is None
    full = plan_calibration(
        ["A", "B", "C"], "A", base, [("B", 3), ("C", 3)], cands
    )
    assert full[0] == ["cb", "cc"] and full[1] == 2


def test_calibration_independent_of_submission_order():
    base = [_obs("base", "A", "B", 0, 10)]
    cands = [
        _cand("cal-a", "A", "B", 2, 2, 10),
        _cand("cal-b", "A", "B", 0, 4, 1),
        _cand("cal-c", "B", "A", -6, -2, 1),
    ]
    plans = [
        plan_calibration(["A", "B"], "A", base, [("B", 3)], list(perm))
        for perm in itertools.permutations(cands)
    ]
    for plan in plans[1:]:
        assert (plan[0], plan[1]) == (plans[0][0], plans[0][1])
    assert plans[0][0] == ["cal-b", "cal-c"]


# ---------------------------------------------------------------------------
# 窗口峰值
# ---------------------------------------------------------------------------


def _event(id_, node, ts):
    return WindowEvent(id=id_, node=node, timestamp=ts)


def _in_window(offsets, event, start, end):
    return start <= event.timestamp - offsets[event.node] <= end


def test_window_peak_all_events_fit():
    # e1 入窗 ⇔ x_B ∈ [5,10]；e2 入窗 ⇔ x_B ∈ [8,13]；交集 [8,10] 非空
    obs = [_obs("sync", "A", "B", 0, 100)]
    events = [_event("e1", "B", 1000), _event("e2", "B", 1003)]
    ids, offsets = plan_window_peak(["A", "B"], "A", obs, events, 990, 995)
    assert ids == ["e1", "e2"]
    assert offsets["A"] == 0.0
    assert all(_in_window(offsets, e, 990, 995) for e in events)


def test_window_peak_shared_clock_cannot_cooccur():
    # 两条事件共享同一时钟偏移 x_B：e1 ⇔ x_B∈[5,10]，e2 ⇔ x_B∈[15,20]
    obs = [_obs("sync", "A", "B", 0, 100)]
    events = [_event("e1", "B", 1000), _event("e2", "B", 1010)]
    ids, offsets = plan_window_peak(["A", "B"], "A", obs, events, 990, 995)
    # 并列 {e1} 与 {e2}，选 ID 字典序最小者
    assert ids == ["e1"]
    by_id = {e.id: e for e in events}
    assert _in_window(offsets, by_id["e1"], 990, 995)
    assert not _in_window(offsets, by_id["e2"], 990, 995)


def test_window_peak_lex_tie_break_and_order_independence():
    # 三事件两两可同窗、三者不可同窗 -> 最大 2 条，选字典序最小对 [e1, e2]
    obs = [_obs("sync", "A", "B", 0, 100)]
    events = [
        _event("e1", "B", 1000),
        _event("e2", "B", 1005),
        _event("e3", "B", 1010),
    ]
    for perm in itertools.permutations(events):
        ids, offsets = plan_window_peak(["A", "B"], "A", obs, list(perm), 990, 997)
        assert ids == ["e1", "e2"]
        # 见证中入窗事件恰为所选集合
        in_window = [e.id for e in events if _in_window(offsets, e, 990, 997)]
        assert in_window == ["e1", "e2"]


def test_window_peak_empty_events_returns_empty_selection():
    obs = [_obs("o1", "A", "B", 1, 5)]
    ids, offsets = plan_window_peak(["A", "B"], "A", obs, [], 0, 10)
    assert ids == []
    # 见证仍覆盖全部节点且满足全部观测
    assert set(offsets) == {"A", "B"}
    assert offsets["A"] == 0.0
    assert 1 <= offsets["B"] - offsets["A"] <= 5


def test_window_peak_event_node_merged_into_node_set():
    # Z 只出现在事件中，也必须进入求解节点集并出现在见证里
    ids, offsets = plan_window_peak(["A"], "A", [], [_event("e1", "Z", 7)], 5, 10)
    assert ids == ["e1"]
    assert set(offsets) == {"A", "Z"}
    assert 5 <= 7 - offsets["Z"] <= 10


def test_window_peak_event_on_reference_has_fixed_real_time():
    # 参考节点偏移恒为 0：t=5 恒在 [0,10] 内可选，t=50 永不可选
    events = [_event("in", "A", 5), _event("out", "A", 50)]
    ids, offsets = plan_window_peak(["A"], "A", [], events, 0, 10)
    assert ids == ["in"]
    assert offsets["A"] == 0.0


def test_window_peak_witness_satisfies_all_observations():
    obs = [_obs("o1", "A", "B", 1, 5), _obs("o2", "B", "C", 2, 4)]
    events = [_event("e1", "C", 100)]
    ids, offsets = plan_window_peak(["A", "B", "C"], "A", obs, events, 90, 200)
    assert ids == ["e1"]
    assert 1 <= offsets["B"] - offsets["A"] <= 5
    assert 2 <= offsets["C"] - offsets["B"] <= 4
    assert 90 <= 100 - offsets["C"] <= 200


def test_window_peak_rigid_link_propagates_constraint():
    # x_C - x_B = 5（刚性）：e1 在 B、e2 在 C，二者不能同时入窗
    obs = [_obs("link", "B", "C", 5, 5), _obs("sync", "A", "B", 0, 100)]
    events = [_event("e1", "B", 1000), _event("e2", "C", 1016)]
    ids, offsets = plan_window_peak(["A", "B", "C"], "A", obs, events, 990, 995)
    # e1 ⇔ x_B∈[5,10]；e2 ⇔ x_C∈[21,26] ⇔ x_B∈[16,21]；区间不相交
    assert len(ids) == 1
    in_window = [e.id for e in events if _in_window(offsets, e, 990, 995)]
    assert in_window == ids
