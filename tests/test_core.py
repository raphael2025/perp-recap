import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from perp_recap.importer import (
    detect_utc_offset,
    number,
    parse_rows,
    parse_time,
    read_csv,
    read_upload,
    suggest_mapping,
    valid_symbol,
)
from perp_recap.market import bounds, candle, coverage, download, ingest_archive
from perp_recap.positions import derive
from perp_recap.store import Store


def fill(side="BUY", qty="1", time=1736928000000, trade_id="1", ps="BOTH", pnl="0"):
    return {
        "identity": "id:BTCUSDT:" + trade_id,
        "time": time,
        "symbol": "BTCUSDT",
        "side": side,
        "quantity": qty,
        "price": "100",
        "fee": "0.01",
        "fee_asset": "USDT",
        "realized_pnl": pnl,
        "position_side": ps,
        "order_id": "order-1",
        "trade_id": trade_id,
        "order_type": "UNKNOWN",
        "liquidity": "false",
        "order_type_raw": "",
    }


def sequence(*items, initial="0", mode="one_way", key="BTCUSDT:BOTH"):
    return derive(
        list(items), {"mode": mode, "initial": {key: initial} if initial is not None else {}}
    )


class ImportTests(unittest.TestCase):
    def test_actual_chinese_schema_with_synthetic_values(self):
        text = (
            "用户ID,时间,代币名称/币种名称/币对,方向,价格,数量,金额,手续费,已实现利润,买家,做市商,交易 ID,订单编号\n"
            "synthetic,2025-01-15 00:00:00,币安人生USDT,BUY,1,2,2,0.001USDT,0,true,false,10,20\n"
        )
        h, rows = read_csv(text.encode("utf-8-sig"))
        records, errors = parse_rows(rows, suggest_mapping(h))
        self.assertFalse(errors)
        self.assertEqual(records[0]["fee_asset"], "USDT")
        self.assertEqual(records[0]["order_type"], "UNKNOWN")
        self.assertEqual(records[0]["position_side"], "UNKNOWN")
        self.assertNotIn("用户ID", records[0])

    def test_one_letter_and_unicode_symbols(self):
        for s in ["WUSDT", "牛来USDT", "龙虾USDT", "1000SHIBUSDT", "BTCUSDC"]:
            self.assertTrue(valid_symbol(s))
        for s in ["../../BTCUSDT", "BTCUSD_PERP", "BTCUSDT?x", "USDT"]:
            self.assertFalse(valid_symbol(s))

    def test_timezone_and_timestamp_units(self):
        expected = 1736899200000
        for text, offset in [
            ("2025-01-15 00:00:00", 0),
            ("2025-01-15 08:00:00", 8),
            ("2025-01-15T08:00:00+08:00", 0),
            ("1736899200", 0),
            ("1736899200000000", 0),
        ]:
            self.assertEqual(parse_time(text, offset), expected)
        self.assertEqual(detect_utc_offset("report(UTC0).zip")[0], 0)
        self.assertEqual(detect_utc_offset("report(UTC+3).zip")[0], 3)
        self.assertEqual(detect_utc_offset("report.zip", "Date(UTC+05:30)")[0], 5.5)

    def test_bad_numbers_rejected(self):
        for text in ["NaN", "inf", "-1", "0", "=1+1", "1e999"]:
            with self.assertRaises(ValueError):
                number(text, positive=True)

    def test_zip_parts(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for part in (1, 2):
                z.writestr(
                    f"part{part}.csv",
                    "Time,Symbol,Side,Price,Quantity,Trade ID\n"
                    f"2025-01-15 00:00:00,BTCUSDT,BUY,100,1,{part}\n",
                )
        h, rows = read_upload(buf.getvalue())
        records, errors = parse_rows(rows, suggest_mapping(h))
        self.assertEqual(len(records), 2)
        self.assertFalse(errors)

    def test_duplicate_headers_and_wrong_side(self):
        with self.assertRaises(ValueError):
            read_csv(b"Symbol,Symbol\na,b\n")
        h, rows = read_csv(b"Time,Symbol,Side,Price,Quantity\n2025-01-15,BTCUSDT,LONG,100,1\n")
        records, errors = parse_rows(rows, suggest_mapping(h))
        self.assertFalse(records)
        self.assertEqual(len(errors), 1)

    def test_identical_no_id_fills_preserved(self):
        h, rows = read_csv(
            b"Time,Symbol,Side,Price,Quantity\n2025-01-15,BTCUSDT,BUY,100,1\n2025-01-15,BTCUSDT,BUY,100,1\n"
        )
        records, _ = parse_rows(rows, suggest_mapping(h))
        self.assertNotEqual(records[0]["identity"], records[1]["identity"])


class PositionTests(unittest.TestCase):
    def test_profit_rule_is_explicit_and_conflicts_do_not_invent_positions(self):
        row = derive(
            [fill("SELL")],
            {"mode": "one_way", "basis": "pnl_rule", "initial": {"BTCUSDT:BOTH": "1"}},
        )[0]
        self.assertEqual(row["profit_rule"], "开仓/加仓")
        self.assertIn("仓位待核对", row["action"])
        self.assertIsNone(row["after"])

    def test_missing_pnl_is_not_treated_as_zero(self):
        row = sequence(fill(pnl=None))[0]
        self.assertEqual(row["profit_rule"], "缺少已实现利润")

    def test_long_scale_partial_flat(self):
        rows = sequence(
            fill(qty="2"),
            fill(qty="1", trade_id="2"),
            fill("SELL", "1", trade_id="3", pnl="1"),
            fill("SELL", "2", trade_id="4", pnl="2"),
        )
        self.assertEqual([r["action"] for r in rows], ["开多", "加多", "减多", "平多"])
        self.assertEqual(rows[-1]["after"], "0")

    def test_short_and_reverse(self):
        rows = sequence(fill("SELL", "2"), fill("BUY", "3", trade_id="2", pnl="1"))
        self.assertEqual(rows[0]["action"], "开空")
        self.assertEqual(
            rows[1]["effects"], [{"action": "平空", "qty": "2"}, {"action": "开多", "qty": "1"}]
        )

    def test_unknown_start_and_taker_not_market(self):
        rows = sequence(fill(), initial=None)
        self.assertFalse(rows[0]["effects"])
        self.assertEqual(rows[0]["label"], "类型未知买入")

    def test_hedge_streams_are_separate(self):
        rows = derive(
            [fill(ps="LONG"), fill("SELL", "2", ps="SHORT", trade_id="2")],
            {"mode": "hedge", "initial": {"BTCUSDT:LONG": "0", "BTCUSDT:SHORT": "0"}},
        )
        self.assertEqual([r["action"] for r in rows], ["开多", "开空"])
        self.assertEqual(rows[1]["after"], "-2")

    def test_hedge_over_close_invalidates(self):
        rows = sequence(fill("SELL", "2", ps="LONG"), mode="hedge", key="BTCUSDT:LONG", initial="1")
        self.assertIsNone(rows[0]["after"])

    def test_explicit_initial_short_buy_reduces(self):
        self.assertEqual(sequence(fill(), initial="-2")[0]["action"], "减空")

    def test_zero_pnl_close_preserves_position_math(self):
        self.assertEqual(sequence(fill("SELL"), initial="1")[0]["action"], "平多")

    def test_nonzero_pnl_open_conflict(self):
        row = sequence(fill(pnl="-1"))[0]
        self.assertFalse(row["effects"])
        self.assertIsNone(row["after"])
        self.assertEqual(row["pnl_evidence"], "包含平仓")

    def test_ambiguous_same_time_without_ids(self):
        rows = sequence(fill(trade_id=""), fill("SELL", trade_id=""))
        self.assertTrue(all(r["after"] is None for r in rows))

    def test_mode_conflict_not_silently_relabelled(self):
        rows = sequence(fill(ps="LONG"), key="BTCUSDT:LONG")
        self.assertIsNone(rows[0]["after"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_idempotent_and_numeric_sort(self):
        records = [fill(trade_id="10"), fill(trade_id="2"), fill(trade_id="3", time=1736920000000)]
        self.assertEqual(self.store.import_fills("a", records), (3, 0))
        self.assertEqual(self.store.import_fills("a", records), (0, 3))
        self.assertEqual([f["trade_id"] for f in self.store.fills("a")], ["3", "2", "10"])

    def test_conflict_rolls_back_entire_import(self):
        self.store.import_fills("a", [fill()])
        with self.assertRaises(ValueError):
            self.store.import_fills("a", [fill(trade_id="2"), fill(qty="99")])
        self.assertEqual(len(self.store.fills("a")), 1)

    def test_order_enrichment_account_scoped(self):
        self.store.import_fills("a", [fill()])
        self.store.import_fills("b", [fill()])
        self.store.import_orders(
            "a", [{"symbol": "BTCUSDT", "order_id": "order-1", "order_type": "LIMIT"}]
        )
        self.assertEqual(self.store.fills("a")[0]["order_type"], "LIMIT")
        self.assertEqual(self.store.fills("b")[0]["order_type"], "UNKNOWN")

    def test_new_data_invalidates_starting_context(self):
        self.store.import_fills("a", [fill()])
        self.store.save_context("a", {"mode": "one_way", "initial": {"BTCUSDT:BOTH": "0"}})
        self.store.import_fills("a", [fill(trade_id="2")])
        self.assertFalse(self.store.context("a")["initial"])

    def test_coverage_does_not_deserialize_market_history(self):
        with patch.object(self.store, "candles", side_effect=AssertionError("must count in SQL")):
            result = coverage(self.store, "BTCUSDT", "1m", 1736928000000, 1736928120000)
        self.assertEqual(result["missing"], 3)

    def test_paged_download_and_retry_skips_cache(self):
        start = 1736928000000

        class Response:
            def __init__(self, params):
                self.params = params

            def json(self):
                return [
                    [t, "100", "102", "99", "101", "2", t + 59999, "202", 2, "1", "101", "0"]
                    for t in range(self.params["startTime"], self.params["endTime"] + 1, 60000)
                ]

        with (
            patch(
                "perp_recap.market.get", side_effect=lambda url, params: Response(params)
            ) as request,
            patch("perp_recap.market.time.sleep"),
        ):
            result = download(self.store, "BTCUSDT", "1m", start, start + 60000 * 1000, "rest")
            self.assertEqual(request.call_count, 3)
            self.assertEqual(result["missing"], 0)
            download(self.store, "BTCUSDT", "1m", start, start + 60000 * 1000, "rest")
            self.assertEqual(request.call_count, 3)

    def test_archive_stream_and_bounded_batches(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            lines = [
                f"{1736928000000 + i * 60000},100,102,99,101,2,0,202,2,1,101,0" for i in range(2501)
            ]
            z.writestr("BTCUSDT-1m-2025-01.csv", "\n".join(lines))
        buf.seek(0)
        batches = []
        original = self.store.put_candles

        def put(symbol, interval, values):
            batches.append(len(values))
            original(symbol, interval, values)

        with patch.object(self.store, "put_candles", side_effect=put):
            count = ingest_archive(self.store, buf, "BTCUSDT", "1m")
        self.assertEqual(count, 2501)
        self.assertEqual(batches, [1000, 1000, 501])


class MarketTests(unittest.TestCase):
    def test_open_candle_not_cached(self):
        self.assertEqual(bounds(0, 120000, "1m", now=150000), (0, 60000))

    def test_candle_rejects_corrupt_prices(self):
        with self.assertRaises(Exception):
            candle([1736928000000, "100", "90", "99", "101", "1", 0, "1", 1, "1", "1"], "test")


if __name__ == "__main__":
    unittest.main()
