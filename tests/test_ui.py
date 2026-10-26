"""Offline end-to-end UI checks using synthetic trades/candles only."""

import json
from datetime import datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

from perp_recap.importer import parse_rows, read_csv, suggest_mapping
from perp_recap.market import candle
from perp_recap.store import Store


def test_workspaces_and_timezone_alignment(tmp_path, monkeypatch):
    path = tmp_path / "ui.sqlite3"
    monkeypatch.setenv("PERP_RECAP_DB", str(path))
    store = Store(path)
    headers, rows = read_csv(Path("examples/fills.csv").read_bytes())
    fills, errors = parse_rows(rows, suggest_mapping(headers))
    assert not errors
    store.import_fills("demo", fills)
    store.save_context(
        "demo",
        {
            "mode": "one_way",
            "basis": "pnl_rule",
            "initial": {"BTCUSDT:BOTH": "0", "ETHUSDT:BOTH": "0"},
        },
    )
    times = [f["time"] for f in fills if f["symbol"] == "BTCUSDT"]
    bars = [
        candle(
            [
                t,
                "97000",
                "98000",
                "96000",
                "97500",
                "2",
                t + 299999,
                "195000",
                2,
                "1",
                "97500",
                "0",
            ],
            "synthetic-test",
        )
        for t in range(min(times) - 86400000, max(times) + 86400000, 300000)
    ]
    store.put_candles("BTCUSDT", "5m", bars)
    store.close()
    at = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=15).run()
    assert not at.exception
    for name in ["仓位依据", "下载行情", "图表复盘"]:
        at.sidebar.radio[0].set_value(name).run()
        assert not at.exception
    before = json.loads(at.get("plotly_chart")[0].proto.spec)
    at.sidebar.number_input[0].set_value(8).run()
    assert not at.exception
    after = json.loads(at.get("plotly_chart")[0].proto.spec)
    for trace in [0, 2, 3]:  # Candle, buy markers, sell markers.
        a = datetime.fromisoformat(before["data"][trace]["x"][0])
        b = datetime.fromisoformat(after["data"][trace]["x"][0])
        assert (b - a).total_seconds() == 8 * 3600
