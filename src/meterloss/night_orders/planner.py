from collections import defaultdict
from math import log

from ..loss_screen.isolation_graph import (
    PipeLink,
    ValvePulse,
    downstream,
    localize,
    path_to_root,
)
from .contracts import NightOrder, TrialGate

# 两支段份额差不超过该阈值即视为“两支接近”，进入信息增益裁决。
CLOSE_RACE_MARGIN = 0.18
# 定案所需的最少独立试验与领先份额，与漏损筛查口径一致。
MIN_SUPPORT = 2
DECISION_SHARE = 0.65


def _zone_graphs(pipes):
    lines = defaultdict(list)
    for raw in pipes:
        link = PipeLink(**raw)
        lines[link.sector].append(link)
    graphs = {}
    for zone, segments in sorted(lines.items()):
        parent_of = path_to_root(segments)
        if len({s.parent for s in segments if s.parent not in parent_of}) != 1:
            raise ValueError("单个分区应有唯一的进水根")
        graphs[zone] = (segments, parent_of)
    return graphs


def review_trials(pipes, trials, cutoff):
    """截点复核：只放可靠、不迟到且属于本分区的试验进闸。"""
    graphs = _zone_graphs(pipes)
    gates = []
    accepted = defaultdict(list)
    seen_valve = defaultdict(dict)
    for raw in sorted(trials, key=lambda row: (row.get("observed", 0), row.get("name", ""))):
        pulse = ValvePulse(**raw)
        zone = pulse.sector
        if zone not in graphs:
            gates.append(TrialGate(pulse.name, zone, pulse.interrupted, pulse.observed,
                                   "cross_zone", "试验分区不在隔离图内，不能改变任何本班指令"))
            continue
        parent_of = graphs[zone][1]
        if pulse.interrupted not in parent_of:
            gates.append(TrialGate(pulse.name, zone, pulse.interrupted, pulse.observed,
                                   "unknown_valve", "阀点不在本分区隔离图上，不加入候选权重"))
            continue
        if not pulse.reliable:
            gates.append(TrialGate(pulse.name, zone, pulse.interrupted, pulse.observed,
                                   "unreviewed", "复核未确认有效，本班不予采信"))
            continue
        if pulse.observed > cutoff:
            gates.append(TrialGate(pulse.name, zone, pulse.interrupted, pulse.observed,
                                   "late", f"观测时点 {pulse.observed} 晚于截点 {cutoff}，迟到记录不改变本班指令"))
            continue
        earlier = seen_valve[zone].get(pulse.interrupted)
        if earlier is not None:
            gates.append(TrialGate(pulse.name, zone, pulse.interrupted, pulse.observed,
                                   "duplicate", f"与已采信试验 {earlier} 操作同一阀点，属于重跑，不新增派工"))
            continue
        seen_valve[zone][pulse.interrupted] = pulse.name
        gates.append(TrialGate(pulse.name, zone, pulse.interrupted, pulse.observed,
                               "accepted", "截点前复核有效且属于本分区"))
        accepted[zone].append(pulse)
    return gates, accepted


def _eligible_crews(roster, zone):
    teams = []
    for crew in roster["crews"]:
        if zone in crew["zones"] and crew["acoustic"] and crew["valve"]:
            teams.append(crew["name"])
    return tuple(sorted(teams))


def _expected_entropy(rank, below):
    """在给定阀点做一次独立试验后的期望后验熵（份额即贝叶斯后验权重）。"""
    positive = sum(share for node, share in rank if node in below)
    negative = 1.0 - positive

    def branch_entropy(mass, inside):
        if mass <= 0:
            return 0.0
        terms = (share / mass for node, share in rank if (node in below) == inside)
        return -mass * sum(p * log(p) for p in terms if p > 0)

    return branch_entropy(positive, True) + branch_entropy(negative, False)


def choose_next_valve(parent_of, rank, tested_valves):
    """裁决下一夜唯一一次独立试验的阀点。

    两支接近时按期望信息增益选择：先证实首选与先排开另一支由系统自行裁决，
    派次随所选阀点；信息增益相同则优先证实领先支段，再按阀点名稳定消歧。
    """
    untested = [node for node in parent_of if node not in tested_valves]
    if not untested:
        return None, "本分区阀点均已独立试验，无未做过的试验可安排"
    leader, leader_share = rank[0]
    runner_up, runner_share = rank[1]
    close_race = leader_share - runner_share <= CLOSE_RACE_MARGIN
    options = []
    for valve in untested:
        below = downstream(parent_of, valve)
        gain = -_expected_entropy([(node, share) for node, share in rank], below)
        confirm_leader = leader in below
        exclude_runner = runner_up in below and leader not in below
        options.append((gain, 1 if confirm_leader else 0, 1 if exclude_runner else 0, valve))
    # 增益最大优先；并列时先证实首选（confirm=1），再看是否直接排开次选，最后按阀点名稳定消歧。
    gain, confirm, exclude, valve = max(options, key=lambda item: (item[0], item[1], item[2], item[3]))
    below = downstream(parent_of, valve)
    if not close_race:
        strategy = "领先优势明确，优先证实首选支段" if leader in below else "选择区分度最高的未试阀点补证"
    elif leader in below:
        strategy = "两支接近，本夜先证实首选支段，避免整夜耗在错误支段"
    else:
        strategy = "两支接近，本夜先隔离排开另一支段，用一次试验收窄候选"
    return valve, strategy


def plan_night_orders(pipes, trials, roster, cutoff, min_support=MIN_SUPPORT):
    if min_support < 1:
        raise ValueError("最少独立隔离测试次数须为正")
    graphs = _zone_graphs(pipes)
    gates, accepted = review_trials(pipes, trials, cutoff)
    accepted_payload = [
        {
            "name": pulse.name, "sector": pulse.sector, "interrupted": pulse.interrupted,
            "before_lpm": pulse.before_lpm, "after_lpm": pulse.after_lpm,
            "reliable": pulse.reliable, "observed": pulse.observed,
        }
        for zone_pulses in accepted.values() for pulse in zone_pulses
    ]
    findings = {row["sector"]: row for row in localize(pipes, accepted_payload, cutoff, min_support)}
    orders = []
    for zone, (segments, parent_of) in sorted(graphs.items()):
        zone_pulses = accepted[zone]
        tested_valves = {pulse.interrupted for pulse in zone_pulses}
        accepted_names = tuple(pulse.name for pulse in sorted(zone_pulses, key=lambda p: (p.observed, p.name)))
        finding = findings[zone]
        rank = [(row["node"], row["share"]) for row in finding["rank"]]
        crews = _eligible_crews(roster, zone)
        if finding["located"]:
            branch = finding["priority_branch"]
            crew = crews[0] if crews else None
            dispatch = "隔离定案派工" if crew else "值班协调"
            notes = (f"两次以上独立支撑且份额 {finding['rank'][0]['share']:.3f} 达到 {DECISION_SHARE:.0%}，"
                     "交双资质班组执行隔离听漏")
            if not crew:
                notes = "本分区无同时具备阀门与听漏资质的班组，定案支段暂缓派工，等待值班协调"
            orders.append(NightOrder(
                sector=zone, decision="定案隔离", decided_branch=branch,
                confidence_share=finding["rank"][0]["share"],
                isolation_support=finding["isolation_support"],
                next_trial_valve=None, next_trial_strategy=None,
                dispatch=dispatch, crew=crew, pending_branches=(), close_contenders=(),
                accepted_trials=accepted_names, notes=notes))
            continue
        valve, strategy = choose_next_valve(parent_of, rank, tested_valves)
        crew = crews[0] if (valve and crews) else None
        if valve is None:
            dispatch, crew = "无未做试验", None
        elif crews:
            dispatch = "补证试验派工"
        else:
            dispatch = "值班协调"
        leader, leader_share = rank[0]
        runner_up, runner_share = rank[1]
        close = leader_share - runner_share <= CLOSE_RACE_MARGIN
        contenders = (leader, runner_up) if close else (leader,)
        notes = (f"独立支撑 {finding['isolation_support']} 次或领先份额 {leader_share:.3f} 未达 {DECISION_SHARE:.0%}，"
                 "不得定案；本夜仅安排一次未做过的独立试验")
        if any(gate.sector == zone and gate.status == "duplicate" for gate in gates):
            notes += "；同一阀点重跑不新增派工"
        orders.append(NightOrder(
            sector=zone, decision="补证待定", decided_branch=None, confidence_share=None,
            isolation_support=finding["isolation_support"],
            next_trial_valve=valve, next_trial_strategy=strategy,
            dispatch=dispatch, crew=crew,
            pending_branches=tuple(node for node, _ in rank), close_contenders=contenders,
            accepted_trials=accepted_names, notes=notes))
    return tuple(orders), tuple(sorted(gates, key=lambda gate: (gate.sector, gate.observed, gate.name)))
