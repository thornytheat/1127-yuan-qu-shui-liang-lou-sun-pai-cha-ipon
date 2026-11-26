import json
from pathlib import Path

import pytest

from meterloss.night_orders.planner import (
    CLOSE_RACE_MARGIN,
    choose_next_valve,
    plan_night_orders,
    review_trials,
)
from meterloss.night_orders.service import stage_view
from meterloss.pipeline import SAMPLE, run

CUTOFF = 9000


def load_data():
    raw = json.loads((SAMPLE / "isolation_measurements.json").read_text(encoding="utf-8"))
    controls = json.loads((SAMPLE / "round_controls.json").read_text(encoding="utf-8"))
    return raw["pipes"], raw["trials"], controls


def by_sector(orders):
    return {order.sector: order for order in orders}


def test_one_order_per_sector_keeps_locate_and_pending_split():
    pipes, trials, controls = load_data()
    orders, gates = plan_night_orders(pipes, trials, controls, CUTOFF)
    sectors = {row["sector"] for row in pipes}
    assert {order.sector for order in orders} == sectors
    assert len(orders) == len(sectors)
    indexed = by_sector(orders)
    decided = {zone for zone, order in indexed.items() if order.decision == "定案隔离"}
    pending = {zone for zone, order in indexed.items() if order.decision == "补证待定"}
    assert decided == {"南立管", "窑炉间"}
    assert pending == {"中水站"}


def test_decided_branches_go_only_to_dual_credential_crews():
    pipes, trials, controls = load_data()
    orders, _ = plan_night_orders(pipes, trials, controls, CUTOFF)
    dual = {crew["name"] for crew in controls["crews"] if crew["acoustic"] and crew["valve"]}
    for order in orders:
        if order.decision == "定案隔离":
            assert order.crew in dual and order.dispatch == "隔离定案派工"
            assert order.decided_branch and order.confidence_share >= 0.65
            assert order.isolation_support >= 2 and not order.pending_branches
        else:
            assert order.decided_branch is None and order.confidence_share is None
            assert order.crew in dual and order.dispatch == "补证试验派工"


def test_late_and_unreviewed_and_duplicate_trials_never_change_orders():
    pipes, trials, controls = load_data()
    orders_with_late, gates_full = plan_night_orders(pipes, trials, controls, CUTOFF)
    late_status = {gate.name: gate.status for gate in gates_full}
    assert late_status["南立管迟到夜阀-E"] == "late"
    assert late_status["窑炉间失准夜阀-E"] == "unreviewed"
    assert late_status["中水站夜阀-A复测"] == "duplicate"
    assert late_status["窑炉间北立补录"] == "duplicate"
    # 把迟到、失准、重跑记录全部删掉，本班指令必须完全一致。
    blocked = {"late", "unreviewed", "duplicate"}
    clean_trials = [
        row for row in trials
        if late_status.get(row["name"]) not in blocked
    ]
    orders_clean, _ = plan_night_orders(pipes, clean_trials, controls, CUTOFF)
    def substance(orders):
        return [{k: v for k, v in order.as_record().items() if k != "notes"} for order in orders]
    assert substance(orders_clean) == substance(orders_with_late)
    # 截点前移后，迟到记录依旧无法改变指令。
    orders_early, _ = plan_night_orders(pipes, trials, controls, 8800)
    early_names = {name for order in orders_early for name in order.accepted_trials}
    assert "南立管迟到夜阀-E" not in early_names


def test_duplicate_rerun_creates_no_extra_dispatch():
    pipes, trials, controls = load_data()
    orders, gates = plan_night_orders(pipes, trials, controls, CUTOFF)
    duplicates = [gate for gate in gates if gate.status == "duplicate"]
    assert duplicates and all("重跑" in gate.reason and "不新增派工" in gate.reason for gate in duplicates)
    # 每个分区最多一次新试验派工。
    for order in orders:
        new_trials = 0 if order.next_trial_valve is None else 1
        assert new_trials <= 1
    # 再追加一条同名阀点重跑，派工结果不变。
    extra = trials + [{
        "name": "中水站夜阀-B再次重跑", "sector": "中水站", "interrupted": "缓压支路",
        "before_lpm": 18, "after_lpm": 16.5, "reliable": True, "observed": 8970,
    }]
    orders_extra, gates_extra = plan_night_orders(pipes, extra, controls, CUTOFF)
    assert by_sector(orders_extra)["中水站"].next_trial_valve == by_sector(orders)["中水站"].next_trial_valve
    assert any(gate.name == "中水站夜阀-B再次重跑" and gate.status == "duplicate" for gate in gates_extra)


def test_pending_zone_gets_single_untested_independent_trial():
    pipes, trials, controls = load_data()
    orders, _ = plan_night_orders(pipes, trials, controls, CUTOFF)
    water = by_sector(orders)["中水站"]
    accepted_valves = {"北立", "缓压支路", "蓄水井"}
    assert water.next_trial_valve not in accepted_valves
    assert water.pending_branches and "支流南" in water.pending_branches
    # 一支待定只派一次试验，且承接班组与阀点选择保持同一裁决。
    assert water.close_contenders == ("支流南", "北立")
    assert water.next_trial_valve == water.close_contenders[0]


def test_close_race_arbitrates_confirm_leader_vs_exclude_runner():
    # 对称树构造两支接近的局面：首选 B 与次选 E 份额相同，系统须自行裁决本夜先证实还是先排开。
    pipes = [
        {"sector": "景行", "parent": "进水", "child": "A", "material": "钢管", "age_years": 5, "prior": 0.15},
        {"sector": "景行", "parent": "A", "child": "B", "material": "钢管", "age_years": 5, "prior": 0.35},
        {"sector": "景行", "parent": "B", "child": "C", "material": "钢管", "age_years": 5, "prior": 0.25},
        {"sector": "景行", "parent": "A", "child": "E", "material": "钢管", "age_years": 5, "prior": 0.35},
        {"sector": "景行", "parent": "E", "child": "F", "material": "钢管", "age_years": 5, "prior": 0.25},
    ]

    def trial(name, node, after, observed=100):
        return {"name": name, "sector": "景行", "interrupted": node, "before_lpm": 10,
                "after_lpm": after, "reliable": True, "observed": observed}

    roster = {"crews": [{"name": "双证组", "zones": ["景行"], "hours": 200, "acoustic": True,
                         "valve": True, "max_cases": 3, "base": "景行", "starts": 60}], "walks": {}}
    orders, _ = plan_night_orders(pipes, [trial("V1", "A", 6.0)], roster, 200)
    order = orders[0]
    assert order.decision == "补证待定" and order.close_contenders == ("B", "E")
    # B 与 E 均未试；切 B 可直接证实首选且信息增益不劣，裁决先证实首选，派次跟随 B。
    assert order.next_trial_valve == "B"
    assert "先证实首选" in order.next_trial_strategy
    # 首选阀点 B 已试但仍两支接近且未定案时，系统改在次选 E 支上裁决试验，派次跟随该支段。
    trials = [trial("V1", "A", 6.0), trial("V2", "B", 9.4, observed=120),
              trial("V3", "C", 9.5, observed=130)]
    orders_after, _ = plan_night_orders(pipes, trials, roster, 200)
    order_after = orders_after[0]
    assert order_after.decision == "补证待定"
    assert order_after.next_trial_valve in {"E", "F"}
    assert order_after.next_trial_valve in order_after.close_contenders
    assert order_after.next_trial_strategy.startswith("两支接近")


def test_arbitration_is_deterministic_and_ignores_input_order():
    pipes, trials, controls = load_data()
    first, gates_first = plan_night_orders(pipes, trials, controls, CUTOFF)
    second, gates_second = plan_night_orders(list(reversed(pipes)), list(reversed(trials)), controls, CUTOFF)
    assert [o.as_record() for o in first] == [o.as_record() for o in second]
    assert [g.as_record() for g in gates_first] == [g.as_record() for g in gates_second]


def test_zone_without_dual_credential_crew_is_held_for_coordination():
    pipes, trials, controls = load_data()
    restricted = {
        "crews": [{**crew, "zones": ["南立管", "窑炉间"]} if crew["name"] == "阀门三组" else crew
                  for crew in controls["crews"]],
        "walks": controls["walks"],
    }
    orders, _ = plan_night_orders(pipes, trials, restricted, CUTOFF)
    water = by_sector(orders)["中水站"]
    # 听漏一组仍覆盖中水站；把它也剔除后只能值班协调。
    no_crew = {"crews": [crew for crew in controls["crews"] if crew["name"] != "听漏一组"
                         and crew["name"] != "阀门三组"], "walks": controls["walks"]}
    orders_held, _ = plan_night_orders(pipes, trials, no_crew, CUTOFF)
    assert all(order.dispatch == "值班协调" and order.crew is None for order in orders_held)
    assert water.sector == "中水站"


def test_cross_zone_and_unknown_valve_trials_are_rejected():
    pipes, trials, controls = load_data()
    foreign = trials + [
        {"name": "串区试验", "sector": "不存在分区", "interrupted": "北立", "before_lpm": 10,
         "after_lpm": 8.0, "reliable": True, "observed": 8900},
        {"name": "幽灵阀", "sector": "中水站", "interrupted": "图纸外节点", "before_lpm": 10,
         "after_lpm": 8.0, "reliable": True, "observed": 8900},
    ]
    orders, gates = plan_night_orders(pipes, foreign, controls, CUTOFF)
    status = {gate.name: gate.status for gate in gates}
    assert status["串区试验"] == "cross_zone"
    assert status["幽灵阀"] == "unknown_valve"
    baseline, _ = plan_night_orders(pipes, trials, controls, CUTOFF)
    assert [o.as_record() for o in orders] == [o.as_record() for o in baseline]


def test_undecided_zone_with_every_valve_tested_gets_no_new_dispatch():
    pipes = [
        {"sector": "孤区", "parent": "根", "child": "A", "material": "PE", "age_years": 3, "prior": 0.4},
        {"sector": "孤区", "parent": "A", "child": "B", "material": "PE", "age_years": 3, "prior": 0.3},
        {"sector": "孤区", "parent": "根", "child": "C", "material": "PE", "age_years": 3, "prior": 0.35},
    ]
    trials = [
        {"name": "T1", "sector": "孤区", "interrupted": "A", "before_lpm": 9, "after_lpm": 8.5,
         "reliable": True, "observed": 100},
        {"name": "T2", "sector": "孤区", "interrupted": "B", "before_lpm": 9, "after_lpm": 8.6,
         "reliable": True, "observed": 110},
        {"name": "T3", "sector": "孤区", "interrupted": "C", "before_lpm": 9, "after_lpm": 8.7,
         "reliable": True, "observed": 120},
    ]
    roster = {"crews": [{"name": "双证", "zones": ["孤区"], "hours": 100, "acoustic": True,
                         "valve": True, "max_cases": 2, "base": "孤区", "starts": 0}], "walks": {}}
    orders, _ = plan_night_orders(pipes, trials, roster, 200)
    order = orders[0]
    assert order.decision == "补证待定" and order.next_trial_valve is None
    assert order.dispatch == "无未做试验" and order.crew is None
    assert order.pending_branches


def test_stage_view_and_pipeline_expose_night_orders():
    pipes, trials, controls = load_data()
    orders, gates = plan_night_orders(pipes, trials, controls, CUTOFF)
    rendered = stage_view(orders, gates, CUTOFF)
    snapshot = run()
    view = snapshot["night_orders"]
    assert view["title"] == "夜班隔离指令"
    assert rendered["records"] == view["records"]
    assert len(view["records"]) == 3 and view["columns"]["sector"] == "分区"
    assert {gate["status"] for gate in view["errors"]} >= {"late", "unreviewed", "duplicate"}
    assert "截点 9000" in view["summary"]
    # 指纹可复现：相同输入得到完全相同的指令。
    assert run()["night_orders"] == run()["night_orders"]
