from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from perp_recap.importer import (
    ALIASES,
    detect_utc_offset,
    parse_rows,
    read_csv,
    read_upload,
    suggest_mapping,
    time_audit,
)
from perp_recap.market import INTERVALS, MarketError, coverage, download, ingest_archive
from perp_recap.positions import derive, stream_key
from perp_recap.store import Store

st.set_page_config(page_title="Perp Recap · 合约复盘", page_icon="↗", layout="wide")
st.markdown(
    """<style>
.block-container {max-width:1500px;padding-top:2rem}
h1 {letter-spacing:-1.5px} [data-testid=stMetric] {background:#141c29;padding:16px;border-radius:12px}
.eyebrow {color:#49e6b1;letter-spacing:3px;font-size:12px;font-weight:700}
</style>""",
    unsafe_allow_html=True,
)

db_path = os.environ.get("PERP_RECAP_DB", "data/recap.sqlite3")
store = Store(db_path)


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def shown(ms):
    return utc(ms).astimezone(timezone(timedelta(hours=display_offset)))


def chart_time(ms):
    # Plotly receives explicit wall-clock strings, avoiding the browser's implicit timezone.
    return shown(ms).strftime("%Y-%m-%d %H:%M:%S.%f")


def csv_bytes(rows):
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]))
    writer.writeheader()
    for row in rows:
        # Protect spreadsheet users from formula-like untrusted IDs / labels.
        safe = {}
        for k, v in row.items():
            if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")):
                try:
                    numeric = Decimal(v).is_finite()
                except ArithmeticError:
                    numeric = False
                if not numeric:
                    v = "'" + v
            safe[k] = v
        writer.writerow(safe)
    return buf.getvalue().encode("utf-8-sig")


def fill_view(f):
    return {
        "显示时间": shown(f["time"]).isoformat(),
        "时间 UTC": utc(f["time"]).isoformat(),
        "交易对": f["symbol"],
        "成交方式": f.get("label", f["side"]),
        "成交价格": f["price"],
        "数量": f["quantity"],
        "仓位变化": f.get("action", "待还原"),
        "成交前仓位": f.get("before"),
        "成交后仓位": f.get("after"),
        "手续费": f["fee"],
        "手续费币种": f["fee_asset"],
        "已实现盈亏（CSV）": f["realized_pnl"],
        "订单类型来源": f.get("type_source", "CSV"),
        "盈亏辅助证据": f.get("pnl_evidence", ""),
        "已实现利润规则分类": f.get("profit_rule", "待分析"),
        "仓位判断依据": f.get("position_evidence", ""),
        "订单 ID": f["order_id"],
        "成交 ID": f["trade_id"],
    }


def import_panel(account):
    st.subheader("把你的真实交易放回行情里")
    st.caption(
        "上传币安 U 本位「交易历史」CSV；可另补「订单历史」以识别市价 / 限价。文件仅存于本机。"
    )
    kind = st.radio("导入内容", ["成交历史", "订单历史（补充订单类型）"], horizontal=True)
    uploaded = st.file_uploader("选择 CSV 或币安导出的 ZIP", type=["csv", "zip"])
    with st.expander("先用虚构交易试一试"):
        st.write("示例包含多币种、加减仓、反手和缺失订单类型；所有成交均为虚构。")
        example = Path("examples/fills.csv").read_bytes()
        st.download_button("下载示例成交 CSV", example, "fictional-fills.csv", "text/csv")
        st.download_button(
            "下载示例订单 CSV",
            Path("examples/orders.csv").read_bytes(),
            "fictional-orders.csv",
            "text/csv",
        )
        if st.button("载入虚构示例到 demo 账户"):
            headers, rows = read_csv(example)
            parsed, _ = parse_rows(rows, suggest_mapping(headers))
            store.import_fills("demo", parsed)
            store.save_context(
                "demo", {"mode": "one_way", "initial": {"BTCUSDT:BOTH": "0", "ETHUSDT:BOTH": "0"}}
            )
            st.success("已载入 demo；在左侧切换账户查看。")
    if uploaded is None:
        return
    content = uploaded.getvalue()
    file_key = hashlib.sha256(content).hexdigest()[:16] + kind
    try:
        headers, rows = read_upload(content)
        suggested = suggest_mapping(headers)
    except (ValueError, zipfile.BadZipFile, csv.Error, UnicodeError, RuntimeError) as exc:
        st.error(str(exc))
        return
    st.write(f"检测到 **{len(rows):,} 行** · 导入账户 **{escape(account)}**")
    st.dataframe([{k: v for k, v in r.items() if k != "_line"} for r in rows[:8]], hide_index=True)
    field_labels = {
        "time": "成交时间",
        "symbol": "交易对",
        "side": "买卖方向",
        "price": "成交价格",
        "quantity": "成交数量",
        "fee": "手续费",
        "fee_asset": "手续费币种",
        "realized_pnl": "已实现盈亏",
        "position_side": "持仓方向 LONG/SHORT/BOTH",
        "order_type": "订单类型 MARKET/LIMIT",
        "order_id": "订单 ID",
        "trade_id": "成交 ID",
        "liquidity": "Maker / Taker（不代表订单类型）",
    }
    mapping = {}
    with st.expander(
        "检查字段映射", expanded=not all(f in suggested for f in ("time", "price", "quantity"))
    ):
        cols = st.columns(3)
        for i, field in enumerate(ALIASES):
            if kind != "成交历史" and field not in ("symbol", "order_id", "order_type"):
                continue
            options = ["未提供"] + headers
            val = cols[i % 3].selectbox(
                field_labels[field],
                options,
                index=options.index(suggested[field]) if field in suggested else 0,
                key=file_key + field,
            )
            if val != "未提供":
                mapping[field] = val
    default_offset, offset_source = detect_utc_offset(uploaded.name, mapping.get("time", ""))
    offset = st.number_input(
        "CSV 中未带时区的时间：UTC 偏移小时", -12.0, 14.0, default_offset, 0.25, key=file_key + "tz"
    )
    st.caption(
        f"时区识别依据：{offset_source}。导入时区决定时间的真实含义；左侧显示时区只改变展示。"
    )
    st.caption("截图导出选择 UTC 时填 0；UTC+3 填 3。时间统一转换为 UTC，与合约 K 线对齐。")
    try:
        parsed, errors = parse_rows(
            rows, mapping, offset, "fills" if kind == "成交历史" else "orders"
        )
    except ValueError as exc:
        st.warning(str(exc))
        return
    st.write(f"有效 {len(parsed):,} 行 · 错误 {len(errors):,} 行")
    allow_partial = False
    if errors:
        st.dataframe(errors, hide_index=True)
        allow_partial = st.checkbox(
            "我已检查错误，仅导入有效行（可能导致仓位序列不完整）", key=file_key + "partial"
        )
    if kind == "成交历史" and parsed:
        st.json(time_audit(parsed), expanded=True)
        st.caption(
            "入库后按 UTC 时间排序，同币种同一时刻按数值成交 ID 排序。不同币种的成交 ID 不作跨币种比较。"
        )
        unknown = sum(f["order_type"] == "UNKNOWN" for f in parsed)
        missing_id = sum(not f["trade_id"] for f in parsed)
        if unknown:
            st.info(f"{unknown} 笔缺少可识别订单类型，将标记为未知；可补充含订单 ID 的订单历史。")
        if missing_id:
            st.warning(
                f"{missing_id} 笔没有成交 ID：将使用内容与出现次数去重。不同导出格式、手续费修订或部分重叠记录需人工核对。"
            )
        st.dataframe([fill_view(f) for f in parsed[:20]], hide_index=True)
    auto_rule = False
    if kind == "成交历史":
        auto_rule = st.checkbox(
            "导入后按利润规则自动分析：0 = 开仓/加仓，非 0 = 减仓/平仓（单向持仓、假设期初空仓）",
            value=True,
        )
        st.caption(
            "这是一条可选分类规则；与仓位数量冲突时会提示，不将规则标签当作交易所提供的开平仓字段。"
        )
    if st.button(
        "确认导入", type="primary", disabled=not parsed or bool(errors and not allow_partial)
    ):
        try:
            inserted, duplicates = (
                store.import_fills(account, parsed)
                if kind == "成交历史"
                else store.import_orders(account, parsed)
            )
            st.success(f"新增 {inserted} 条，跳过重复 {duplicates} 条。")
            if kind == "成交历史" and inserted:
                all_fills = store.fills(account)
                if auto_rule and not any(
                    f["position_side"] in ("LONG", "SHORT") for f in all_fills
                ):
                    store.save_context(
                        account,
                        {
                            "mode": "one_way",
                            "basis": "pnl_rule",
                            "initial": {stream_key(f, "one_way"): "0" for f in all_fills},
                        },
                    )
                    st.info(
                        "已应用利润规则 + 假设期初空仓。仓位依据页可修改；冲突会保留在逐笔明细中。"
                    )
                else:
                    st.info("为避免新文件改变历史起点，请在「仓位依据」重新确认期初仓位。")
        except ValueError as exc:
            st.error(str(exc))


def context_panel(account, fills):
    st.subheader("先确认仓位，再解释交易")
    st.write(
        "买入可以是开多，也可以是平空。以下期初数量指该账户全部已导入记录中，各持仓序列第一笔成交之前的仓位。"
    )
    context = store.context(account)
    st.caption(
        "当前模式："
        + {
            "pnl_rule": "已实现利润规则 + 假设期初空仓",
            "assumed_flat": "按数量推算 + 假设期初空仓",
        }.get(context.get("basis"), "按用户确认的期初仓位推算")
    )
    with st.expander("自动推算：假设各币种在记录开始前为空仓", expanded=not context["initial"]):
        st.write(
            "适用于单向持仓：第一笔 BUY 作为开多、SELL 作为开空，再按成交数量逐笔计算。结果标记为推测；非零已实现利润若与开仓判断冲突，则停止该序列的仓位推算。"
        )
        use_rule = st.checkbox("同时应用已实现利润规则：0 开仓/加仓，非 0 减仓/平仓", value=True)
        if st.button("按单向持仓 + 期初空仓自动推算"):
            if any(f["position_side"] in ("LONG", "SHORT") for f in fills):
                st.error("文件包含双向持仓方向，请使用下方独立 LONG / SHORT 期初仓位设置。")
            else:
                store.save_context(
                    account,
                    {
                        "mode": "one_way",
                        "basis": "pnl_rule" if use_rule else "assumed_flat",
                        "initial": {stream_key(f, "one_way"): "0" for f in fills},
                    },
                )
                st.success("已启用推测模式。可在图表复盘页查看，或在下方逐项修正。")
                context = store.context(account)
    modes = {"unknown": "未确认模式", "one_way": "单向持仓", "hedge": "双向持仓"}
    mode = st.selectbox(
        "持仓模式", list(modes), index=list(modes).index(context["mode"]), format_func=modes.get
    )
    keys = sorted({stream_key(f, mode) for f in fills})
    with st.form("position-context"):
        st.caption(
            "未知留空；确认空仓填 0。单向：多仓正数、空仓负数；双向 LONG 填正数、SHORT 填负数。"
        )
        values = {}
        for key in keys:
            first = min(f["time"] for f in fills if stream_key(f, mode) == key)
            values[key] = st.text_input(
                f"{key} · {utc(first):%Y-%m-%d %H:%M:%S} UTC 前的数量",
                value=str(context["initial"].get(key, "")),
            )
        confirmed = st.checkbox("我已核对这些期初仓位；留空的序列保持未知")
        if st.form_submit_button("保存仓位依据"):
            try:
                initial = {}
                for key, value in values.items():
                    if not value.strip():
                        continue
                    q = Decimal(value)
                    if not q.is_finite() or abs(q) > Decimal("1e30"):
                        raise ValueError("仓位数量必须是有限数值。")
                    if key.endswith(":UNKNOWN"):
                        raise ValueError(
                            "未知持仓方向无法设置期初仓位；请确认单向模式或补充方向字段。"
                        )
                    if (key.endswith(":LONG") and q < 0) or (key.endswith(":SHORT") and q > 0):
                        raise ValueError("LONG 必须非负，SHORT 必须非正。")
                    initial[key] = str(q)
                if not confirmed:
                    raise ValueError("请核对并勾选期初仓位确认。")
                store.save_context(account, {"mode": mode, "initial": initial})
                st.success("已保存。复盘页将显示推导结果和判断依据。")
            except (ValueError, ArithmeticError) as exc:
                st.error(str(exc))


def download_panel(fills):
    st.subheader("只下载你需要复盘的行情")
    with st.expander("载入已经下载的官方行情 ZIP"):
        st.caption(
            "此处用于行情包，不是个人交易记录包。支持 SYMBOL-周期-YYYY-MM[-DD].zip；只接受 U 本位合约归档。"
        )
        archive_upload = st.file_uploader(
            "官方 K 线 ZIP（单文件 ≤ 50 MB）", type=["zip"], key="market-archive"
        )
        is_um = st.checkbox("我确认此文件来自币安 data/futures/um 的 K 线归档")
        if archive_upload and st.button("分批载入行情到本地数据库", disabled=not is_um):
            match = re.fullmatch(
                r"(.+)-(1m|3m|5m|15m|30m|1h|2h|4h|6h|12h|1d)-\d{4}-\d{2}(?:-\d{2})?\.zip",
                archive_upload.name,
            )
            if not match:
                st.error("文件名不符合币安 K 线归档格式。")
            else:
                try:
                    from perp_recap.importer import valid_symbol

                    if not valid_symbol(match[1]):
                        raise ValueError("不支持的交易对。")
                    count = ingest_archive(
                        store,
                        archive_upload,
                        match[1],
                        match[2],
                        source="用户确认的 USD-M 归档（未校验官方 SHA256）",
                    )
                    st.success(f"已分批载入 {count:,} 根 K 线。")
                except (
                    ValueError,
                    MarketError,
                    OSError,
                    zipfile.BadZipFile,
                    ArithmeticError,
                    csv.Error,
                ) as exc:
                    st.error(str(exc))
    symbols = sorted({f["symbol"] for f in fills})
    all_symbols = st.checkbox("下载全部已导入币种", value=True)
    selected = symbols if all_symbols else st.multiselect("选择币种", symbols, default=symbols[:1])
    col1, col2, col3 = st.columns(3)
    interval = col1.selectbox("K 线周期", list(INTERVALS), index=2)
    padding = col2.number_input("交易前后各扩展（小时）", 0, 168, 12)
    source = col3.selectbox(
        "行情来源",
        ["archive", "ccxt"],
        format_func=lambda x: (
            "官方 ZIP 全量归档（按月 / 日）" if x == "archive" else "CCXT 合约历史数据（分页）"
        ),
    )
    st.caption(
        "归档模式下载涉及月份的完整官方 ZIP 并保留在本机；当前月或缺少月包时按日补齐。CCXT 按指定时间段分页获取。两者均为 UTC 合约 K 线。"
    )
    custom = st.checkbox("手动限定下载日期（UTC）")
    dates = None
    if custom:
        dates = st.date_input(
            "日期范围", (utc(fills[0]["time"]).date(), utc(fills[-1]["time"]).date())
        )
        if not isinstance(dates, tuple) or len(dates) != 2:
            st.info("请选择开始和结束日期。")
            return
    plans = []
    for symbol in selected:
        times = [f["time"] for f in fills if f["symbol"] == symbol]
        start, end = min(times) - padding * 3600000, max(times) + padding * 3600000
        if dates:
            start = int(
                datetime.combine(dates[0], datetime.min.time(), timezone.utc).timestamp() * 1000
            )
            end = (
                int(
                    datetime.combine(dates[1], datetime.min.time(), timezone.utc).timestamp() * 1000
                )
                + 86400000
                - 1
            )
        plans.append((symbol, start, end))
    report = [
        {
            "币种": s,
            "开始 UTC": str(utc(a)),
            "结束 UTC": str(utc(b)),
            **coverage(store, s, interval, a, b),
        }
        for s, a, b in plans
    ]
    if report:
        st.dataframe(report, hide_index=True)
    if st.button("下载所选行情 / 补齐缓存", type="primary", disabled=not plans):
        progress = st.progress(0.0, "准备下载")
        for i, (symbol, start, end) in enumerate(plans):
            try:
                result = download(
                    store,
                    symbol,
                    interval,
                    start,
                    end,
                    source,
                    lambda p, text: progress.progress((i + p) / len(plans), text),
                )
                if result["missing"]:
                    st.warning(
                        f"{symbol}：已缓存 {result['cached']:,} 根，仍缺 {result['missing']:,} 根。缺口可能来自未上市、已下架、归档未生成或数据缺失。"
                    )
                else:
                    st.success(f"{symbol}：区间完整，已缓存 {result['cached']:,} 根。")
            except (MarketError, ValueError, OSError) as exc:
                st.error(f"{symbol}：{exc}")
                # Stop the entire batch after an upstream failure, including rate limits.
                break
        progress.empty()


def chart_panel(account, fills):
    st.subheader("每一次买卖，都回到当时的位置")
    context = store.context(account)
    derived = derive(fills, context)  # Derive the entire account before applying chart filters.
    col1, col2 = st.columns([1, 2])
    symbol = col1.selectbox("交易对", sorted({f["symbol"] for f in derived}), key="review-symbol")
    selected = [f for f in derived if f["symbol"] == symbol]
    choices = {"all": "按时间窗口浏览"}
    choices.update(
        {
            f[
                "identity"
            ]: f"{shown(f['time']):%Y-%m-%d %H:%M:%S} · {f['label']} · {f['action']} · {f['price']}"
            for f in selected
        }
    )
    focus = col2.selectbox("定位一笔成交", list(choices), format_func=choices.get)
    c1, c2 = st.columns(2)
    interval = c1.selectbox("图表周期", list(INTERVALS), index=2)
    hours = c2.slider("选中成交前后观察小时数", 1, 168, 12)
    active = next((f for f in selected if f["identity"] == focus), None)
    center = active["time"] if active else selected[-1]["time"]
    if not active:
        nav = st.columns([1, 2, 2, 1])
        date_key, time_key = f"date:{symbol}:{display_offset}", f"time:{symbol}:{display_offset}"
        if date_key not in st.session_state:
            st.session_state[date_key] = shown(center).date()
            st.session_state[time_key] = shown(center).time().replace(tzinfo=None)
        previous = nav[0].button("← 前一窗口")
        following = nav[3].button("后一窗口 →")
        if previous or following:
            dt = datetime.combine(
                st.session_state[date_key], st.session_state[time_key]
            ) + timedelta(hours=hours * 2 * (-1 if previous else 1))
            st.session_state[date_key], st.session_state[time_key] = dt.date(), dt.time()
        date = nav[1].date_input("窗口中心日期（显示时区）", key=date_key)
        clock = nav[2].time_input("窗口中心时间", key=time_key)
        center = int(
            datetime.combine(date, clock, timezone(timedelta(hours=display_offset))).timestamp()
            * 1000
        )
    start, end = center - hours * 3600000, center + hours * 3600000
    visible = [f for f in selected if start <= f["time"] <= end]
    start = start // INTERVALS[interval] * INTERVALS[interval]
    candle_count = store.candle_count(symbol, interval, start, end)
    candles = store.candles(symbol, interval, start, end) if candle_count <= 8000 else []
    st.caption(
        f"当前显示时区 UTC{display_offset:+g}；K 线和成交点同步转换。仅查询当前窗口，单次图表上限 8,000 根。"
    )
    if st.button("用 CCXT 获取 / 补齐当前窗口"):
        try:
            with st.spinner("按当前窗口分页下载…"):
                result = download(store, symbol, interval, start, end, "ccxt")
            if result["missing"]:
                st.warning(f"仍缺 {result['missing']} 根；可改用官方归档。")
            st.rerun()
        except (MarketError, ValueError, OSError) as exc:
            st.error(str(exc))
    a, b, c, d = st.columns(4)
    a.metric("成交笔数", len(visible))
    b.metric("订单类型已知", sum(f["order_type"] != "UNKNOWN" for f in visible))
    c.metric("仓位变化可推导", sum(bool(f["effects"]) for f in visible))
    d.metric("本区间 K 线", f"{candle_count:,}")
    st.caption(
        "▲ 买入 · ▼ 卖出；纵坐标是实际成交价。市价 / 限价来自文件字段；开平仓及加减仓来自仓位推导。"
    )
    if candle_count > 8000:
        st.warning(
            "本窗口超过 8,000 根 K 线；请缩短观察小时数，或下载并切换更大周期。未读取全部行情到内存。"
        )
    elif not candles:
        st.info(
            "这个币种 / 周期 / 时间范围还没有行情。请先到「下载行情」获取；下方仍可查看成交明细。"
        )
    else:
        cov = coverage(store, symbol, interval, start, end)
        if cov["missing"]:
            st.warning(f"观察区间还缺 {cov['missing']:,} 根已收盘 K 线；缺口保持空白。")
        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True, row_heights=[0.78, 0.22], vertical_spacing=0.04
        )
        x = [chart_time(c["time"]) for c in candles]
        fig.add_trace(
            go.Candlestick(
                x=x,
                open=[float(c["open"]) for c in candles],
                high=[float(c["high"]) for c in candles],
                low=[float(c["low"]) for c in candles],
                close=[float(c["close"]) for c in candles],
                name="合约 K 线",
                increasing_line_color="#49e6b1",
                decreasing_line_color="#ff6b86",
            ),
            row=1,
            col=1,
        )
        colors = [
            "#49e6b1" if Decimal(c["close"]) >= Decimal(c["open"]) else "#ff6b86" for c in candles
        ]
        fig.add_trace(
            go.Bar(
                x=x,
                y=[float(c["volume"]) for c in candles],
                marker_color=colors,
                name="成交量（基础币）",
                opacity=0.5,
            ),
            row=2,
            col=1,
        )
        for side, color, marker in [
            ("BUY", "#58baff", "triangle-up"),
            ("SELL", "#ffcc66", "triangle-down"),
        ]:
            marks = [f for f in visible if f["side"] == side]
            fig.add_trace(
                go.Scatter(
                    x=[chart_time(f["time"]) for f in marks],
                    y=[float(f["price"]) for f in marks],
                    mode="markers",
                    name="买入" if side == "BUY" else "卖出",
                    customdata=[f["identity"] for f in marks],
                    marker={
                        "symbol": marker,
                        "color": color,
                        "size": 14,
                        "line": {"color": "#0b1018", "width": 1},
                    },
                    text=[
                        escape(
                            f"{f['label']} | {f['action']}<br>数量 {f['quantity']} | 价格 {f['price']}<br>{f['position_evidence']}"
                        ).replace("&lt;br&gt;", "<br>")
                        for f in marks
                    ],
                    hovertemplate="%{x}<br>%{text}<extra></extra>",
                ),
                row=1,
                col=1,
            )
        if active:
            fig.add_hline(
                y=float(active["price"]), line_dash="dot", line_color="#8b99ae", row=1, col=1
            )
        fig.update_layout(
            height=680,
            template="plotly_dark",
            paper_bgcolor="#0b1018",
            plot_bgcolor="#0b1018",
            margin={"l": 10, "r": 10, "t": 20, "b": 10},
            xaxis_rangeslider_visible=False,
            legend_orientation="h",
            dragmode="pan",
            uirevision=f"{symbol}-{interval}-{focus}-{hours}-{center}-{display_offset}",
        )
        fig.update_xaxes(
            range=[chart_time(start), chart_time(end)],
            showgrid=True,
            gridcolor="#1d2838",
            type="date",
        )
        fig.update_yaxes(gridcolor="#1d2838")
        st.plotly_chart(fig, width="stretch", config={"scrollZoom": True, "displaylogo": False})
        st.caption(
            "滚轮缩放、拖动查看已载入窗口；用上方日期和前后窗口按钮按需读取更多历史，拖动本身不触发全库加载。数据源："
            + " / ".join(sorted({c["source"] for c in candles}))
        )
    if active:
        st.markdown("**选中成交详情**")
        st.json(fill_view(active), expanded=True)
        if len(active["effects"]) > 1:
            st.write("此成交跨越零仓位，拆解为：", active["effects"])
    views = [fill_view(f) for f in visible]
    st.dataframe(views, hide_index=True, width="stretch")
    st.download_button("导出当前成交复盘 CSV", csv_bytes(views), f"{symbol}-review.csv", "text/csv")
    if candles:
        st.download_button(
            "导出当前合约行情 CSV（含成交量、主动买入量、成交笔数）",
            csv_bytes(candles),
            f"{symbol}-{interval}-candles.csv",
            "text/csv",
        )
    st.caption(
        "已实现盈亏和手续费按 CSV 原值保留；未加入个人资金费流水，不将其称为最终净收益。不同手续费币种不会直接相加。"
    )


with st.sidebar:
    st.markdown("### ↗ Perp Recap")
    st.caption("合约复盘 · 本地优先 · v0.1")
    display_offset = st.number_input(
        "显示时区（UTC 偏移）",
        -12.0,
        14.0,
        0.0,
        0.25,
        help="例如 UTC=0，北京=8。仅改变 K 线和成交时间的展示，不改动 UTC 数据。",
    )
    accounts = store.accounts()
    choice = st.selectbox("账户", accounts + ["＋ 新账户"], index=0)
    account = (
        st.text_input("本地账户名称", "main", max_chars=80) if choice == "＋ 新账户" else choice
    )
    account = account.strip()
    st.divider()
    page = st.radio(
        "工作区", ["导入记录", "仓位依据", "下载行情", "图表复盘"], label_visibility="collapsed"
    )
    st.divider()
    st.caption("账户仅用于隔离文件。无需币安 API Key。公开仓库不包含你的交易记录。")
    st.caption("首版支持 USDT / USDC 线性永续；不支持币本位反向合约。")

st.markdown('<div class="eyebrow">YOUR TRADES. IN CONTEXT.</div>', unsafe_allow_html=True)
st.title("看清每一笔交易")
st.caption("导入成交 → 选择币种下载行情 → 将每次操作放回真实合约 K 线。")
if not account:
    st.info("请填写账户名称。")
elif page == "导入记录":
    import_panel(account)
else:
    fills = store.fills(account)
    if not fills:
        st.info("这个账户还没有成交记录，请先导入 CSV。")
    else:
        if account == "demo":
            st.warning("演示账户：成交记录均为虚构；下载的行情来自币安真实合约市场。")
        if page == "仓位依据":
            context_panel(account, fills)
        elif page == "下载行情":
            download_panel(fills)
        else:
            chart_panel(account, fills)
store.close()
