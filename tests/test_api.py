"""FastAPI 接口测试。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_diagnose_feasible():
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is True
    bounds = {b["node"]: b for b in data["node_bounds"]}
    assert bounds["A"]["lower"] == 0.0 and bounds["A"]["upper"] == 0.0
    assert bounds["B"]["lower"] == 1.0 and bounds["B"]["upper"] == 5.0
    assert bounds["C"]["lower"] == 3.0 and bounds["C"]["upper"] == 9.0
    assert data["unconstrained_nodes"] == []


def test_diagnose_infeasible_cycle_uses_from_alias():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["feasible"] is False
    steps = data["cycle"]["steps"]
    assert data["cycle"]["accumulated_bound"] == -3.0
    assert data["cycle"]["contradiction"] == "0 <= -3.0"
    # 序列化别名为 from，且每步闭环
    assert set(steps[0].keys()) >= {"from", "to", "observation_id", "constraint"}
    for i, step in enumerate(steps):
        assert step["from"] == steps[i - 1]["to"]


def test_unconstrained_node_reported():
    payload = {
        "reference": "A",
        "nodes": ["A", "D"],
        "observations": [
            {"id": "solo", "source": "D", "target": "D", "min": -1, "max": 1}
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["unconstrained_nodes"] == ["D"]
    d = next(b for b in data["node_bounds"] if b["node"] == "D")
    assert d["lower"] is None and d["upper"] is None
    assert d["constrained"] is False


def test_proof_chains_walk_and_sum_to_bounds():
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 200, resp.text
    bounds = {b["node"]: b for b in resp.json()["node_bounds"]}

    # 参考节点：空链
    assert bounds["A"]["lower_proof"] == [] and bounds["A"]["upper_proof"] == []

    # 单条观测同时给出两个方向的证据
    assert bounds["B"]["upper_proof"] == [
        {"observation_id": "o1", "constraint": "upper",
         "from": "A", "to": "B", "weight": 5.0}
    ]
    assert bounds["B"]["lower_proof"] == [
        {"observation_id": "o1", "constraint": "lower",
         "from": "B", "to": "A", "weight": -1.0}
    ]

    # 多步链：上界链从参考节点走到目标，权重和等于 upper
    assert bounds["C"]["upper_proof"] == [
        {"observation_id": "o1", "constraint": "upper",
         "from": "A", "to": "B", "weight": 5.0},
        {"observation_id": "o2", "constraint": "upper",
         "from": "B", "to": "C", "weight": 4.0},
    ]
    assert sum(s["weight"] for s in bounds["C"]["upper_proof"]) == bounds["C"]["upper"]
    # 下界链从目标走回参考节点，权重和的相反数等于 lower
    assert bounds["C"]["lower_proof"] == [
        {"observation_id": "o2", "constraint": "lower",
         "from": "C", "to": "B", "weight": -2.0},
        {"observation_id": "o1", "constraint": "lower",
         "from": "B", "to": "A", "weight": -1.0},
    ]
    assert sum(s["weight"] for s in bounds["C"]["lower_proof"]) == -bounds["C"]["lower"]


def test_proof_chains_closed_walks_for_all_nodes():
    # 任意可行结果：证据链首尾与衔接关系恒成立
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5},
            {"id": "o2", "source": "B", "target": "C", "min": 2, "max": 4},
            {"id": "o3", "source": "A", "target": "C", "min": 0, "max": 8},
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    for b in data["node_bounds"]:
        up, lp = b["upper_proof"], b["lower_proof"]
        if up:
            assert up[0]["from"] == "A" and up[-1]["to"] == b["node"]
            for i in range(1, len(up)):
                assert up[i]["from"] == up[i - 1]["to"]
        if lp:
            assert lp[0]["from"] == b["node"] and lp[-1]["to"] == "A"
            for i in range(1, len(lp)):
                assert lp[i]["from"] == lp[i - 1]["to"]


def test_proof_tie_break_picks_lexicographically_smallest():
    # A->B->D 与 A->C->D 上界等权（均为 4）；下界 D->B->A 与 D->C->A 等权（均为 0）
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 2},
            {"id": "o2", "source": "A", "target": "C", "min": 0, "max": 1},
            {"id": "o3", "source": "B", "target": "D", "min": 0, "max": 2},
            {"id": "o4", "source": "C", "target": "D", "min": 0, "max": 3},
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    d = next(b for b in data["node_bounds"] if b["node"] == "D")
    assert d["upper"] == 4.0 and d["lower"] == 0.0
    # 首步四元组 ("o1",...) < ("o2",...)，上界链走 B
    assert [s["observation_id"] for s in d["upper_proof"]] == ["o1", "o3"]
    # 首步四元组 ("o3",...) < ("o4",...)，下界链走 B
    assert [s["observation_id"] for s in d["lower_proof"]] == ["o3", "o1"]


def test_proof_tie_break_decided_at_later_step():
    # 两条等权上界链共享首步 o1，第二步 ("o2",...) < ("o3",...) 决胜
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "o2", "source": "B", "target": "C", "min": 0, "max": 1},
            {"id": "o3", "source": "B", "target": "D", "min": 0, "max": 1},
            {"id": "o4", "source": "C", "target": "E", "min": 0, "max": 1},
            {"id": "o5", "source": "D", "target": "E", "min": 0, "max": 1},
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    e = next(b for b in data["node_bounds"] if b["node"] == "E")
    assert e["upper"] == 3.0
    assert [s["observation_id"] for s in e["upper_proof"]] == ["o1", "o2", "o4"]


def test_proofs_independent_of_observation_submission_order():
    observations = [
        {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 2},
        {"id": "o2", "source": "A", "target": "C", "min": 0, "max": 1},
        {"id": "o3", "source": "B", "target": "D", "min": 0, "max": 2},
        {"id": "o4", "source": "C", "target": "D", "min": 0, "max": 3},
    ]
    orders = [
        observations,
        observations[::-1],
        [observations[2], observations[0], observations[3], observations[1]],
    ]
    results = [
        client.post("/api/v1/diagnose",
                    json={"reference": "A", "observations": obs}).json()
        for obs in orders
    ]
    for r in results[1:]:
        assert r["node_bounds"] == results[0]["node_bounds"]


def test_unreachable_bound_proofs_are_null():
    payload = {
        "reference": "A",
        "nodes": ["A", "D"],
        "observations": [
            {"id": "solo", "source": "D", "target": "D", "min": -1, "max": 1}
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    d = next(b for b in data["node_bounds"] if b["node"] == "D")
    assert d["lower"] is None and d["lower_proof"] is None
    assert d["upper"] is None and d["upper_proof"] is None
    a = next(b for b in data["node_bounds"] if b["node"] == "A")
    assert a["lower_proof"] == [] and a["upper_proof"] == []


def test_infeasible_response_has_no_proof_fields():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    assert data["feasible"] is False
    assert "node_bounds" not in data
    assert data["cycle"]["accumulated_bound"] == -3.0


def test_repair_feasible_diagnosis_carries_proofs():
    # 修复后的可行诊断与单条诊断结构一致（含证据链），删除语义不变
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    data = client.post("/api/v1/diagnose/repair", json=payload).json()
    assert data["removed_observation_ids"] == ["o1"]
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    b = next(x for x in diag["node_bounds"] if x["node"] == "B")
    # 剩余 o2：x_A - x_B ∈ [3, 4] -> x_B - x_A ∈ [-4, -3]
    assert b["lower"] == -4.0 and b["upper"] == -3.0
    assert b["upper_proof"] == [
        {"observation_id": "o2", "constraint": "lower",
         "from": "A", "to": "B", "weight": -3.0}
    ]
    assert b["lower_proof"] == [
        {"observation_id": "o2", "constraint": "upper",
         "from": "B", "to": "A", "weight": 4.0}
    ]


def test_invalid_interval_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 9, "max": 1}
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 422


def test_duplicate_observation_id_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "x", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "x", "source": "B", "target": "A", "min": 0, "max": 1},
        ],
    }
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 422


def test_reference_missing_from_nodes_rejected():
    payload = {"reference": "Z", "nodes": ["A", "B"], "observations": []}
    resp = client.post("/api/v1/diagnose", json=payload)
    assert resp.status_code == 422


def test_nodes_default_to_observation_endpoints():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1}
        ],
    }
    data = client.post("/api/v1/diagnose", json=payload).json()
    assert [b["node"] for b in data["node_bounds"]] == ["A", "B"]


def test_batch_order_and_index_stable():
    payload = {
        "diagnoses": [
            {
                "name": "ok",
                "reference": "A",
                "observations": [
                    {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1}
                ],
            },
            {
                "name": "bad",
                "reference": "A",
                "observations": [
                    {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 1},
                    {"id": "o2", "source": "B", "target": "A", "min": 2, "max": 3},
                ],
            },
        ]
    }
    resp = client.post("/api/v1/diagnose/batch", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["count"] == 2
    assert data["results"][0]["index"] == 0
    assert data["results"][0]["name"] == "ok"
    assert data["results"][0]["result"]["feasible"] is True
    assert data["results"][1]["index"] == 1
    assert data["results"][1]["name"] == "bad"
    assert data["results"][1]["result"]["feasible"] is False


def test_batch_empty_rejected():
    resp = client.post("/api/v1/diagnose/batch", json={"diagnoses": []})
    assert resp.status_code == 422


def test_repair_feasible_returns_empty_suggestion():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 1, "max": 5}
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == []
    assert data["limit_reached"] is False
    assert data["max_repairs"] == 3  # 默认上限
    assert data["diagnosis"]["feasible"] is True


def test_repair_infeasible_minimal_deletion():
    payload = {
        "name": "fixme",
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "fixme"
    assert data["removed_observation_ids"] == ["o1"]
    assert data["limit_reached"] is False
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    assert diag["name"] == "fixme"
    bounds = {b["node"]: b for b in diag["node_bounds"]}
    # 剩余 o2：x_A - x_B ∈ [3, 4] -> x_B - x_A ∈ [-4, -3]
    assert bounds["B"]["lower"] == -4.0 and bounds["B"]["upper"] == -3.0


def test_repair_default_max_repairs_allows_two_deletions():
    # 两对独立矛盾需删 2 条，默认上限 3 足够
    payload = {
        "reference": "A",
        "observations": [
            {"id": "a1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "a2", "source": "B", "target": "A", "min": 3, "max": 4},
            {"id": "b1", "source": "C", "target": "D", "min": 0, "max": 10},
            {"id": "b2", "source": "D", "target": "C", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["max_repairs"] == 3
    assert data["removed_observation_ids"] == ["a1", "b1"]
    assert data["limit_reached"] is False
    assert data["diagnosis"]["feasible"] is True


def test_repair_limit_reached_returns_original_infeasible_diagnosis():
    payload = {
        "reference": "A",
        "max_repairs": 1,
        "observations": [
            {"id": "a1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "a2", "source": "B", "target": "A", "min": 3, "max": 4},
            {"id": "b1", "source": "C", "target": "D", "min": 0, "max": 10},
            {"id": "b2", "source": "D", "target": "C", "min": 3, "max": 4},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == []
    assert data["limit_reached"] is True
    diag = data["diagnosis"]
    assert diag["feasible"] is False
    assert diag["cycle"]["accumulated_bound"] < 0


def test_repair_max_repairs_zero_on_infeasible():
    payload = {
        "reference": "A",
        "max_repairs": 0,
        "observations": [
            {"id": "bad", "source": "A", "target": "A", "min": 1, "max": 2}
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["limit_reached"] is True
    assert data["removed_observation_ids"] == []
    assert data["diagnosis"]["feasible"] is False


def test_repair_negative_max_repairs_rejected():
    payload = {"reference": "A", "max_repairs": -1, "observations": []}
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 422


def test_repair_unknown_field_rejected():
    payload = {"reference": "A", "observations": [], "bogus": 1}
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 422


def test_repair_dropped_observation_node_vanishes():
    # bad 为 C 上的自环矛盾；删除后 C 不再是剩余系统的一部分
    payload = {
        "reference": "A",
        "observations": [
            {"id": "ok", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "bad", "source": "C", "target": "C", "min": 1, "max": 2},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == ["bad"]
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    assert [b["node"] for b in diag["node_bounds"]] == ["A", "B"]


def test_repair_explicit_nodes_are_retained():
    # 显式声明的节点在修复后保留，被删观测的节点变为不受约束
    payload = {
        "reference": "A",
        "nodes": ["A", "B", "C"],
        "observations": [
            {"id": "ok", "source": "A", "target": "B", "min": 0, "max": 1},
            {"id": "bad", "source": "C", "target": "C", "min": 1, "max": 2},
        ],
    }
    resp = client.post("/api/v1/diagnose/repair", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["removed_observation_ids"] == ["bad"]
    diag = data["diagnosis"]
    assert [b["node"] for b in diag["node_bounds"]] == ["A", "B", "C"]
    assert diag["unconstrained_nodes"] == ["C"]


# ---------------------------------------------------------------------------
# 校准方案
# ---------------------------------------------------------------------------


def test_calibration_plan_optimal_combination():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
        ],
        "targets": [{"node": "B", "max_width": 3}],
        "candidates": [
            {"id": "exact", "source": "A", "target": "B",
             "min": 2, "max": 2, "cost": 10},
            {"id": "lo", "source": "A", "target": "B",
             "min": 0, "max": 4, "cost": 1},
            {"id": "hi", "source": "B", "target": "A",
             "min": -6, "max": -2, "cost": 1},
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == ["hi", "lo"]
    assert data["total_cost"] == 2
    diag = data["diagnosis"]
    assert diag["feasible"] is True
    bounds = {b["node"]: b for b in diag["node_bounds"]}
    # B-A ∈ [0,10] ∩ [0,4] ∩ [2,6] = [2,4]，宽 2
    assert bounds["B"]["lower"] == 2.0 and bounds["B"]["upper"] == 4.0


def test_calibration_plan_cost_beats_count():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
        ],
        "targets": [{"node": "B", "max_width": 3}],
        "candidates": [
            {"id": "exact", "source": "A", "target": "B",
             "min": 2, "max": 2, "cost": 10},
            {"id": "lo", "source": "A", "target": "B",
             "min": 0, "max": 4, "cost": 1},
            {"id": "hi", "source": "B", "target": "A",
             "min": -6, "max": -2, "cost": 1},
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    # 成本 1+1=2 < exact 的 10，尽管条数更多
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == ["hi", "lo"]


def test_calibration_plan_empty_selection_when_already_qualified():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 2}
        ],
        "targets": [{"node": "B", "max_width": 2}],
        "candidates": [
            {"id": "exact", "source": "A", "target": "B",
             "min": 1, "max": 1, "cost": 1}
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == []
    assert data["total_cost"] == 0
    assert data["diagnosis"]["feasible"] is True


def test_calibration_plan_base_contradiction():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 10},
            {"id": "o2", "source": "B", "target": "A", "min": 3, "max": 4},
        ],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "exact", "source": "A", "target": "B",
             "min": 2, "max": 2, "cost": 1}
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "base_contradiction"
    assert data["selected_observation_ids"] == []
    assert data["total_cost"] == 0
    assert data["diagnosis"]["feasible"] is False
    assert data["diagnosis"]["cycle"]["accumulated_bound"] == -3.0


def test_calibration_plan_no_solution_when_unreachable():
    # C 为已声明目标，但无基础观测、无候选可将其与 A 连通 -> 无解
    payload = {
        "reference": "A",
        "nodes": ["A", "C"],
        "observations": [],
        "targets": [{"node": "C", "max_width": 1}],
        "candidates": [],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "no_solution"
    assert data["selected_observation_ids"] == []
    assert data["total_cost"] == 0
    assert data["diagnosis"] is None


def test_calibration_plan_contradictory_candidate_unselectable():
    # 候选与基础观测矛盾（B-A ∈ [11,20] ∩ [0,10] 为空），无解
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
        ],
        "targets": [{"node": "B", "max_width": 3}],
        "candidates": [
            {"id": "bad", "source": "B", "target": "A",
             "min": 11, "max": 20, "cost": 0}
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "no_solution"
    assert data["diagnosis"] is None


def test_calibration_plan_final_diagnosis_excludes_unselected_endpoints():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "base", "source": "A", "target": "B", "min": 0, "max": 10}
        ],
        "targets": [{"node": "B", "max_width": 3}],
        "candidates": [
            {"id": "c1", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1},
            {"id": "unused", "source": "C", "target": "D",
             "min": 0, "max": 0, "cost": 0},
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == ["c1"]
    # C/D 仅出现在未选中的候选中，最终诊断不包含它们
    assert [b["node"] for b in data["diagnosis"]["node_bounds"]] == ["A", "B"]


def test_calibration_plan_duplicate_target_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 2}
        ],
        "targets": [
            {"node": "B", "max_width": 1},
            {"node": "B", "max_width": 2},
        ],
        "candidates": [],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_empty_targets_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [],
        "candidates": [],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_duplicate_candidate_id_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "x", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1},
            {"id": "x", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 2},
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_candidate_id_collides_base_rejected():
    payload = {
        "reference": "A",
        "observations": [
            {"id": "o1", "source": "A", "target": "B", "min": 0, "max": 5}
        ],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "o1", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1}
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_negative_cost_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "c1", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": -1}
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_non_integer_cost_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": "c1", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1.5}
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_negative_max_width_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "A", "max_width": -1}],
        "candidates": [],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_too_many_candidates_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "B", "max_width": 1}],
        "candidates": [
            {"id": f"c{i:02d}", "source": "A", "target": "B",
             "min": 0, "max": 1, "cost": 1}
            for i in range(13)
        ],
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_unknown_field_rejected():
    payload = {
        "reference": "A",
        "observations": [],
        "targets": [{"node": "A", "max_width": 1}],
        "candidates": [],
        "bogus": 1,
    }
    resp = client.post("/api/v1/diagnose/calibration-plan", json=payload)
    assert resp.status_code == 422


def test_calibration_plan_candidate_connects_new_target():
    # C 原本不存在于任何基础观测；通过显式 nodes 声明目标，
    # 候选 A->C 将其连通并收窄
    payload = {
        "reference": "A",
        "nodes": ["A", "C"],
        "observations": [],
        "targets": [{"node": "C", "max_width": 2}],
        "candidates": [
            {"id": "link", "source": "A", "target": "C",
             "min": 1, "max": 3, "cost": 4}
        ],
    }
    data = client.post("/api/v1/diagnose/calibration-plan", json=payload).json()
    assert data["status"] == "optimal"
    assert data["selected_observation_ids"] == ["link"]
    assert data["total_cost"] == 4
    bounds = {b["node"]: b for b in data["diagnosis"]["node_bounds"]}
    assert bounds["C"]["lower"] == 1.0 and bounds["C"]["upper"] == 3.0
