"""Reconstruct venue holdings from a stable snapshot and unrounded source records.

Amounts stay Decimal until serialized. Reports distinguish reconstruction checks
from independent confirmation, and never substitute today's prices for history.
"""
import asyncio
import csv
import hashlib
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from xml.sax.saxutils import escape

import httpx

GENESIS_MS = 1737072000000
ZERO = Decimal(0)
STABLE_ASSETS = {"core": "USDC", "rh": "USDG"}
BASES = {"core": "https://mainnet.zklighter.elliot.ai", "rh": "https://api.rh.lighter.xyz"}
BUY_SIDES = {"Buy", "Open Long", "Increase Long", "Reduce Short", "Close Short", "Short > Long"}
SELL_SIDES = {"Sell", "Open Short", "Increase Short", "Reduce Long", "Close Long", "Long > Short"}


class StatementError(ValueError):
    pass


def amount(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise StatementError("A source amount is invalid.") from None
    if not result.is_finite():
        raise StatementError("A source amount is not finite.")
    return result


def text_amount(value):
    return format(value, "f") if value else "0"


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def event_time(row, exported=False):
    if exported:
        try:
            return int(datetime.strptime(row["Date"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()) * 1000
        except (ValueError, KeyError):
            raise StatementError("An export date is invalid.") from None
    # Wallet histories use milliseconds. Do not infer units from old schema examples.
    result = int(row["timestamp"])
    if result < GENESIS_MS:
        raise StatementError("A wallet timestamp is outside supported history.")
    return result


def financial_fingerprint(snapshot):
    fields = ("index", "account_trading_mode", "collateral", "assets", "shares", "pending_unlocks", "bo_positions")
    data = {key: snapshot.get(key) for key in fields}
    data["positions"] = [{key: p.get(key) for key in
                          ("market_id", "symbol", "position", "sign", "avg_entry_price", "allocated_margin", "margin_mode")}
                         for p in snapshot.get("positions", [])]
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def unique_rows(rows, key):
    unique = {}
    for row in rows:
        identity = key(row)
        if identity in unique and unique[identity] != row:
            raise StatementError("Conflicting duplicate source records.")
        unique[identity] = row
    return list(unique.values())


def reconstruct(dataset):
    """Return unpriced cutoff holdings plus a genesis reconciliation diagnostic."""
    network = dataset["network"]
    if network not in BASES:
        raise StatementError("Unknown network.")
    stable = STABLE_ASSETS[network]
    snapshot = dataset["snapshot"]
    index = int(dataset["account_index"])
    if int(snapshot["index"]) != index:
        raise StatementError("The snapshot belongs to a different account.")
    if financial_fingerprint(snapshot) != financial_fingerprint(dataset["snapshot_after"]):
        raise StatementError("Account balances changed during collection. Stop account activity and retry.")
    anchor = int(dataset["snapshot_at_ms"])
    if anchor > int(datetime.now(timezone.utc).timestamp() * 1000) + 60000:
        raise StatementError("The snapshot time is in the future.")
    cutoffs = dataset["cutoffs"]
    if not 1 <= len(cutoffs) <= 36:
        raise StatementError("Choose between 1 and 36 statement dates.")
    times = [int(item["timestamp_ms"]) for item in cutoffs]
    if len(times) != len(set(times)) or any(t < GENESIS_MS or t > anchor or t % 1000 for t in times):
        raise StatementError("Statement cutoffs must be unique UTC seconds between genesis and the snapshot.")
    warnings = []
    balances = defaultdict(lambda: ZERO)
    positions = {}
    symbols = {int(a["asset_id"]): a["symbol"] for a in dataset["asset_details"]}
    decimals = {a["symbol"]: int(a["decimals"]) for a in dataset["asset_details"]}
    tolerance = defaultdict(lambda: ZERO)
    basis_tolerance = defaultdict(lambda: ZERO)
    for asset in snapshot["assets"]:
        symbol = asset["symbol"]
        # balance includes locked_balance. margin_balance is a separate holding.
        balances[symbol] += amount(asset["balance"]) + amount(asset["margin_balance"])
        if amount(asset.get("multiplier", "1")) != 1 and balances[symbol]:
            warnings.append(f"{symbol}: historical yield multipliers are unavailable; quantity needs review.")
        if int(asset["asset_id"]) == 42 and balances[symbol]:
            warnings.append("Fee credits need separate historical treatment.")
    for p in snapshot["positions"]:
        qty = amount(p["position"]) * int(p["sign"])
        symbol = p["symbol"]
        if symbol in positions:
            raise StatementError("Ambiguous position symbols in the snapshot.")
        positions[symbol] = [qty, qty * amount(p["avg_entry_price"])]
        basis_tolerance[symbol] += abs(qty) * Decimal(10) ** amount(p["avg_entry_price"]).as_tuple().exponent / 2
        # Isolated collateral is held in addition to the cross margin balance.
        balances[stable] += amount(p.get("allocated_margin", "0"))
    if snapshot.get("shares") or snapshot.get("pending_unlocks"):
        warnings.append("Pool/staking holdings or pending unlocks need historical share records; totals are incomplete.")
    if snapshot.get("bo_positions"):
        warnings.append("Binary-option holdings need separate historical records; totals are incomplete.")
    if dataset.get("leases"):
        warnings.append("Leasing/fee-credit activity needs historical fee treatment; totals require review.")

    events = []
    counts = {}
    source_fields = ("trades", "fundings", "deposits", "transfers", "withdrawals")
    source_rows = {}
    exact_spot = {str(r["trade_id"]): r for r in unique_rows(dataset.get("spot_trades", []), lambda r: str(r["trade_id"]))}
    exact_perp = {str(r["trade_id"]): r for r in unique_rows(dataset.get("perp_trades", []), lambda r: str(r["trade_id"]))}
    for field in source_fields:
        rows = dataset[field]
        if field == "trades":
            rows = unique_rows(rows, lambda r: str(r["Trade ID"]))
        elif field == "fundings":
            rows = unique_rows(rows, lambda r: (r["Market"], r["Side"], r["Date"]))
        elif field == "transfers":
            rows = unique_rows(rows, lambda r: (r["id"], r["type"], r["asset_id"], r["from_account_index"],
                                               r["to_account_index"], r["from_route"], r["to_route"]))
        else:
            rows = unique_rows(rows, lambda r: str(r["id"]))
        counts[field] = len(rows)
        source_rows[field] = rows
    counts.update(spot_precision_records=len(exact_spot), perp_precision_records=len(exact_perp))

    def native_role(native, size):
        if index not in (int(native["bid_account_id"]), int(native["ask_account_id"])) or amount(native["size"]) != size:
            raise StatementError("Trade precision record does not match its export.")
        is_taker = (int(native["bid_account_id"]) == index) if native["is_maker_ask"] else (int(native["ask_account_id"]) == index)
        return "taker" if is_taker else "maker"

    def add(row, changes, position_change=None, exported=False, kind=""):
        timestamp = event_time(row, exported)
        if timestamp > anchor:
            raise StatementError("Activity is newer than the snapshot. Collect the account again.")
        events.append((timestamp, changes, position_change, kind))

    for row in source_rows["trades"]:
        market, side = row["Market"], row["Side"]
        if side not in BUY_SIDES | SELL_SIDES:
            raise StatementError("Unknown trade side; cannot reconstruct quantities.")
        direction = Decimal(1 if side in BUY_SIDES else -1)
        size, price, fee = amount(row["Size"]), amount(row["Price"]), amount(row["Fee"])
        if size <= 0 or price <= 0:
            raise StatementError("Trade size and price must be positive.")
        changes = defaultdict(lambda: ZERO)
        position_change = None
        if "/" in market:
            base, quote = market.split("/")
            changes[base] += direction * size
            changes[quote] -= direction * amount(row["Trade Value"])
            native = exact_spot.get(str(row["Trade ID"]))
            if native:
                role = native_role(native, size)
                ticks = amount(native.get(f"{role}_fee") or 0) + amount(native.get(f"integrator_{role}_fee") or 0)
                rate = ticks / Decimal(1_000_000)
                if direction > 0:
                    changes[base] -= size * rate
                else:
                    changes[quote] -= amount(native["usd_amount"]) * rate
            else:
                # Spot fees are paid in the received asset. CSV Fee is quote-denominated.
                if direction > 0:
                    changes[base] -= fee / price
                    tolerance[base] += Decimal(10) ** fee.as_tuple().exponent / (2 * price)
                else:
                    changes[quote] -= fee
                    tolerance[quote] += Decimal(10) ** fee.as_tuple().exponent / 2
            # Export trade value is six decimals, fee is commonly four decimals.
            tolerance[quote] += Decimal(10) ** amount(row["Trade Value"]).as_tuple().exponent / 2
        else:
            quote = stable
            pnl = ZERO if row["Closed PnL"].strip() in ("", "-") else amount(row["Closed PnL"])
            native = exact_perp.get(str(row["Trade ID"]))
            if native:
                role = native_role(native, size)
                ticks = amount(native.get(f"{role}_fee") or 0) + amount(native.get(f"integrator_{role}_fee") or 0)
                fee = amount(native["usd_amount"]) * ticks / Decimal(1_000_000)
            elif dataset.get("perp_trades") is not None:
                warnings.append("Some perpetual fee records are unavailable (native history is capped); integrator charges may be missing.")
            changes[quote] += pnl - fee
            position_change = (market, direction * size, price * direction * size + pnl)
            if pnl:
                tolerance[quote] += Decimal(10) ** pnl.as_tuple().exponent / 2
                basis_tolerance[market] += Decimal(10) ** pnl.as_tuple().exponent / 2
            if not native:
                tolerance[quote] += Decimal(10) ** fee.as_tuple().exponent / 2
        if row.get("Type", "trade") != "trade":
            warnings.append("Liquidation/deleverage/settlement activity may include additional charges; needs review.")
        add(row, changes, position_change, exported=True, kind="trade")
    for row in source_rows["fundings"]:
        payment = amount(row["Payment"])
        tolerance[stable] += Decimal(10) ** payment.as_tuple().exponent / 2
        add(row, {stable: payment}, exported=True, kind="funding")
    for kind, sign in (("deposits", 1), ("withdrawals", -1)):
        for row in source_rows[kind]:
            status = row["status"].lower()
            if status in ("failed", "canceled", "cancelled", "rejected"):
                continue
            accepted = ("completed",) if kind == "deposits" else ("completed", "claimable")
            if status not in accepted:
                warnings.append(f"An unsettled {kind[:-1]} has ambiguous accounting timing; needs review.")
                continue
            if int(row["asset_id"]) not in symbols:
                raise StatementError("An asset is missing from the asset catalog.")
            add(row, {symbols[int(row["asset_id"])]: amount(row["amount"]) * sign}, kind=kind)
    for row in source_rows["transfers"]:
        symbol = symbols.get(int(row["asset_id"]))
        if not symbol:
            raise StatementError("A transfer asset is missing from the asset catalog.")
        value, fee, typ = amount(row["amount"]), amount(row["fee"]), row["type"]
        sender, recipient = int(row["from_account_index"]), int(row["to_account_index"])
        if index not in (sender, recipient):
            raise StatementError("Transfer history contains a different account.")
        if fee:
            warnings.append("Transfer fee denomination is not documented; fee reconciliation needs review.")
        if typ == "L2SelfTransfer":
            if sender != index or recipient != index:
                raise StatementError("Invalid self-transfer.")
            delta = ZERO
        elif typ.endswith("Inflow") and recipient == index:
            delta = value
        elif typ.endswith("Outflow") and sender == index:
            delta = -value
        else:
            raise StatementError("Unknown transfer direction; cannot reconstruct quantities.")
        if not typ.startswith("L2Transfer") and typ != "L2SelfTransfer":
            warnings.append("Pool/staking or other transfer activity needs additional holding records; totals are incomplete.")
        add(row, {symbol: delta}, kind="transfers")

    # Wallet history omits some historical fast-withdrawal charges. Independently
    # measure a closed UTC day's cumulative outflows, only when every flow that
    # day is denominated in the collateral stablecoin. No balancing plug is used.
    adjustments = []
    day_ms = 86400000
    checks = {int(c["day_ms"]): c for c in dataset.get("outflow_checks", [])}
    fast_days = {event_time(r) // day_ms * day_ms for r in source_rows["withdrawals"]
                 if r.get("type") == "fast" and r["status"].lower() == "completed"}
    for day in sorted(fast_days):
        flows = [r for r in source_rows["withdrawals"] if day <= event_time(r) < day + day_ms and r["status"].lower() in ("completed", "claimable")]
        transfers = [r for r in source_rows["transfers"] if day <= event_time(r) < day + day_ms and int(r["from_account_index"]) == index]
        check = checks.get(day)
        eligible = all(symbols[int(r["asset_id"])] == stable for r in flows + transfers)
        eligible = eligible and all(r["type"].startswith("L2Transfer") or r["type"] == "L2SelfTransfer" for r in transfers)
        if not eligible or not check or day + day_ms > anchor:
            warnings.append("Historical fast-withdrawal charges could not be checked against a closed stablecoin-only UTC day.")
            continue
        points = {int(p["timestamp"]) * 1000: p for p in check.get("pnl", [])}
        before, after = points.get(day), points.get(day + day_ms)
        if not before or not after:
            warnings.append("Daily outflow evidence is missing a boundary; historical withdrawal charges need review.")
            continue
        total = sum((amount(after.get(k, 0)) - amount(before.get(k, 0)) for k in ("outflow", "spot_outflow")), ZERO)
        listed = sum((amount(r["amount"]) for r in flows + transfers), ZERO)
        extra = total - listed
        if extra < -Decimal("0.000002"):
            warnings.append("Daily outflow evidence disagrees with wallet history; charges need review.")
            continue
        if extra > Decimal("0.000002"):
            events.append((day + day_ms - 1, {stable: -extra}, None, "daily_outflow_adjustment"))
            adjustments.append({"day_utc": utc(day), "asset": stable, "amount": text_amount(extra),
                                "listed_outflows": text_amount(listed), "observed_outflows": text_amount(total),
                                "source": "pnl daily cumulative outflow + spot_outflow; timing known only to UTC day"})
        tolerance[stable] += Decimal("0.000002")
    counts["daily_outflow_checks"] = len(checks)

    def reverse(event):
        _, changes, position_change, _ = event
        for symbol, delta in changes.items():
            balances[symbol] -= delta
        if position_change:
            symbol, qty, basis = position_change
            current = positions.setdefault(symbol, [ZERO, ZERO])
            current[0] -= qty
            current[1] -= basis

    results = []
    events.sort(key=lambda e: e[0], reverse=True)
    cursor = 0
    for cutoff in sorted(cutoffs, key=lambda x: int(x["timestamp_ms"]), reverse=True):
        timestamp = int(cutoff["timestamp_ms"])
        # Cutoff is the opening instant: activity stamped exactly at it belongs to the next period.
        while cursor < len(events) and events[cursor][0] >= timestamp:
            reverse(events[cursor])
            cursor += 1
        local_warnings = []
        if timestamp % day_ms and any(int(c["day_ms"]) <= timestamp < int(c["day_ms"]) + day_ms
                                     and utc(int(c["day_ms"])) in {a["day_utc"] for a in adjustments} for c in checks.values()):
            local_warnings.append("Withdrawal charge timing is known only to this UTC day; intraday quantity requires review.")
        assets = []
        for symbol, quantity in sorted(balances.items()):
            unit = Decimal(10) ** -decimals.get(symbol, 8)
            unrounded = quantity
            if quantity < 0 and abs(quantity) <= max(tolerance[symbol], unit) or abs(quantity) < unit / 2:
                quantity = ZERO
            if quantity < 0:
                local_warnings.append(f"{symbol}: negative reconstructed asset balance needs reconciliation.")
            assets.append({"symbol": symbol, "quantity": text_amount(quantity),
                           "reconstructed_quantity": text_amount(unrounded),
                           "rounding_tolerance": text_amount(tolerance[symbol] + unit)})
        active = []
        for symbol, (qty, basis) in sorted(positions.items()):
            if qty:
                entry = basis / qty
                if entry <= 0:
                    local_warnings.append(f"{symbol}: reconstructed entry basis is invalid.")
                active.append({"market": symbol, "quantity": text_amount(qty), "entry_price": text_amount(entry)})
        results.append({"label": str(cutoff["label"])[:120], "timestamp_ms": timestamp,
                        "as_of_utc": utc(timestamp), "assets": assets, "positions": active,
                        "warnings": local_warnings})
    while cursor < len(events):
        reverse(events[cursor])
        cursor += 1
    residuals = []
    for symbol, value in sorted(balances.items()):
        tol = tolerance[symbol] + Decimal(10) ** -decimals.get(symbol, 8)
        residuals.append({"symbol": symbol, "opening_residual": text_amount(value),
                          "tolerance": text_amount(tol), "passed": abs(value) <= tol})
    position_residuals = [{"market": symbol, "quantity": text_amount(values[0]), "entry_basis_residual": text_amount(values[1]),
                           "basis_tolerance": text_amount(basis_tolerance[symbol]),
                           "passed": values[0] == 0 and abs(values[1]) <= basis_tolerance[symbol]}
                          for symbol, values in sorted(positions.items())]
    reconciled = all(r["passed"] for r in residuals + position_residuals)
    if not reconciled:
        warnings.append("History does not reconcile to zero opening holdings at genesis. Treat statements as drafts.")
    warnings = sorted(set(warnings))
    source_hash = hashlib.sha256(json.dumps(dataset, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "schema_version": 1, "network": network, "account_index": index,
        "l1_address": snapshot["l1_address"], "snapshot_at_utc": utc(anchor),
        "generated_at_utc": utc(int(datetime.now(timezone.utc).timestamp() * 1000)),
        "scope": "Combined Lighter spot and margin holdings; external wallets and outstanding withdrawal claims excluded.",
        "cutoff_convention": "Opening instant in UTC. Activity timestamped at the cutoff is excluded.",
        "precision_note": "Quantities are reconstructed estimates within the stated tolerance. Native trade fee ticks include integrator charges; exports round funding and PnL. Small negative residuals within tolerance display as zero; JSON/CSV retain the raw quantity.",
        "validation": {"status": "reconciled" if reconciled and not warnings else "review_required",
                       "asset_residuals": residuals, "position_residuals": position_residuals,
                       "meaning": "A stable current snapshot and zero opening residual check; not independent exchange confirmation."},
        "warnings": warnings, "source_counts": counts, "source_sha256": source_hash,
        "outflow_adjustments": adjustments,
        "statements": sorted(results, key=lambda r: r["timestamp_ms"]),
    }


class HistoricalPrices:
    """Use the last fully completed 1-minute candle ending at/before the cutoff."""
    def __init__(self, client, network, markets):
        self.client = client
        self.base = BASES[network]
        self.markets = markets
        self.cache = {}

    async def candle(self, market, timestamp, mark=False):
        key = (market["market_id"], timestamp, mark)
        if key in self.cache:
            return self.cache[key]
        path = "markPriceCandles" if mark else "candles"
        for lookback in (7200, 86400):
            response = None
            for attempt in range(4):
                response = await self.client.get(f"{self.base}/api/v1/{path}", params={
                    "market_id": market["market_id"], "resolution": "1m", "count_back": 0,
                    "start_timestamp": (timestamp - lookback * 1000) // 1000,
                    "end_timestamp": (timestamp - 1) // 1000,
                })
                if response.status_code not in (429, 405) or attempt == 3:
                    break
                await asyncio.sleep(min(float(response.headers.get("Retry-After", "3")), 15))
            if response.status_code != 200:
                raise StatementError("Historical price request failed. Try again.")
            data = response.json()
            if data.get("code") != 200 or not isinstance(data.get("c"), list):
                raise StatementError("Historical price response is invalid.")
            available = []
            for row in data["c"]:
                t = int(row["t"])
                if t < 100000000000:
                    t *= 1000
                if t + 60000 <= timestamp:
                    available.append((t, row))
            if available:
                t, row = max(available, key=lambda item: item[0])
                price = amount(row["c"])
                if price <= 0:
                    continue
                result = {"price": text_amount(price), "source": f"{path}: market {market['market_id']}, 1m close",
                          "price_at_utc": utc(t + 60000), "stale_seconds": (timestamp - t - 60000) // 1000}
                self.cache[key] = result
                return result
        self.cache[key] = None
        return None

    async def asset(self, symbol, timestamp, stable):
        if symbol == stable:
            return {"price": "1", "source": "Stablecoin valued at USD 1 by convention", "price_at_utc": utc(timestamp), "stale_seconds": 0}
        candidates = [m for m in self.markets if m.get("market_type") == "spot" and m["symbol"] == f"{symbol}/{stable}"]
        if not candidates:
            return None
        return await self.candle(candidates[0], timestamp)

    async def position(self, symbol, timestamp):
        candidates = [m for m in self.markets if m.get("market_type") == "perp" and m["symbol"] == symbol]
        return await self.candle(candidates[0], timestamp, True) if candidates else None


async def build_report(dataset, prices=None):
    report = reconstruct(dataset)
    stable = STABLE_ASSETS[dataset["network"]]

    async def price_report(provider):
        for statement in report["statements"]:
            total, unrealized = ZERO, ZERO
            complete = not report["warnings"] and not statement["warnings"]
            for row in statement["assets"]:
                qty = amount(row["quantity"])
                pricing = await provider.asset(row["symbol"], statement["timestamp_ms"], stable) if qty else None
                row.update({"unit_price_usd": pricing["price"] if pricing else None,
                            "price_source": pricing["source"] if pricing else "No price needed for zero balance" if not qty else "Historical price unavailable",
                            "price_at_utc": pricing["price_at_utc"] if pricing else None,
                            "value_usd": text_amount(qty * amount(pricing["price"])) if pricing else "0" if not qty else None})
                if qty and not pricing:
                    complete = False
                    statement["warnings"].append(f"{row['symbol']}: historical USD price unavailable.")
                if pricing and pricing["stale_seconds"]:
                    statement["warnings"].append(f"{row['symbol']}: price predates the cutoff by {pricing['stale_seconds']} seconds.")
                    complete = False
                if row["value_usd"] is not None:
                    total += amount(row["value_usd"])
            for row in statement["positions"]:
                pricing = await provider.position(row["market"], statement["timestamp_ms"])
                qty = amount(row["quantity"])
                row.update({"mark_price_usd": pricing["price"] if pricing else None,
                            "price_source": pricing["source"] if pricing else "Historical mark unavailable",
                            "price_at_utc": pricing["price_at_utc"] if pricing else None,
                            "unrealized_pnl_usd": text_amount(qty * (amount(pricing["price"]) - amount(row["entry_price"]))) if pricing else None})
                if not pricing:
                    complete = False
                    statement["warnings"].append(f"{row['market']}: historical mark unavailable.")
                elif pricing["stale_seconds"]:
                    statement["warnings"].append(f"{row['market']}: mark predates the cutoff by {pricing['stale_seconds']} seconds.")
                    complete = False
                if row["unrealized_pnl_usd"] is not None:
                    unrealized += amount(row["unrealized_pnl_usd"])
            statement.update({"known_assets_usd": text_amount(total), "known_unrealized_pnl_usd": text_amount(unrealized),
                              "account_equity_usd": text_amount(total + unrealized) if complete else None,
                              "status": "reconciled" if complete and report["validation"]["status"] == "reconciled" else "review_required"})
    if prices is None:
        async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
            await price_report(HistoricalPrices(client, dataset["network"], dataset["markets"]))
    else:
        await price_report(prices)
    report["valuation_policy"] = f"{stable} = USD 1 convention (not an observed FX rate). Other assets: last completed 1m spot candle; perps: 1m mark candle. Stale/missing prices require review."
    return report


def report_csv(report):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Account", "Network", "Statement", "As of UTC", "Status", "Kind", "Asset/Market", "Quantity",
                     "Entry price", "Unit/Mark price USD", "Value/Unrealized PnL USD", "Price source", "Price at UTC", "Notes",
                     "Raw reconstructed quantity", "Quantity tolerance"])
    def cell(value):
        s = str(value) if value is not None else ""
        # Symbols/labels are untrusted strings; prevent formula execution in spreadsheet apps.
        return "'" + s if s.startswith(("=", "+", "@", "-")) and not _numeric(s) else s
    for statement in report["statements"]:
        prefix = [report["account_index"], report["network"], statement["label"], statement["as_of_utc"], statement["status"]]
        notes = " | ".join(report["warnings"] + statement["warnings"] + [report["valuation_policy"], report["precision_note"],
                           f"Dataset SHA-256: {report['source_sha256']}"])
        for row in statement["assets"]:
            writer.writerow([cell(v) for v in prefix + ["asset", row["symbol"], row["quantity"], "", row["unit_price_usd"], row["value_usd"], row["price_source"], row["price_at_utc"], notes, row["reconstructed_quantity"], row["rounding_tolerance"]]])
        for row in statement["positions"]:
            writer.writerow([cell(v) for v in prefix + ["perp", row["market"], row["quantity"], row["entry_price"], row["mark_price_usd"], row["unrealized_pnl_usd"], row["price_source"], row["price_at_utc"], notes, "", ""]])
        writer.writerow([cell(v) for v in prefix + ["account_equity", "USD", "", "", "", statement["account_equity_usd"], "", "", notes, "", ""]])
    for row in report.get("outflow_adjustments", []):
        writer.writerow([cell(v) for v in [report["account_index"], report["network"], "Daily charge evidence", row["day_utc"], "", "outflow_adjustment", row["asset"], row["amount"], "", "", "", row["source"], "", f"Listed {row['listed_outflows']}; observed {row['observed_outflows']}", "", ""]])
    return "\ufeff" + output.getvalue()


def _numeric(value):
    try:
        return amount(value).is_finite()
    except StatementError:
        return False


def report_pdf(report):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

    output = io.BytesIO()
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Fine", fontName="Helvetica", fontSize=8, leading=11, textColor=colors.HexColor("#526077")))
    styles.add(ParagraphStyle(name="Cell", fontName="Helvetica", fontSize=8, leading=11))
    def para(value, style="Cell"):
        return Paragraph(escape(str(value)), styles[style])
    def pretty(value, money=False):
        if value is None:
            return "Unavailable"
        d = amount(value)
        return f"{d:,.2f}" if money else format(d.normalize(), "f")
    def table(headers, rows, widths):
        data = [[para(v) for v in headers]] + [[para(v) for v in row] for row in rows]
        result = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        result.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0f7")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#b8c6d8")),
            ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#e0e6ed")),
            ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        return result
    story = []
    width = A4[0] - 84
    for n, statement in enumerate(report["statements"]):
        if n:
            story.append(PageBreak())
        story.extend([para("LIGHTER / ACCOUNT STATEMENT", "Fine"), Spacer(1, 12), para(statement["label"], "Title"),
                      para(f"As of {statement['as_of_utc']}", "Heading2"),
                      para(f"Account {report['account_index']} | {report['network'].upper()} | {statement['status'].replace('_', ' ').upper()}"),
                      Spacer(1, 6), para(f"L1 address: {report['l1_address']}", "Fine"), Spacer(1, 16)])
        story.append(table(["Asset", "Quantity", "Unit price (USD)", "Value (USD)"],
                           [[r["symbol"], pretty(r["quantity"]), pretty(r["unit_price_usd"]), pretty(r["value_usd"], True)]
                            for r in statement["assets"]], [width * f for f in (.18, .29, .24, .29)]))
        story.extend([Spacer(1, 6), para("Quantity tolerance: " + "; ".join(f"{r['symbol']} +/- {r['rounding_tolerance']}" for r in statement["assets"]), "Fine")])
        if statement["positions"]:
            story.extend([Spacer(1, 14), para("Open perpetual positions", "Heading2"),
                          table(["Market / signed size", "Entry price", "Mark price", "Unrealized PnL (USD)"],
                                [[f"{r['market']} / {pretty(r['quantity'])}", pretty(r["entry_price"]), pretty(r["mark_price_usd"]), pretty(r["unrealized_pnl_usd"], True)]
                                 for r in statement["positions"]], [width * f for f in (.29, .21, .21, .29)])])
        story.extend([Spacer(1, 14), para(f"Known asset value: USD {pretty(statement['known_assets_usd'], True)}", "Heading2"),
                      para(f"Account equity including valued perp PnL: USD {pretty(statement['account_equity_usd'], True)}"), Spacer(1, 12)])
        for note in report["warnings"] + statement["warnings"]:
            story.append(para("REVIEW: " + note, "Fine"))
        story.extend([Spacer(1, 10), para("Basis and evidence", "Heading3"), para(report["scope"], "Fine"),
                      para(report["cutoff_convention"], "Fine"), para(report["valuation_policy"], "Fine"),
                      para(report["precision_note"], "Fine"),
                      para(f"Snapshot captured: {report['snapshot_at_utc']}. Generated: {report['generated_at_utc']}.", "Fine"),
                      para("Source rows: " + ", ".join(f"{k} {v}" for k, v in report["source_counts"].items()), "Fine"),
                      para("Reconstructed by this exporter; not issued or independently confirmed by Lighter.", "Fine"),
                      Spacer(1, 8), para("Price evidence", "Heading3")])
        for row in statement["assets"] + statement["positions"]:
            symbol = row.get("symbol", row.get("market"))
            story.append(para(f"{symbol}: {row['price_source']}. {row.get('price_at_utc') or ''}", "Fine"))
    story.extend([PageBreak(), para("Reconciliation and source integrity", "Title"),
                  para(report["validation"]["meaning"], "Fine"), Spacer(1, 12),
                  table(["Asset", "Opening residual", "Tolerance", "Check"],
                        [[r["symbol"], r["opening_residual"], r["tolerance"], "Within tolerance" if r["passed"] else "Review required"]
                         for r in report["validation"]["asset_residuals"]], [width * f for f in (.18, .29, .29, .24)]),
                  Spacer(1, 12), para("Dataset SHA-256", "Heading3"), para(report["source_sha256"], "Fine")])
    for row in report["validation"]["position_residuals"]:
        story.append(para(f"{row['market']}: opening size {row['quantity']}; entry basis residual {row['entry_basis_residual']}; basis tolerance {row['basis_tolerance']}; {'passed' if row['passed'] else 'review required'}.", "Fine"))
    for row in report.get("outflow_adjustments", []):
        story.extend([Spacer(1, 8), para(f"Daily outflow adjustment: {row['day_utc']}", "Heading3"),
                      para(f"{row['asset']}: listed outflows {row['listed_outflows']}, observed {row['observed_outflows']}; additional debit {row['amount']}. {row['source']}.", "Fine")])
    def footer(canvas, doc):
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#526077"))
        canvas.drawString(42, 26, f"Lighter {report['network'].upper()} | Account {report['account_index']} | Reconstructed statement")
        canvas.drawRightString(A4[0] - 42, 26, str(doc.page))
    SimpleDocTemplate(output, pagesize=A4, rightMargin=42, leftMargin=42, topMargin=38, bottomMargin=44,
                      title="Lighter account statements", author="Lighter Data Exporter").build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
