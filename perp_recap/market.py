"""Public USD-M contract-price candles only. No spot fallback or private API."""

from __future__ import annotations

import csv
import hashlib
import io
import time
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import requests

from perp_recap.importer import valid_symbol

INTERVALS = {
    "1m": 60000,
    "3m": 180000,
    "5m": 300000,
    "15m": 900000,
    "30m": 1800000,
    "1h": 3600000,
    "2h": 7200000,
    "4h": 14400000,
    "6h": 21600000,
    "12h": 43200000,
    "1d": 86400000,
}
MAX_BARS = 500_000


class MarketError(Exception):
    pass


def get(url, params=None, stream=False):
    last_error = ""
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=(5, 20), stream=stream)
            if r.status_code in (418, 429):
                raise MarketError(
                    f"币安限流，已停止本次下载。请在 {r.headers.get('Retry-After', '60')} 秒后重试；已完成部分已缓存。"
                )
            if r.status_code in (403, 451):
                raise MarketError(
                    "此网络无法访问该币安数据源；可选择公开归档，或在可访问的网络重试。"
                )
            if r.status_code == 404:
                return None
            if r.status_code >= 500:
                raise requests.RequestException(f"服务暂不可用 ({r.status_code})")
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last_error = str(exc)
            if attempt < 2:
                time.sleep(0.5 * 2**attempt)
    raise MarketError("下载失败，已缓存部分可保留重试：" + last_error[:180])


def candle(row, source):
    t = int(row[0])
    if t > 10**14:
        t //= 1000
    vals = [Decimal(str(row[i])) for i in (1, 2, 3, 4, 5, 7, 9, 10)]
    if any(not v.is_finite() for v in vals):
        raise MarketError("行情包含无效数值。")
    o, h, low, c, volume, quote, taker, taker_quote = vals
    if low <= 0 or h < max(o, c) or low > min(o, c) or min(volume, quote, taker, taker_quote) < 0:
        raise MarketError("行情 OHLC / 成交量校验失败。")
    return {
        "time": t,
        "open": str(o),
        "high": str(h),
        "low": str(low),
        "close": str(c),
        "volume": str(volume),
        "quote_volume": str(quote),
        "trades": int(row[8]),
        "taker_buy_volume": str(taker),
        "taker_buy_quote": str(taker_quote),
        "source": source,
    }


def bounds(start, end, interval, now=None):
    step = INTERVALS[interval]
    now = int(time.time() * 1000) if now is None else now
    # Only cache closed bars; the current candle cannot become stale forever.
    return start // step * step, min(end // step * step, now // step * step - step)


def coverage(store, symbol, interval, start, end):
    start, end = bounds(start, end, interval)
    expected = max(0, (end - start) // INTERVALS[interval] + 1)
    cached = store.candle_count(symbol, interval, start, end) if expected else 0
    return {"expected": expected, "cached": cached, "missing": max(0, expected - cached)}


def ingest_archive(
    store, file, symbol, interval, start=0, end=None, source="Binance USD-M archive"
):
    """Stream decompression into bounded SQLite batches. Never materialize a whole ZIP."""
    end = bounds(0, int(time.time() * 1000), interval)[1] if end is None else end
    total, batch = 0, []
    with zipfile.ZipFile(file) as archive:
        files = [i for i in archive.infolist() if i.filename.endswith(".csv")]
        if len(files) != 1 or files[0].file_size > 512 * 1024 * 1024:
            raise MarketError("行情归档应包含一个 CSV，解压大小不超过 512 MB。")
        with archive.open(files[0]) as raw:
            for row in csv.reader(io.TextIOWrapper(raw, encoding="utf-8-sig")):
                if not row or row[0] in ("open_time", "open time"):
                    continue
                c = candle(row, source)
                if start <= c["time"] <= end and c["time"] % INTERVALS[interval] == 0:
                    batch.append(c)
                if len(batch) == 1000:
                    store.put_candles(symbol, interval, batch)
                    total += len(batch)
                    batch.clear()
    if batch:
        store.put_candles(symbol, interval, batch)
        total += len(batch)
    return total


def fetch_archive(store, symbol, interval, period, date):
    """Keep the official ZIP on disk, verify SHA256, then stream its full contents."""
    name = f"{symbol}-{interval}-{date}.zip"
    folder = Path(store.path).parent / "archives" / symbol / interval
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    url = f"https://data.binance.vision/data/futures/um/{period}/klines/{symbol}/{interval}/{name}"
    checksum = get(url + ".CHECKSUM")
    if checksum is None:
        return False
    expected = checksum.text.split()[0]
    if path.exists():
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        if digest == expected:
            ingest_archive(store, path, symbol, interval)
            return True
    response = get(url, stream=True)
    if response is None:
        return False
    partial = path.with_suffix(".zip.part")
    try:
        sha, size = hashlib.sha256(), 0
        with partial.open("wb") as f:
            for chunk in response.iter_content(256 * 1024):
                size += len(chunk)
                if size > 128 * 1024 * 1024:
                    raise MarketError("行情归档超过 128 MB 下载上限。")
                f.write(chunk)
                sha.update(chunk)
        if sha.hexdigest() != expected:
            raise MarketError("归档校验和不匹配，未导入。")
        partial.replace(path)
        ingest_archive(store, path, symbol, interval)
        return True
    finally:
        response.close()
        partial.unlink(missing_ok=True)


def download(store, symbol, interval, start, end, source="ccxt", progress=None):
    if not valid_symbol(symbol):
        raise MarketError("不支持的交易对。")
    if interval not in INTERVALS:
        raise MarketError("不支持的周期。")
    start, end = bounds(start, end, interval)
    step = INTERVALS[interval]
    count = max(0, (end - start) // step + 1)
    if count > MAX_BARS:
        raise MarketError("单币种单次最多 50 万根，请选择更大周期或缩短日期。")
    if count == 0 or store.candle_count(symbol, interval, start, end) == count:
        return coverage(store, symbol, interval, start, end)
    imported = 0
    if source == "archive":
        dt = datetime.fromtimestamp(start / 1000, timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        while int(dt.timestamp() * 1000) <= end:
            next_month = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
            a, b = (
                max(start, int(dt.timestamp() * 1000)),
                min(end, int(next_month.timestamp() * 1000) - step),
            )
            if store.candle_count(symbol, interval, a, b) != (b - a) // step + 1:
                monthly = False
                if next_month <= datetime.now(timezone.utc):
                    monthly = fetch_archive(
                        store, symbol, interval, "monthly", dt.strftime("%Y-%m")
                    )
                if not monthly:
                    for day in range(a // 86400000 * 86400000, b + 1, 86400000):
                        da, db = max(a, day), min(b, day + 86400000 - step)
                        if store.candle_count(symbol, interval, da, db) == (db - da) // step + 1:
                            continue
                        date = datetime.fromtimestamp(day / 1000, timezone.utc).strftime("%Y-%m-%d")
                        fetch_archive(store, symbol, interval, "daily", date)
                        if progress:
                            progress(
                                min(1.0, (day + 86400000 - start) / max(step, end - start + step)),
                                f"{symbol} · 日归档 {date}",
                            )
                        time.sleep(0.1)
            if progress:
                progress(
                    min(1.0, (b + step - start) / max(step, end - start + step)),
                    f"{symbol} · 月归档 {dt:%Y-%m} 已处理",
                )
            dt = next_month
    else:
        exchange = None
        if source == "ccxt":
            import ccxt

            exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 20000})
        # Constant-size windows: never build a list of all years of candles or gaps.
        for a in range(start, end + 1, step * 499):
            b = min(end, a + step * 498)
            if store.candle_count(symbol, interval, a, b) == (b - a) // step + 1:
                continue
            params = {
                "symbol": symbol,
                "interval": interval,
                "startTime": a,
                "endTime": b + step - 1,
                "limit": 499,
            }
            if exchange:
                try:
                    # CCXT's raw public method preserves quote volume, trades and taker volume.
                    # Unified fetch_ohlcv drops these fields. No load_markets or private API needed.
                    payload = exchange.fapiPublicGetKlines(params)
                except Exception as exc:
                    raise MarketError(
                        f"CCXT 下载失败；已缓存数据保留。{type(exc).__name__}: {str(exc)[:160]}"
                    ) from exc
            else:
                response = get("https://fapi.binance.com/fapi/v1/klines", params)
                if response is None:
                    raise MarketError("合约行情接口不存在。")
                payload = response.json()
            if not isinstance(payload, list):
                raise MarketError("合约行情响应格式不正确。")
            candles = [
                candle(row, "Binance USD-M via CCXT" if exchange else "Binance USD-M REST")
                for row in payload
            ]
            candles = [c for c in candles if a <= c["time"] <= b and c["time"] % step == 0]
            store.put_candles(symbol, interval, candles)
            imported += len(candles)
            if progress:
                progress(
                    (b + step - start) / max(step, end - start + step),
                    f"{symbol} · 已写入 {imported} 根",
                )
            time.sleep(0.15)
    return coverage(store, symbol, interval, start, end)
