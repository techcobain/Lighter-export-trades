# Lighter Data Exporter

A web app to fetch and export trading data from [Lighter Exchange](https://lighter.xyz) — trades, funding payments, deposits, transfers, and withdrawals.

## Features

- **5 Data Types** — Trades, Funding, Deposits, Transfers, Withdrawals
- **Multi-account support** — Fetch data from multiple sub-accounts simultaneously
- **Read-only tokens** — Uses secure read-only auth tokens (can't trade or withdraw)
- **Custom timeframes** — Export complete history or select specific date ranges
- **Spot & Perp filtering** — Filter trades by market type (Perpetuals, Spot)
- **Transfer filtering** — Filter by type (Incoming, Outgoing, Internal, Pool Mint/Burn)
- **Customizable columns** — Choose which fields to display and export
- **CSV & JSON export** — Per-account downloads in both formats
- **Click to copy** — Copy transaction hashes and addresses with one click
- **Asset symbols** — Automatic mapping of asset IDs to symbols (cached hourly)
- **Automatic pagination** — Handles rate limits and fetches all data

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
4. Click any of the 5 fetch buttons to retrieve data
5. Export as CSV or JSON

## Data Types

| Type | Endpoint | Description |
|------|----------|-------------|
| **Trades** | `/api/v1/trades` | Trade history with PnL calculation |
| **Funding** | `/api/v1/positionFunding` | Funding payments on positions |
| **Deposits** | `/api/v1/deposit/history` | L1 deposits (Ethereum) |
| **Transfers** | `/api/v1/transfer/history` | L2 transfers between accounts |
| **Withdrawals** | `/api/v1/withdraw/history` | Withdrawals to L1/L2 (Arbitrum) |

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
| Data fetching | Client-side direct to Lighter API |
| Trade processing | Server-side (market names, PnL calculation) |
| Asset mapping | Client-side with hourly cache |

Data is fetched directly from your browser to Lighter's API, so rate limits apply to your IP (not the server).

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web interface |
| `/api/lookup-accounts` | POST | Get account indexes for L1 address |
| `/api/process-trades` | POST | Process raw trades (add market names, PnL) |
| `/api/markets` | GET | Cached market details |

## Rate Limits

| Data Type | Rate | Pages/Min |
|-----------|------|-----------|
| Trades | 3.5s delay | ~17 |
| Funding | 1s delay | ~60 |
| Deposits/Transfers/Withdrawals | 1s delay | ~60 |

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

## License

MIT

---

Built by [Supertramp](https://t.me/heysupertramp)
