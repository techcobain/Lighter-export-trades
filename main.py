"""
Lighter Trades Fetcher - Fetch and export trading history from Lighter exchange.
"""

import re
import time
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict
import httpx
from eth_utils.address import to_checksum_address
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel

# Configuration
BASE_URL = "https://mainnet.zklighter.elliot.ai"
TRADES_LIMIT = 100
RATE_LIMIT_DELAY = 3.5
RATE_LIMIT_RETRY_DELAY = 15

ENDPOINT_RATE_LIMITS = {
    "/api/lookup-accounts": {"requests": 20, "window": 60},
    "/api/process-trades": {"requests": 30, "window": 60},
}


# Security Middleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "connect-src 'self' https://mainnet.zklighter.elliot.ai; "
            "img-src 'self' data:; "
            "frame-ancestors 'none';"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.requests = defaultdict(lambda: defaultdict(list))
    
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path not in ENDPOINT_RATE_LIMITS:
            return await call_next(request)
        
        client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not client_ip:
            client_ip = request.client.host if request.client else "unknown"
        
        limit_config = ENDPOINT_RATE_LIMITS[path]
        current_time = time.time()
        
        self.requests[client_ip][path] = [
            t for t in self.requests[client_ip][path]
            if current_time - t < limit_config["window"]
        ]
        
        if len(self.requests[client_ip][path]) >= limit_config["requests"]:
            return Response(
                content='{"detail": "Rate limit exceeded"}',
                status_code=429,
                media_type="application/json"
            )
        
        self.requests[client_ip][path].append(current_time)
        return await call_next(request)


# App Setup
app = FastAPI(title="Lighter Trades Fetcher")
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)

market_cache = {"data": {}, "last_updated": 0}
MARKET_CACHE_TTL = 3600


# Models
class LookupAccountsRequest(BaseModel):
    l1_address: str


class ProcessTradesRequest(BaseModel):
    account_index: int
    trades: list[dict]


class TradeData(BaseModel):
    trade_id: int
    tx_hash: str
    market: str
    market_type: str  # "Perp" or "Spot"
    side: str
    datetime_utc: str
    trade_value_usd: float
    size: float
    price_usd: float
    fee_usd: float
    role: str
    trade_type: str
    pnl_usd: Optional[float] = None


# Helper Functions
def normalize_eth_address(address: str) -> str:
    """Validate and convert an Ethereum address to checksum format."""
    address = address.strip()
    if not address:
        raise ValueError("Address cannot be empty")
    if not re.match(r'^0x[0-9a-fA-F]{40}$', address):
        raise ValueError("Invalid Ethereum address format")

    try:
        return to_checksum_address(address)
    except Exception:
        raise ValueError("Invalid Ethereum address")


async def fetch_market_details() -> dict:
    """Fetch and cache market details (market_id -> symbol mapping)."""
    current_time = time.time()
    if market_cache["data"] and (current_time - market_cache["last_updated"]) < MARKET_CACHE_TTL:
        return market_cache["data"]
    
    market_map = {}
    async with httpx.AsyncClient() as client:
        # Fetch order book details (contains both perp and spot markets in separate arrays)
        response = await client.get(f"{BASE_URL}/api/v1/orderBookDetails")
        if response.status_code == 200:
            data = response.json()
            # Perp markets are in "order_book_details"
            for book in data.get("order_book_details", []):
                market_id = int(book["market_id"])
                if "symbol" in book:
                    market_map[market_id] = book["symbol"]
            # Spot markets are in "spot_order_book_details"
            for book in data.get("spot_order_book_details", []):
                market_id = int(book["market_id"])
                if "symbol" in book:
                    market_map[market_id] = book["symbol"]
    
    if market_map:
        market_cache["data"] = market_map
        market_cache["last_updated"] = current_time
    return market_cache["data"] if market_cache["data"] else market_map


async def get_account_indexes(l1_address: str) -> list[int]:
    """Fetch account indexes for a given L1 address."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/api/v1/accountsByL1Address", params={"l1_address": l1_address})
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch account info")
        data = response.json()
        if data.get("code") != 200:
            raise HTTPException(status_code=400, detail="Invalid address or API error")
        return [acc["index"] for acc in data.get("sub_accounts", [])]


def is_user_taker(trade: dict, account_index: int) -> bool:
    """Determine if user is taker in this trade."""
    if trade.get("is_maker_ask", False):
        return trade.get("bid_account_id") == account_index
    return trade.get("ask_account_id") == account_index


def determine_side(trade: dict, account_index: int) -> str:
    """Determine trade side based on position state."""
    is_taker = is_user_taker(trade, account_index)
    is_buyer = trade.get("bid_account_id") == account_index
    trade_size = float(trade.get("size", 0))
    
    if is_taker:
        position_before = float(trade.get("taker_position_size_before", 0) or 0)
        position_sign_changed = trade.get("taker_position_sign_changed", False)
    else:
        position_before = float(trade.get("maker_position_size_before", 0) or 0)
        position_sign_changed = trade.get("maker_position_sign_changed", False)
    
    was_long = position_before > 0
    was_short = position_before < 0
    had_position = abs(position_before) > 0
    
    # Full close or flip
    if position_sign_changed and had_position:
        is_flip = trade_size > abs(position_before)
        if is_buyer:
            return "Short > Long" if is_flip else "Close Short"
        return "Long > Short" if is_flip else "Close Long"
    
    # Had position: reducing or adding
    if had_position:
        is_reducing = (was_long and not is_buyer) or (was_short and is_buyer)
        if is_reducing:
            return "Reduce Long" if was_long else "Reduce Short"
        return "Increase Long" if is_buyer else "Increase Short"
    
    # Opening new position
    return "Open Long" if is_buyer else "Open Short"


def calculate_fee_usd(trade: dict, account_index: int, price: float, size: float) -> float:
    """Calculate fee in USD."""
    is_taker = is_user_taker(trade, account_index)
    fee_bp = trade.get("taker_fee", 0) if is_taker else trade.get("maker_fee", 0)
    return price * size * (fee_bp / 1_000_000)


def process_trade(trade: dict, account_index: int, market_map: dict) -> TradeData:
    """Process raw trade into display format."""
    market_id = int(trade.get("market_id", 0))
    size = float(trade.get("size", 0))
    price = float(trade.get("price", 0))
    
    # Determine market type: Spot (market_id >= 2048) or Perp
    is_spot = market_id >= 2048
    market_type = "Spot" if is_spot else "Perp"
    
    # Get market symbol and normalize for spot markets (ETH/USDC -> ETH)
    raw_symbol = market_map.get(market_id, f"ID:{market_id}")
    if is_spot and "/" in raw_symbol:
        market_symbol = raw_symbol.split("/")[0]
    else:
        market_symbol = raw_symbol
    
    is_taker = is_user_taker(trade, account_index)
    is_buyer = trade.get("bid_account_id") == account_index
    
    if is_taker:
        entry_quote = float(trade.get("taker_entry_quote_before", 0) or 0)
        position_before = float(trade.get("taker_position_size_before", 0) or 0)
    else:
        entry_quote = float(trade.get("maker_entry_quote_before", 0) or 0)
        position_before = float(trade.get("maker_position_size_before", 0) or 0)
    
    was_long = position_before > 0
    was_short = position_before < 0
    is_reducing = (was_long and not is_buyer) or (was_short and is_buyer)
    
    # Calculate PnL for reducing trades
    pnl = None
    if is_reducing and abs(position_before) > 0 and abs(entry_quote) > 0:
        entry_price = abs(entry_quote) / abs(position_before)
        closed_size = min(size, abs(position_before))
        if was_long:
            pnl = (price - entry_price) * closed_size
        else:
            pnl = (entry_price - price) * closed_size
    
    timestamp_ms = trade.get("timestamp", 0)
    dt_utc = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    
    return TradeData(
        trade_id=trade.get("trade_id", 0),
        tx_hash=trade.get("tx_hash", ""),
        market=market_symbol,
        market_type=market_type,
        side=determine_side(trade, account_index),
        datetime_utc=dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        trade_value_usd=round(float(trade.get("usd_amount", 0)), 2),
        size=size,
        price_usd=round(price, 6),
        fee_usd=round(calculate_fee_usd(trade, account_index, price, size), 6),
        role="Taker" if is_taker else "Maker",
        trade_type=trade.get("type", "trade"),
        pnl_usd=round(pnl, 4) if pnl is not None else None
    )


# API Endpoints
@app.post("/api/lookup-accounts")
async def lookup_accounts(request: LookupAccountsRequest):
    """Lookup account indexes for an L1 address."""
    try:
        checksummed_address = normalize_eth_address(request.l1_address)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    account_indexes = await get_account_indexes(checksummed_address)
    if not account_indexes:
        raise HTTPException(status_code=400, detail="No accounts found for this address")
    return {"success": True, "l1_address": checksummed_address, "account_indexes": account_indexes}


@app.post("/api/process-trades")
async def process_trades(request: ProcessTradesRequest):
    """Process raw trades (add market names, PnL, etc)."""
    market_map = await fetch_market_details()
    processed = []
    
    for trade in request.trades:
        try:
            processed.append(process_trade(trade, request.account_index, market_map).model_dump())
        except:
            continue
    
    processed.sort(key=lambda x: x["datetime_utc"], reverse=True)
    return {"success": True, "total_trades": len(processed), "trades": processed}


@app.get("/api/markets")
async def get_markets():
    """Get cached market details."""
    return {"markets": await fetch_market_details()}


# Static Files
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
