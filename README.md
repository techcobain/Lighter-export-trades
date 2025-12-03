# Lighter Trades Fetcher

A minimal web application to fetch and export your trading history from [Lighter Exchange](https://lighter.xyz).

## Features

- Fetch all trades for your Lighter account(s)
- Display trades with market, side, price, size, fees, and PnL
- Export trades to CSV for further analysis
- Handles pagination and rate limits automatically
- Secure: Private keys are only used server-side for auth token generation

## Quick Start

### Local Development

```bash
# Clone the repository
git clone <your-repo-url>
cd RetrieveTrades

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the server
python main.py
```

Visit `http://localhost:8000` in your browser.

### Deploy to Railway

1. Push this repository to GitHub
2. Connect your GitHub repo to [Railway](https://railway.app)
3. Railway will automatically detect the Python app and deploy

Railway will use the `Procfile` for deployment configuration.

## Configuration

The app is configured for Lighter Mainnet by default. Key settings in `main.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `BASE_URL` | mainnet | Lighter API endpoint |
| `TRADES_LIMIT` | 100 | Max trades per API call |
| `RATE_LIMIT_DELAY` | 3.5s | Delay between calls (20/min limit) |

## How It Works

1. **Enter credentials**: Provide your API private key, L1 address, and API key index
2. **Account lookup**: App fetches your account index(es) from your L1 address
3. **Auth generation**: Creates a 10-minute auth token using the Lighter SDK
4. **Fetch trades**: Retrieves all trades with automatic pagination
5. **Display & Export**: View trades in-browser or download as CSV

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves the web interface |
| `/api/fetch-trades` | POST | Fetch trades for an account |
| `/api/export-csv` | POST | Export trades as CSV file |
| `/api/markets` | GET | Get cached market details |

## Trade Data Fields

| Field | Description |
|-------|-------------|
| `market` | Trading pair (e.g., BTC, ETH) |
| `side` | Open Long, Open Short, Close Long, Close Short |
| `datetime_utc` | Trade timestamp in UTC |
| `trade_value_usd` | Trade notional value in USD |
| `size` | Position size in base currency |
| `price_usd` | Execution price |
| `fee_usd` | Trading fee in USD |
| `role` | Maker or Taker |
| `trade_type` | trade, liquidation, or deleverage |
| `pnl_usd` | Realized PnL for closing trades |

## Security

- **Private keys are never stored** - They're only used in-memory to generate auth tokens
- **No logging of sensitive data** - Keys are not logged or persisted
- **Open source** - All code is public for transparency
- **Server-side only** - Auth happens on the server, not in the browser

## Tech Stack

- **Backend**: FastAPI (Python)
- **Auth**: Lighter SDK (`lighter` package)
- **HTTP Client**: httpx
- **Frontend**: Vanilla HTML/CSS/JS

## License

MIT

