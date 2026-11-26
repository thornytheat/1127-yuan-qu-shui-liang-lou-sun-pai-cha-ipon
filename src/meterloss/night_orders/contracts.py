from dataclasses import asdict, dataclass
from math import isfinite


@dataclass(frozen=True, slots=True)
class NightOrder:
    """单分区下一夜的隔离指令（一个分区只出一份）。"""

    sector: str
    decision: str
    decided_branch: str | None
    confidence_share: float | None
    isolation_support: int
    next_trial_valve: str | None
    next_trial_strategy: str | None
    dispatch: str
    crew: str | None
    pending_branches: tuple[str, ...]
    close_contenders: tuple[str, ...]
    accepted_trials: tuple[str, ...]
    notes: str

    def as_record(self) -> dict:
        row = asdict(self)
        row["pending_branches"] = list(self.pending_branches)
        row["close_contenders"] = list(self.close_contenders)
        row["accepted_trials"] = list(self.accepted_trials)
        return row


@dataclass(frozen=True, slots=True)
class TrialGate:
    """单条阀门试验的截点复核结论。"""

    name: str
    sector: str
    interrupted: str
    observed: int
    status: str
    reason: str

    def __post_init__(self):
        if self.status not in {"accepted", "late", "unreviewed", "cross_zone", "duplicate", "unknown_valve"}:
            raise ValueError("未知的试验复核状态")
        if type(self.observed) is not int or not isfinite(self.observed):
            raise ValueError("试验观测时点非法")

    def as_record(self) -> dict:
        return asdict(self)


COLUMN_LABELS = {
    "sector": "分区",
    "decision": "本班裁决",
    "decided_branch": "定案支段",
    "confidence_share": "定案份额",
    "isolation_support": "独立支撑次数",
    "next_trial_valve": "下一夜试验阀点",
    "next_trial_strategy": "验证策略",
    "dispatch": "派工状态",
    "crew": "承接班组",
    "pending_branches": "待补测支段",
    "close_contenders": "接近支段",
    "accepted_trials": "采信试验",
    "notes": "裁决说明",
}
