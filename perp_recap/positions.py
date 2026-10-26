"""Position effects are derived, not exchange facts. Unknown starts stay unknown."""

from collections import defaultdict
from decimal import Decimal

TYPE_LABELS = {
    "MARKET": "市价",
    "LIMIT": "限价",
    "UNKNOWN": "类型未知",
    "STOP": "止损限价",
    "STOP_MARKET": "止损市价",
    "TAKE_PROFIT": "止盈限价",
    "TAKE_PROFIT_MARKET": "止盈市价",
    "TRAILING_STOP_MARKET": "跟踪止损市价",
    "LIQUIDATION": "强平",
}


def stream_key(fill, mode):
    side = fill["position_side"]
    if side == "UNKNOWN" and mode == "one_way":
        side = "BOTH"
    return f"{fill['symbol']}:{side}"


def derive(fills, context):
    mode, initial = context.get("mode", "unknown"), context.get("initial", {})
    states = {k: Decimal(str(v)) for k, v in initial.items() if v is not None}
    buckets = defaultdict(list)
    for fill in fills:
        buckets[(stream_key(fill, mode), fill["time"])].append(fill)
    ambiguous = {
        k
        for k, group in buckets.items()
        if len({f["side"] for f in group}) > 1 and not all(f["trade_id"].isdigit() for f in group)
    }
    output = []
    for f in fills:
        key = stream_key(f, mode)
        ps = key.rsplit(":", 1)[1]
        before = states.get(key)
        note = (
            "基于假设期初空仓（未核实）和成交序列推导"
            if context.get("basis") in ("assumed_flat", "pnl_rule")
            else "基于用户确认期初仓位和成交序列推导"
        )
        delta = Decimal(f["quantity"]) * (1 if f["side"] == "BUY" else -1)
        effects = []
        if (key, f["time"]) in ambiguous:
            before = None
            states.pop(key, None)
            note = "同一时刻存在反向成交且缺少数值成交 ID，先后顺序不确定"
        if ps == "UNKNOWN":
            before = None
            note = "缺少持仓方向；请确认单向模式，或补充 LONG/SHORT 字段"
        elif (mode == "one_way" and ps != "BOTH") or (mode == "hedge" and ps == "BOTH"):
            before = None
            states.pop(key, None)
            note = "所选持仓模式与 CSV 持仓方向冲突"
        elif before is None and note.startswith("基于"):
            note = "期初仓位未知，不能确定开仓、加仓、减仓或平仓"
        after = None
        if before is not None:
            after = before + delta
            if (ps == "LONG" and (before < 0 or after < 0)) or (
                ps == "SHORT" and (before > 0 or after > 0)
            ):
                after = None
                states.pop(key, None)
                note = "成交超出已知持仓，可能缺少更早的成交或期初仓位"
            else:
                if before == 0:
                    effects = [{"action": "开多" if delta > 0 else "开空", "qty": str(abs(delta))}]
                elif before * delta > 0:
                    effects = [{"action": "加多" if before > 0 else "加空", "qty": str(abs(delta))}]
                elif abs(delta) < abs(before):
                    effects = [{"action": "减多" if before > 0 else "减空", "qty": str(abs(delta))}]
                else:
                    effects = [
                        {"action": "平多" if before > 0 else "平空", "qty": str(abs(before))}
                    ]
                    if after:
                        effects.append(
                            {"action": "开多" if after > 0 else "开空", "qty": str(abs(after))}
                        )
                states[key] = after
        pnl = Decimal(f["realized_pnl"]) if f["realized_pnl"] is not None else None
        rule_class = "缺少已实现利润" if pnl is None else "开仓/加仓" if pnl == 0 else "减仓/平仓"
        contains_close = any(e["action"] in ("平多", "平空", "减多", "减空") for e in effects)
        if pnl is not None and pnl != 0 and not contains_close:
            # Nonzero exchange realized P&L contradicts a pure opening / adding fill.
            # Invalidate uncertain reconstruction instead of inventing an initial quantity.
            effects, after = [], None
            states.pop(key, None)
            note = "非零已实现利润表明包含平仓，但现有仓位依据无法还原；请核对期初仓位或缺失成交"
        conflict = context.get("basis") == "pnl_rule" and pnl == 0 and contains_close
        if conflict:
            effects, after = [], None
            states.pop(key, None)
            note = "利润规则归为开仓，但买卖方向与已知仓位相反；规则与仓位数量冲突，等待核对"
        action = " + ".join(e["action"] for e in effects) or "仓位变化未知"
        if context.get("basis") == "pnl_rule" and not effects and pnl is not None:
            action = rule_class + "（利润规则；仓位待核对）"
        label = TYPE_LABELS.get(f["order_type"], "类型未知") + (
            "买入" if f["side"] == "BUY" else "卖出"
        )
        output.append(
            {
                **f,
                "stream": key,
                "label": label,
                "effects": effects,
                "action": action,
                "profit_rule": rule_class,
                "before": str(before) if before is not None else None,
                "after": str(after) if after is not None else None,
                "pnl_evidence": "包含平仓"
                if pnl is not None and pnl != 0
                else "零 / 缺失已实现利润不能单独判断开平",
                "position_evidence": note,
            }
        )
    return output
