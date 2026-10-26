"""CSV is untrusted input. Never infer order type from liquidity role."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

ALIASES = {
    "time": [
        "time",
        "date",
        "datetime",
        "timestamp",
        "trade time",
        "date(utc)",
        "date(utc+0)",
        "时间",
        "日期",
        "成交时间",
        "交易时间",
    ],
    "symbol": [
        "symbol",
        "pair",
        "trading pair",
        "contract",
        "交易对",
        "合约",
        "币种",
        "代币名称/币种名称/币对",
    ],
    "side": ["side", "direction", "买卖方向", "方向", "交易方向"],
    "price": ["price", "executed price", "fill price", "成交价格", "成交价", "价格"],
    "quantity": ["quantity", "qty", "executed qty", "executed", "成交数量", "数量"],
    "fee": ["fee", "commission", "trading fee", "手续费", "交易手续费"],
    "fee_asset": ["fee coin", "fee asset", "commissionasset", "手续费币种", "手续费资产"],
    "realized_pnl": [
        "realized profit",
        "realizedpnl",
        "realized pnl",
        "realizedprofit",
        "已实现盈亏",
        "实现盈亏",
        "已实现收益",
        "已实现利润",
    ],
    "position_side": ["position side", "positionside", "持仓方向", "仓位方向"],
    "order_type": ["order type", "ordertype", "订单类型", "委托类型"],
    "order_id": ["order id", "orderid", "订单号", "订单编号", "订单id"],
    "trade_id": ["trade id", "tradeid", "成交id", "成交编号", "交易id"],
    "liquidity": ["role", "liquidity", "maker", "ismaker", "流动性", "角色", "做市商"],
}
REQUIRED = {"time", "symbol", "side", "price", "quantity"}
ORDER_TYPES = {
    "MARKET": "MARKET",
    "市价": "MARKET",
    "市价单": "MARKET",
    "LIMIT": "LIMIT",
    "限价": "LIMIT",
    "限价单": "LIMIT",
    "STOP": "STOP",
    "STOP_MARKET": "STOP_MARKET",
    "TAKE_PROFIT": "TAKE_PROFIT",
    "TAKE_PROFIT_MARKET": "TAKE_PROFIT_MARKET",
    "TRAILING_STOP_MARKET": "TRAILING_STOP_MARKET",
    "LIQUIDATION": "LIQUIDATION",
    "止损限价": "STOP",
    "止损市价": "STOP_MARKET",
    "止盈限价": "TAKE_PROFIT",
    "止盈市价": "TAKE_PROFIT_MARKET",
}


def valid_symbol(symbol):
    # Binance also lists single-letter and Unicode base asset names.
    quote = "USDT" if symbol.endswith("USDT") else "USDC" if symbol.endswith("USDC") else ""
    base = symbol[: -len(quote)] if quote else ""
    return bool(base and len(base) <= 40 and base.isalnum())


def read_upload(content: bytes):
    """Read bounded CSV or a multi-part CSV ZIP without extracting paths."""
    if not content.startswith(b"PK"):
        return read_csv(content)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        files = [
            i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith(".csv")
        ]
        if not files or len(files) > 100 or sum(i.file_size for i in files) > 100 * 1024 * 1024:
            raise ValueError("压缩包需含 1–100 个 CSV，解压总量不超过 100 MB。")
        headers, rows = None, []
        for entry in files:
            part_headers, part_rows = read_csv(archive.read(entry))
            if headers is not None and part_headers != headers:
                raise ValueError("压缩包内 CSV 表头不一致，请分别导入。")
            headers = part_headers
            for row in part_rows:
                row["_line"] = f"{entry.filename}:{row['_line']}"
            rows.extend(part_rows)
        return headers, rows


def detect_utc_offset(name: str, header: str = ""):
    """Return (offset, source) without guessing a local timezone."""
    for source, text in [("时间列表头", header), ("导出文件名", name)]:
        match = re.search(r"UTC\s*([+-]?\d{1,2})?(?::(\d{2}))?", text, re.I)
        if match:
            raw = match[1] or "0"
            h = int(raw)
            offset = h + int(match[2] or 0) / 60 * (-1 if raw.startswith("-") else 1)
            if -12 <= offset <= 14:
                return offset, source
    return 0.0, "默认 UTC，需核对导出设置"


def time_audit(fills):
    times = [f["time"] for f in fills]
    if not times:
        return {}
    up = sum(a < b for a, b in zip(times, times[1:]))
    down = sum(a > b for a, b in zip(times, times[1:]))
    return {
        "原文件排序": "混合顺序"
        if up and down
        else "升序"
        if up
        else "降序"
        if down
        else "同一时间",
        "最早 UTC": datetime.fromtimestamp(min(times) / 1000, timezone.utc).isoformat(),
        "最晚 UTC": datetime.fromtimestamp(max(times) / 1000, timezone.utc).isoformat(),
        "相邻同秒成交": sum(a // 1000 == b // 1000 for a, b in zip(times, times[1:])),
        "币种数": len({f["symbol"] for f in fills}),
    }


def normalized(value: str) -> str:
    return re.sub(r"[\s_\-()（）\[\]]", "", value).lower().lstrip("\ufeff")


def suggest_mapping(headers: list[str]) -> dict[str, str]:
    result = {}
    for field, names in ALIASES.items():
        matches = [h for h in headers if normalized(h) in {normalized(n) for n in names}]
        if field == "time" and not matches:
            matches = [
                h
                for h in headers
                if re.fullmatch(
                    r"(?:date|time|时间|日期|成交时间|交易时间)\s*[（(]?UTC[+-]?\d*(?::\d+)?[)）]?",
                    h,
                    re.I,
                )
            ]
        if len(matches) == 1:
            result[field] = matches[0]
    return result


def read_csv(content: bytes) -> tuple[list[str], list[dict]]:
    if len(content) > 50 * 1024 * 1024:
        raise ValueError("文件超过 50 MB，请分段导出。")
    encodings = (
        ["utf-16"] if content.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
    )
    text = None
    for encoding in encodings:
        try:
            text = content.decode(encoding)
            break
        except UnicodeError:
            continue
    if text is None:
        raise ValueError("无法识别编码，请保存为 UTF-8 CSV。")
    if "\x00" in text:
        raise ValueError("文件包含空字节，不是受支持的 CSV。")
    # Binance exports can prepend report metadata before the actual header.
    lines = text.splitlines()
    start = 0
    delimiter = ","
    for i, line in enumerate(lines[:30]):
        found = False
        for sep in (",", ";", "\t"):
            headers = next(csv.reader([line], delimiter=sep))
            mapped = suggest_mapping(headers)
            if "symbol" in mapped and len(mapped) >= 3:
                start, delimiter, found = i, sep, True
                break
        if found:
            break
    else:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t").delimiter
        except csv.Error:
            pass
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])), delimiter=delimiter)
    original = reader.fieldnames or []
    headers = [h.strip() for h in original]
    if not headers or len(headers) != len(set(headers)):
        raise ValueError("表头为空或重复，请检查 CSV。")
    reader.fieldnames = headers
    rows = []
    for row in reader:
        if None in row:
            raise ValueError(f"第 {start + reader.line_num} 行字段数超过表头，检查分隔符。")
        if any(str(v or "").strip() for v in row.values()):
            rows.append(
                {**{k: (v or "").strip() for k, v in row.items()}, "_line": start + reader.line_num}
            )
    if not rows:
        raise ValueError("CSV 没有数据行。")
    return headers, rows


def number(value: str, *, positive=False) -> str:
    # Numeric value with an optional explicit asset suffix, e.g. '0.01 BTC'.
    cleaned = value.strip().replace(",", "")
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([A-Za-z]+)?", cleaned)
    if not match:
        raise ValueError(f"无法识别数值：{value[:60]}")
    try:
        amount = Decimal(match[1])
    except InvalidOperation as exc:
        raise ValueError("无效数值") from exc
    if not amount.is_finite() or abs(amount) > Decimal("1e30") or (positive and amount <= 0):
        raise ValueError("数值必须有限；价格与数量必须大于零。")
    return format(amount, "f")


def parse_time(value: str, utc_offset: float) -> int:
    if re.fullmatch(r"\d{10,16}", value):
        n = int(value)
        n = n * 1000 if len(value) == 10 else n // 1000 if len(value) == 16 else n
        if len(value) not in (10, 13, 16):
            raise ValueError("时间戳应为秒、毫秒或微秒。")
    else:
        value = value.replace("/", "-").replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("日期应为 YYYY-MM-DD HH:MM:SS，或 Unix 时间戳。") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=utc_offset)))
        n = int(dt.timestamp() * 1000)
    if not 1483228800000 <= n <= int(datetime.now(timezone.utc).timestamp() * 1000) + 86400000:
        raise ValueError("成交日期不在合理范围（2017 年至明天）。")
    return n


def parse_rows(
    rows: list[dict], mapping: dict[str, str], utc_offset=0.0, kind="fills"
) -> tuple[list[dict], list[dict]]:
    required = REQUIRED if kind == "fills" else {"symbol", "order_id", "order_type"}
    missing = required - {k for k, v in mapping.items() if v}
    if missing:
        raise ValueError("缺少字段映射：" + ", ".join(sorted(missing)))
    selected = [v for v in mapping.values() if v]
    if len(selected) != len(set(selected)):
        raise ValueError("一个 CSV 列不能同时映射到多个字段。")
    users = {row.get("用户ID", row.get("User ID", "")) for row in rows}
    if len(users - {""}) > 1:
        raise ValueError("文件包含多个用户的记录，请分账户导入。")
    parsed, errors = [], []
    occurrences = Counter()
    for row in rows:

        def get(field):
            return str(row.get(mapping.get(field, ""), "")).strip()

        try:
            symbol = (
                get("symbol").upper().replace("/", "").replace(":USDT", "").replace(":USDC", "")
            )
            if not valid_symbol(symbol):
                raise ValueError("首版仅支持以 USDT / USDC 结算的线性永续交易对。")
            order_type = ORDER_TYPES.get(get("order_type").upper(), "UNKNOWN")
            item = {
                "symbol": symbol,
                "order_id": get("order_id"),
                "order_type": order_type,
                "order_type_raw": get("order_type"),
            }
            if kind == "orders":
                if not item["order_id"] or order_type == "UNKNOWN":
                    raise ValueError("订单补充记录需要订单 ID 和可识别的订单类型。")
                parsed.append(item)
                continue
            side = {
                "BUY": "BUY",
                "SELL": "SELL",
                "买入": "BUY",
                "卖出": "SELL",
                "买": "BUY",
                "卖": "SELL",
            }.get(get("side").upper())
            if side is None:
                raise ValueError("买卖方向应为 BUY/SELL 或 买入/卖出；不要映射持仓方向。")
            ps = {
                "LONG": "LONG",
                "SHORT": "SHORT",
                "BOTH": "BOTH",
                "多": "LONG",
                "空": "SHORT",
                "多头": "LONG",
                "空头": "SHORT",
                "单向": "BOTH",
                "": "UNKNOWN",
            }.get(get("position_side").upper())
            if ps is None:
                raise ValueError("持仓方向无法识别。")
            fee = get("fee")
            suffix = re.search(r"([A-Za-z]+)$", fee)
            asset = get("fee_asset").upper() or (suffix[1].upper() if suffix else "UNKNOWN")
            if fee and suffix and get("fee_asset") and suffix[1].upper() != asset:
                raise ValueError("手续费币种与手续费数值后缀冲突。")
            item.update(
                time=parse_time(get("time"), utc_offset),
                side=side,
                price=number(get("price"), positive=True),
                quantity=number(get("quantity"), positive=True),
                fee=number(fee) if fee else None,
                fee_asset=asset,
                realized_pnl=number(get("realized_pnl")) if get("realized_pnl") else None,
                position_side=ps,
                trade_id=get("trade_id"),
                liquidity=get("liquidity"),
            )
            canonical = json.dumps(item, sort_keys=True, ensure_ascii=False)
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            occurrences[digest] += 1
            item["identity"] = (
                f"id:{symbol}:{item['trade_id']}"
                if item["trade_id"]
                else f"hash:{digest}:{occurrences[digest]}"
            )
            item["source_line"] = row["_line"]
            parsed.append(item)
        except (ValueError, OverflowError) as exc:
            errors.append({"line": row["_line"], "error": str(exc)})
    return parsed, errors
