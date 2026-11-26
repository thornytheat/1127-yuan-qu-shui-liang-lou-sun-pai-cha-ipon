from dataclasses import asdict
from .contracts import COLUMN_LABELS
from .planner import plan_night_orders


def build_orders(pipes, trials, roster, cutoff):
    orders, gates = plan_night_orders(pipes, trials, roster, cutoff)
    return orders, gates


def order_records(orders):
    return [order.as_record() for order in orders]


def gate_records(gates):
    return [gate.as_record() for gate in gates]


def stage_view(orders, gates, cutoff):
    decided = sum(order.decision == "定案隔离" for order in orders)
    pending = sum(order.decision == "补证待定" for order in orders)
    dispatched = sum(order.crew is not None for order in orders)
    summary = (f"截点 {cutoff}：{decided} 个分区定案隔离，{pending} 个分区补证待定，"
               f"已派 {dispatched} 个双资质班组；迟到与失准记录不改变本班指令")
    return {
        "title": "夜班隔离指令",
        "summary": summary,
        "columns": COLUMN_LABELS,
        "records": order_records(orders),
        "errors": gate_records(gates),
    }
