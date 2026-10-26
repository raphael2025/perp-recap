from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path="data/recap.sqlite3"):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS fills (
              account TEXT, identity TEXT, payload TEXT,
              PRIMARY KEY(account, identity));
            CREATE TABLE IF NOT EXISTS orders (
              account TEXT, symbol TEXT, order_id TEXT, order_type TEXT,
              PRIMARY KEY(account, symbol, order_id));
            CREATE TABLE IF NOT EXISTS contexts (account TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE IF NOT EXISTS candles (
              symbol TEXT, interval TEXT, time INTEGER, payload TEXT,
              PRIMARY KEY(symbol, interval, time));
        """)

    def close(self):
        self.db.close()

    def import_fills(self, account, items):
        inserted = duplicates = 0
        with self.db:
            for fill in items:
                # Line number varies across overlapping exports and is not identity evidence.
                payload = json.dumps(
                    {k: v for k, v in fill.items() if k != "source_line"},
                    sort_keys=True,
                    ensure_ascii=False,
                )
                old = self.db.execute(
                    "SELECT payload FROM fills WHERE account=? AND identity=?",
                    (account, fill["identity"]),
                ).fetchone()
                if old:
                    if old[0] != payload:
                        raise ValueError(
                            f"成交 ID 冲突：{fill['identity']}。整批撤回，请核对文件。"
                        )
                    duplicates += 1
                else:
                    self.db.execute(
                        "INSERT INTO fills VALUES(?,?,?)", (account, fill["identity"], payload)
                    )
                    inserted += 1
            # Importing an earlier file changes the meaning of the saved starting position.
            if inserted:
                self.db.execute("DELETE FROM contexts WHERE account=?", (account,))
        return inserted, duplicates

    def import_orders(self, account, items):
        inserted = duplicates = 0
        with self.db:
            for item in items:
                key = (account, item["symbol"], item["order_id"])
                old = self.db.execute(
                    "SELECT order_type FROM orders WHERE account=? AND symbol=? AND order_id=?", key
                ).fetchone()
                if old:
                    if old[0] != item["order_type"]:
                        raise ValueError("同一订单 ID 的订单类型冲突；整批撤回。")
                    duplicates += 1
                else:
                    self.db.execute(
                        "INSERT INTO orders VALUES(?,?,?,?)", (*key, item["order_type"])
                    )
                    inserted += 1
        return inserted, duplicates

    def fills(self, account):
        orders = {
            (s, oid): typ
            for s, oid, typ in self.db.execute(
                "SELECT symbol,order_id,order_type FROM orders WHERE account=?", (account,)
            )
        }
        result = []
        for (payload,) in self.db.execute("SELECT payload FROM fills WHERE account=?", (account,)):
            f = json.loads(payload)
            supplement = orders.get((f["symbol"], f["order_id"]))
            f["type_source"] = "成交 CSV" if f["order_type"] != "UNKNOWN" else "缺失"
            if supplement:
                if f["order_type"] not in ("UNKNOWN", supplement):
                    f["order_type"], f["type_source"] = "UNKNOWN", "成交与订单 CSV 冲突"
                elif f["order_type"] == "UNKNOWN":
                    f["order_type"], f["type_source"] = supplement, "订单 CSV（交易对 + 订单 ID）"
            result.append(f)

        def key(f):
            tid = f["trade_id"]
            return (f["time"], f["symbol"], int(tid) if tid.isdigit() else 0, f["identity"])

        return sorted(result, key=key)

    def accounts(self):
        return [
            r[0] for r in self.db.execute("SELECT DISTINCT account FROM fills ORDER BY account")
        ]

    def context(self, account):
        row = self.db.execute("SELECT payload FROM contexts WHERE account=?", (account,)).fetchone()
        return json.loads(row[0]) if row else {"mode": "unknown", "initial": {}}

    def save_context(self, account, context):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO contexts VALUES(?,?)", (account, json.dumps(context))
            )

    def put_candles(self, symbol, interval, candles):
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO candles VALUES(?,?,?,?)",
                [(symbol, interval, c["time"], json.dumps(c)) for c in candles],
            )

    def candles(self, symbol, interval, start, end):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT payload FROM candles WHERE symbol=? AND interval=? AND time>=? AND time<=? ORDER BY time",
                (symbol, interval, start, end),
            )
        ]

    def candle_count(self, symbol, interval, start, end):
        return self.db.execute(
            "SELECT COUNT(*) FROM candles WHERE symbol=? AND interval=? AND time>=? AND time<=?",
            (symbol, interval, start, end),
        ).fetchone()[0]

    def candle_times(self, symbol, interval, start, end):
        return {
            r[0]
            for r in self.db.execute(
                "SELECT time FROM candles WHERE symbol=? AND interval=? AND time>=? AND time<=?",
                (symbol, interval, start, end),
            )
        }
