import asyncio
import copy
import csv
import io
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from pypdf import PdfReader

import main
from account_statements import (HistoricalPrices, StatementError, build_report,
                                reconstruct, report_csv, report_pdf)


def ms(date):
    return int(datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp() * 1000)


def trade(identity, market, side, date, size, price, pnl="-", fee="0.0000", value=None):
    from decimal import Decimal
    return {"Trade ID": str(identity), "Market": market, "Side": side, "Date": date,
            "Size": size, "Price": price, "Closed PnL": pnl, "Fee": fee,
            "Trade Value": value or str(Decimal(size) * Decimal(price)), "Type": "trade"}


def dataset():
    snapshot = {"index": 42, "l1_address": "0x" + "1" * 40, "collateral": "810.4", "account_trading_mode": 1,
                "assets": [{"asset_id": 3, "symbol": "USDC", "balance": "10", "locked_balance": "5", "margin_balance": "810.4", "multiplier": "1"},
                           {"asset_id": 1, "symbol": "ETH", "balance": "1.49", "locked_balance": "0", "margin_balance": "0", "multiplier": "1"}],
                "positions": [{"symbol": "ETH", "market_id": 0, "position": "0", "sign": 1, "avg_entry_price": "0", "allocated_margin": "0"}],
                "shares": [], "pending_unlocks": []}
    transfer = {"id": "t1", "timestamp": ms("2026-01-25"), "asset_id": 3, "amount": "50", "fee": "0",
                "type": "L2TransferInflow", "from_account_index": 7, "to_account_index": 42, "from_route": "perps", "to_route": "spot"}
    self_transfer = {**transfer, "id": "t2", "timestamp": ms("2026-03-20"), "amount": "100", "type": "L2SelfTransfer",
                     "from_account_index": 42, "from_route": "spot", "to_route": "perps"}
    return {"network": "core", "account_index": 42, "snapshot_at_ms": ms("2026-04-01"),
            "snapshot": snapshot, "snapshot_after": copy.deepcopy(snapshot),
            "cutoffs": [{"label": "January 2026", "timestamp_ms": ms("2026-02-01")}, {"label": "February 2026", "timestamp_ms": ms("2026-03-01")}],
            "asset_details": [{"asset_id": 3, "symbol": "USDC", "decimals": 6}, {"asset_id": 1, "symbol": "ETH", "decimals": 8}], "markets": [],
            "trades": [trade(1, "ETH/USDC", "Buy", "2026-01-10 10:00:00", "2.000000", "100.000000", fee="1.0000", value="200.000000"),
                       trade(2, "ETH", "Open Long", "2026-01-20 10:00:00", "1.000000", "100.000000", fee="1.0000"),
                       trade(3, "ETH", "Long > Short", "2026-02-01 00:00:00", "2.000000", "110.000000", "10.000000", "2.0000"),
                       trade(4, "ETH/USDC", "Sell", "2026-02-10 10:00:00", "0.500000", "120.000000", fee="0.5000"),
                       trade(5, "ETH", "Close Short", "2026-02-15 10:00:00", "1.000000", "105.000000", "5.000000", "1.0000")],
            "spot_trades": [],
            "fundings": [{"Market": "ETH", "Side": "short", "Date": "2026-02-02 00:00:00", "Payment": "-0.100000", "Rate": "0.1%", "Position Size": "1"}],
            "deposits": [{"id": "d1", "timestamp": ms("2026-01-05"), "asset_id": 3, "amount": "1000", "status": "completed"}],
            "transfers": [transfer, self_transfer],
            "withdrawals": [{"id": "w1", "timestamp": ms("2026-03-15"), "asset_id": 3, "amount": "100", "status": "claimable"}], "leases": []}


class Prices:
    async def asset(self, symbol, timestamp, stable):
        return {"price": "1" if symbol == stable else "100", "source": "Synthetic test price", "price_at_utc": "2026-02-01 00:00:00 UTC", "stale_seconds": 0}

    async def position(self, symbol, timestamp):
        return {"price": "120", "source": "Synthetic test mark", "price_at_utc": "2026-02-01 00:00:00 UTC", "stale_seconds": 0}


class AccountStatementTests(unittest.TestCase):
    def test_integrator_fee_precision_and_missing_native_history_are_disclosed(self):
        data = dataset()
        data["perp_trades"] = [{"trade_id": 2, "size": "1", "usd_amount": "100", "bid_account_id": 42,
                               "ask_account_id": 7, "is_maker_ask": True, "taker_fee": 10000,
                               "integrator_taker_fee": 100}]
        data["snapshot"]["assets"][0]["margin_balance"] = "810.39"
        data["snapshot_after"] = copy.deepcopy(data["snapshot"])
        report = reconstruct(data)
        self.assertTrue(all(r["passed"] for r in report["validation"]["asset_residuals"]))
        self.assertTrue(any("integrator" in w for w in report["warnings"]))

    def test_daily_withdrawal_charge_evidence_and_intraday_ambiguity(self):
        data = dataset()
        data["withdrawals"][0].update(type="fast", status="completed")
        data["snapshot"]["assets"][0]["margin_balance"] = "807.4"
        data["snapshot_after"] = copy.deepcopy(data["snapshot"])
        day = ms("2026-03-15")
        data["outflow_checks"] = [{"day_ms": day, "pnl": [
            {"timestamp": day // 1000, "outflow": 0, "spot_outflow": 0},
            {"timestamp": (day + 86400000) // 1000, "outflow": 103, "spot_outflow": 0}]}]
        report = reconstruct(data)
        self.assertEqual(report["validation"]["status"], "reconciled")
        self.assertEqual(report["outflow_adjustments"][0]["amount"], "3")
        data["cutoffs"] = [{"label": "Intraday", "timestamp_ms": ms("2026-03-15T12:00:00")}]
        self.assertIsNone(asyncio.run(build_report(data, Prices()))["statements"][0]["account_equity_usd"])
        data["outflow_checks"][0]["pnl"][1]["outflow"] = 99
        self.assertEqual(reconstruct(data)["validation"]["status"], "review_required")
        data["outflow_checks"] = []
        self.assertEqual(reconstruct(data)["validation"]["status"], "review_required")

    def test_reverse_ledger_spot_received_asset_fees_flips_funding_and_cutoff(self):
        result = reconstruct(dataset())
        self.assertEqual(result["validation"]["status"], "reconciled")
        jan, feb = result["statements"]
        balances = {a["symbol"]: a["quantity"] for a in jan["assets"]}
        self.assertEqual(float(balances["USDC"]), 849)
        self.assertEqual(float(balances["ETH"]), 1.99)
        self.assertEqual(jan["positions"], [{"market": "ETH", "quantity": "1.000000", "entry_price": "100.000000"}])
        self.assertEqual(float(next(a["quantity"] for a in feb["assets"] if a["symbol"] == "USDC")), 920.4)
        self.assertEqual(feb["positions"], [])

    def test_native_spot_fee_ticks_handle_received_asset_and_zero_omitted_fees(self):
        data = dataset()
        data["spot_trades"] = [{"trade_id": 1, "size": "2", "usd_amount": "200", "bid_account_id": 42,
                               "ask_account_id": 7, "is_maker_ask": True, "taker_fee": 5000}]
        self.assertEqual(reconstruct(data)["validation"]["status"], "reconciled")
        data["spot_trades"][0]["taker_fee"] = None
        self.assertEqual(reconstruct(data)["validation"]["status"], "review_required")

    def test_locked_balance_not_double_counted_isolated_collateral_included(self):
        data = dataset()
        data["snapshot"]["assets"][0]["margin_balance"] = "800.4"
        data["snapshot"]["positions"][0]["allocated_margin"] = "10"
        data["snapshot_after"] = copy.deepcopy(data["snapshot"])
        self.assertEqual(reconstruct(data)["validation"]["status"], "reconciled")

    def test_short_entry_basis_at_custom_cutoff(self):
        data = dataset()
        data["cutoffs"] = [{"label": "Custom", "timestamp_ms": ms("2026-02-07")}]
        position = reconstruct(data)["statements"][0]["positions"][0]
        self.assertEqual(float(position["quantity"]), -1)
        self.assertEqual(float(position["entry_price"]), 110)

    def test_changed_snapshot_missing_history_and_conflicting_duplicates(self):
        data = dataset()
        data["snapshot_after"]["assets"][0]["balance"] = "11"
        with self.assertRaisesRegex(StatementError, "changed"):
            reconstruct(data)
        data = dataset(); data["withdrawals"] = []
        self.assertEqual(reconstruct(data)["validation"]["status"], "review_required")
        data = dataset(); data["trades"].append({**data["trades"][0], "Size": "3"})
        with self.assertRaisesRegex(StatementError, "Conflicting"):
            reconstruct(data)

    def test_duplicate_boundary_records_do_not_change_balances(self):
        data = dataset()
        for field in ("trades", "fundings", "deposits", "transfers", "withdrawals"):
            data[field] += copy.deepcopy(data[field])
        actual, expected = reconstruct(data), reconstruct(dataset())
        self.assertEqual(actual["statements"], expected["statements"])
        self.assertEqual(actual["validation"], expected["validation"])
        self.assertEqual(actual["source_counts"], expected["source_counts"])

    def test_unknown_direction_and_nonfinite_amounts_fail(self):
        for change in (lambda d: d["transfers"][0].update(type="Mystery"),
                       lambda d: d["trades"][0].update(Side="Mystery"),
                       lambda d: d["snapshot"]["assets"][0].update(balance="NaN")):
            data = dataset(); change(data); data["snapshot_after"] = copy.deepcopy(data["snapshot"])
            with self.assertRaises(StatementError): reconstruct(data)

    def test_unsupported_pool_holdings_never_produce_complete_equity(self):
        data = dataset(); data["snapshot"]["shares"] = [{"public_pool_index": 99, "shares_amount": 1}]
        data["snapshot_after"] = copy.deepcopy(data["snapshot"])
        report = asyncio.run(build_report(data, Prices()))
        self.assertTrue(report["warnings"])
        self.assertIsNone(report["statements"][0]["account_equity_usd"])

    def test_missing_price_and_stale_prices_require_review(self):
        class Missing(Prices):
            async def asset(self, symbol, timestamp, stable):
                return await super().asset(symbol, timestamp, stable) if symbol == stable else None
        report = asyncio.run(build_report(dataset(), Missing()))
        self.assertIsNone(report["statements"][0]["account_equity_usd"])
        class Stale(Prices):
            async def asset(self, symbol, timestamp, stable):
                return {**await super().asset(symbol, timestamp, stable), "stale_seconds": 60}
        self.assertEqual(asyncio.run(build_report(dataset(), Stale()))["statements"][0]["status"], "review_required")

    def test_pdf_csv_json_output_and_endpoint_security(self):
        report = asyncio.run(build_report(dataset(), Prices()))
        self.assertEqual(float(report["statements"][0]["account_equity_usd"]), 1068)
        pdf = report_pdf(report)
        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 3)
        content = "\n".join(p.extract_text() for p in reader.pages)
        self.assertIn("January 2026", content)
        self.assertIn("not issued or independently confirmed", content)
        rows = list(csv.reader(io.StringIO(report_csv(report).lstrip("\ufeff"))))
        self.assertEqual(len(rows[0]), 16)
        self.assertTrue(any(row[5] == "perp" for row in rows))
        with TestClient(main.app) as client, patch("main.build_report", new_callable=AsyncMock, return_value=report):
            self.assertEqual(client.post("/api/account-statements", json=dataset()).json()["report"], report)
            self.assertEqual(client.post("/api/account-statements", json={**dataset(), "authToken": "not-allowed"}).status_code, 422)
            for fmt in ("pdf", "csv"):
                response = client.post("/api/account-statements/export", json={"report": report, "format": fmt})
                self.assertEqual(response.status_code, 200)
                self.assertIn(fmt, response.headers["content-disposition"])
                self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(client.post("/api/account-statements/export", json={"report": {}, "format": "pdf"}).status_code, 400)

    def test_csv_formula_escape_and_negative_numbers(self):
        report = asyncio.run(build_report(dataset(), Prices()))
        report["statements"][0]["label"] = '=HYPERLINK("bad")'
        rows = list(csv.reader(io.StringIO(report_csv(report).lstrip("\ufeff"))))
        self.assertTrue(rows[1][2].startswith("'="))

    def test_historical_candles_ignore_cutoff_minute_and_future_prices(self):
        cutoff = ms("2026-02-01")
        client = AsyncMock()
        client.get.return_value = httpx.Response(200, json={"code": 200, "c": [
            {"t": cutoff - 60000, "c": 100}, {"t": cutoff, "c": 999}, {"t": cutoff + 60000, "c": 2000}]})
        provider = HistoricalPrices(client, "rh", [{"market_id": 2048, "market_type": "spot", "symbol": "ETH/USDG"}])
        price = asyncio.run(provider.asset("ETH", cutoff, "USDG"))
        self.assertEqual(price["price"], "100")
        self.assertEqual(price["stale_seconds"], 0)
        self.assertIn("api.rh.lighter.xyz", client.get.await_args.args[0])
        self.assertEqual(client.get.await_args.kwargs["params"]["end_timestamp"], (cutoff - 1) // 1000)


if __name__ == "__main__":
    unittest.main()
