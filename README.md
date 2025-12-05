# Lighter Trades Fetcher

A minimal web app to fetch and export trading history and funding payments from [Lighter Exchange](https://lighter.xyz).

## Features

- **Multi-account support** — Fetch data from multiple sub-accounts simultaneously
- **Trades & Funding** — Export both trade history and funding payments
- **Customizable columns** — Choose which fields to display and export
- **CSV & JSON export** — Per-account downloads in both formats
- **Automatic pagination** — Handles rate limits and fetches all data
- **Extended auth** — Option for 60-minute tokens for large datasets
- **Secure by design** — Private keys only used server-side, never stored

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

## Deploy to Railway

1. Push to GitHub
2. Connect repo to [Railway](https://railway.app)
3. Auto-deploys using `Procfile`

## Architecture

| Component | Description |
|-----------|-------------|
| Auth tokens | Generated server-side via Lighter SDK |
| Data fetching | Client-side (rate limits per user IP) |
| Processing | Server-side (market names, PnL calculation) |

This hybrid approach distributes rate limits across users instead of centralizing them on the server.

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web interface |
| `/api/lookup-accounts` | POST | Get account indexes for L1 address |
| `/api/generate-auth` | POST | Generate auth tokens (10 or 60 min) |
| `/api/process-trades` | POST | Process raw trades (add market names, PnL) |
| `/api/markets` | GET | Cached market details |

## Security

### Implemented Protections

- **Security headers** — CSP, HSTS, X-Frame-Options, X-Content-Type-Options
- **Rate limiting** — Per-IP limits on all endpoints (DoS protection)
- **XSS prevention** — All user/API data escaped before DOM insertion
- **Sanitized errors** — No sensitive data leaked in error messages
- **No storage** — Keys used only in-memory for token generation

### Headers Added

```
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; ...
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Strict-Transport-Security: max-age=31536000
```

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `BASE_URL` | mainnet | Lighter API endpoint |
| `TRADES_LIMIT` | 100 | Trades per API call |
| `RATE_LIMIT_DELAY` | 3.5s | Delay between Lighter API calls |

## Tech Stack

- **Backend**: FastAPI + Lighter SDK
- **Frontend**: Vanilla HTML/CSS/JS
- **HTTP**: httpx (async)

## License

MIT

---

Built by [Supertramp](https://t.me/heysupertramp)
