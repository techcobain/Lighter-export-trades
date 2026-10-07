# Lighter Data Exporter

A web app to fetch and export trading data from [Lighter Exchange](https://lighter.xyz) — trades, funding payments, deposits, transfers, withdrawals, public-pool activity, and staking activity.

## Features

- **Account statements** — Monthly, year-end, or custom UTC cutoffs with PDF, CSV and JSON downloads
- **7 Data Types** — Trades, Funding, Deposits, Transfers, Withdrawals, Public Pools, Staking
- **Core & RH** — Switch between Lighter Core and Lighter RH beside the theme selector
- **Multi-account support** — Fetch data from multiple sub-accounts simultaneously
- **Read-only tokens** — Uses secure read-only auth tokens (can't trade or withdraw)
- **Custom timeframes** — Export complete history or select specific date ranges
- **Quick timeframes** — 7D, 30D, 90D, 2026, and 2025 presets in UTC; the 2025 preset displays January 1 while requests start at January 17 mainnet genesis
- **Spot & Perp filtering** — Filter trades by market type (Perpetuals, Spot)
- **Transfer filtering** — Filter by type (Incoming, Outgoing, Internal, Pool Mint/Burn)
- **Customizable columns** — Choose which fields to display and export
- **CSV & JSON export** — Per-account downloads in both formats
- **Click to copy** — Copy transaction hashes and addresses with one click
- **Asset symbols** — Automatic mapping of asset IDs to symbols (cached hourly)
- **Complete trade history** — Bulk exports bypass the 3,000-trade history cap, with automatic date-range splitting and rate-limit retries
- **Complete funding history** — Bulk exports in 90-day ranges, with boundary deduplication and rate-limit retries
- **Automatic pagination** — Handles pagination for wallet history

## Quick Start

```bash
# Clone and setup
git clone https://github.com/techcobain/Lighter-export-trades.git
cd Lighter-export-trades
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Run
python main.py
```

Visit `http://localhost:8000`

## How to Use

1. Enter your L1 address and click "Lookup Accounts"
2. Get a read-only token from [app.lighter.xyz/read-only-tokens](https://app.lighter.xyz/read-only-tokens)
3. Select the accounts you want to export data from
4. Choose a timeframe and click a fetch button to retrieve data
5. Export as CSV or JSON

## Data Types

| Type | Endpoint | Description |
|------|----------|-------------|
| **Trades** | `/api/v1/export?type=trade` | Trade history with PnL and fees from Lighter |
| **Funding** | `/api/v1/export?type=funding` | Funding payments on positions |
| **Deposits** | `/api/v1/deposit/history` | L1 deposits (Ethereum) |
| **Transfers** | `/api/v1/transfer/history` | L2 transfers between accounts |
| **Withdrawals** | `/api/v1/withdraw/history` | Withdrawals to L1/L2 (Arbitrum) |
| **Public Pools** | `/api/v1/transfer/history` | Deposits, withdrawals, pool creation, forced exits, and L1 exits |
| **Staking** | `/api/v1/transfer/history` | Staking-pool deposits, withdrawals, creation, and L1 unstaking |

Core uses `https://mainnet.zklighter.elliot.ai`; RH uses `https://api.rh.lighter.xyz`. The same requests and processing apply to both. Switching clears account selections, tokens, cached asset symbols, and results so data from the two networks cannot mix. Use a read-only token for the selected network.

Pool and staking events have separate result tables, account tabs, CSV downloads, and raw JSON downloads. The tables preserve incoming/outgoing cash-flow directions and pool account indexes. The Transfers view also includes all pool and staking event types.

Trade exports use non-aggregated fills and automatically split history into ranges of at most 180 days. Ranges reaching Lighter's one-million-row export limit are split further, and overlapping boundaries are deduplicated by trade ID. The table, column selection, filters, CSV format, and JSON envelope remain the same. JSON trade records now contain the raw export CSV fields. Transaction hashes are blank because Lighter's export does not include them. Spot trades use Lighter's Buy/Sell labels.

Funding exports explicitly request long and short sides separately: the live API can omit payments for `side=all`. They use ranges of at most 90 days and split further if an export reaches the row limit. Payments at overlapping boundaries are deduplicated by market, side, and UTC timestamp because the export has no funding ID. The funding table and CSV columns remain the same; rates retain the export's percentage precision. JSON retains the `position_fundings` envelope and contains the raw funding export CSV fields (`Market`, `Side`, `Date`, `Position Size`, `Payment`, `Rate`).

### Historical account statements

After connecting and selecting accounts, choose Monthly, Year-end, or Custom date and time in the Account Statements controls. Monthly statements use 00:00 UTC on the first day of the following month; year-end uses January 1 of the next year. Future cutoffs are rejected. January–September 2026 produces nine statements, ending at October 1, 2026 00:00 UTC.

Statements reverse complete trades, funding, deposits, withdrawals and transfers from two consistent current account snapshots. Combined spot and margin quantities include isolated collateral, without double-counting locked balances. Native spot/perpetual trade records supplement rounded export fees and include integrator fees. This native history is capped by Lighter; missing records require review. Historical fast-withdrawal charges are checked independently against daily cumulative outflows on stablecoin-only days. Extra debits are disclosed, with timing known only to that UTC day; an intraday cutoff on an affected day requires review.

USD valuations use the last completed one-minute spot candle, and open perpetual PnL uses historical mark candles. USDC (Core) or USDG (RH) is valued at USD 1 by a disclosed convention. Missing/stale prices, unmatched opening balances, pool/staking holdings, fee credits, leases, yield multipliers or other unsupported activity produce review notes and suppress a complete account-equity total. Quantities retain export rounding uncertainty. Outstanding withdrawal claims and external wallets are excluded.

PDFs include one statement per cutoff plus reconciliation/evidence notes. CSV preserves raw reconstructed quantities and tolerances. JSON includes the report and original financial evidence, with a SHA-256 digest for reproducibility. These are exporter-generated reconstructions, not exchange-issued confirmations.

### Transaction Hash Types

- **Deposits** — Transaction Hash (L1): Processed on Ethereum, bridged via CCTP if from other chains
- **Transfers** — Transaction Hash (L2): Processed on Lighter's app-chain, verify at [Lighter Explorer](https://app.lighter.xyz/explorer)
- **Withdrawals** — Can be Ethereum or Arbitrum (IDs starting with "fast" are Arbitrum)

## Deploy to Railway

1. Push to GitHub
2. Connect repo to [Railway](https://railway.app)
3. Auto-deploys using `Procfile`

## Architecture

| Component | Description |
|-----------|-------------|
| Auth | Read-only tokens from Lighter (user-provided) |
| Data fetching | Client-side direct to Lighter API; signed trade and funding export files downloaded server-side |
| Export processing | Server-side conversion of trade and funding export CSV into the existing display fields |
| Account statements | Server-side Decimal reconstruction, historical valuation and PDF/CSV rendering; JSON assembled in the browser |
| Asset mapping | Client-side with hourly cache |

Lighter API requests are made directly from your browser, so API rate limits apply to your IP. Trade and funding export files are downloaded by the server because Lighter's storage does not allow browser CORS access. The read-only token stays in the browser. Bulk exports send a signed download URL to the server; account statements send financial snapshots and source activity for reconstruction. Statement processing does not persist those records, and statement responses use `Cache-Control: no-store`. Download URLs are restricted to Lighter export buckets on Amazon S3, with redirects disabled.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web interface |
| `/api/lookup-accounts` | POST | Get account indexes for L1 address |
| `/api/process-trades` | POST | Process raw trades (add market names, PnL) |
| `/api/process-trades-export` | POST | Download and process a signed trade export CSV |
| `/api/process-fundings-export` | POST | Download and process a signed funding export CSV |
| `/api/account-statements` | POST | Reconstruct and value historical account statements from evidence (no credentials) |
| `/api/account-statements/export` | POST | Render a statement report as PDF or CSV |
| `/api/markets` | GET | Cached market details |

## Rate Limits

| Data Type | Rate | Pages/Min |
|-----------|------|-----------|
| Trades | 3.5s between bulk exports; retries on HTTP 429/405 | Varies by export size |
| Funding | 3.5s between bulk exports; retries on HTTP 429/405 | Varies by export size |
| Deposits/Transfers/Withdrawals/Pools/Staking | 1s delay | ~60 |

Close the Lighter frontend while fetching to avoid rate limit conflicts.

## Security

### Implemented Protections

- **Read-only only** — Only accepts read-only tokens (can't trade or withdraw)
- **Security headers** — CSP, HSTS, X-Frame-Options, X-Content-Type-Options
- **Rate limiting** — Per-IP limits on server endpoints (DoS protection)
- **XSS prevention** — All user/API data escaped before DOM insertion
- **Sanitized errors** — No sensitive data leaked in error messages
- **No storage** — Tokens used in-memory only, never logged or stored

### Headers Added

```
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; ...
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Strict-Transport-Security: max-age=31536000
```

## Tech Stack

- **Backend**: FastAPI + Lighter SDK
- **Frontend**: Vanilla HTML/CSS/JS
- **HTTP**: httpx (async)

## Tests

```bash
pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
node --test tests/*.test.cjs
```

These tests use synthetic data and mocked downloads; no account credentials are required.

## License

MIT

---

Built by [Supertramp](https://t.me/heysupertramp)
