"""
Lighter Trades Fetcher - A minimal web app to fetch and display trades from Lighter exchange.
Backend API using FastAPI with the Lighter SDK for authentication.
"""

import asyncio
import time
import csv
import io
from datetime import datetime, timezone
from typing import Optional
import httpx
import lighter
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ===== CONFIGURATION =====
BASE_URL = "https://mainnet.zklighter.elliot.ai"
TRADES_LIMIT = 100
RATE_LIMIT_DELAY = 3.5  # seconds between calls (20 calls/min = 3 sec, we use 3.5 for safety)
RATE_LIMIT_RETRY_DELAY = 15  # seconds to wait if rate limited

# ===== APP INITIALIZATION =====
app = FastAPI(title="Lighter Trades Fetcher")

# Cache for market details (refreshed hourly)
market_cache = {"data": {}, "last_updated": 0}
MARKET_CACHE_TTL = 3600  # 1 hour in seconds


# ===== MODELS =====
class AccountCredentials(BaseModel):
    """Credentials for a single account."""
    account_index: int
    private_key: str
    api_key_index: int


class LookupAccountsRequest(BaseModel):
    """Request model for looking up accounts."""
    l1_address: str


class FetchTradesRequest(BaseModel):
    """Request model for fetching trades from multiple accounts."""
    accounts: list[AccountCredentials]


class TradeData(BaseModel):
    """Processed trade data for display."""
    trade_id: int
    market: str
    side: str
    datetime_utc: str
    trade_value_usd: float
    size: float
    price_usd: float
    fee_usd: float
    role: str
    trade_type: str
    pnl_usd: Optional[float] = None


# ===== HELPER FUNCTIONS =====

async def fetch_market_details() -> dict:
    """
    Fetch and cache market details (symbol to market_id mapping).
    Refreshes cache if older than 1 hour.
    """
    current_time = time.time()
    
    # Return cached data if still valid
    if market_cache["data"] and (current_time - market_cache["last_updated"]) < MARKET_CACHE_TTL:
        return market_cache["data"]
    
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/api/v1/orderBookDetails")
        if response.status_code == 200:
            data = response.json()
            # Build mapping: market_id -> symbol
            market_map = {}
            for book in data.get("order_book_details", []):
                market_map[book["market_id"]] = book["symbol"]
            
            market_cache["data"] = market_map
            market_cache["last_updated"] = current_time
            return market_map
    
    return market_cache["data"]  # Return stale cache on error


async def get_account_indexes(l1_address: str) -> list[int]:
    """
    Fetch account indexes for a given L1 address.
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/api/v1/accountsByL1Address",
            params={"l1_address": l1_address}
        )
        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Failed to fetch account info")
        
        data = response.json()
        if data.get("code") != 200:
            raise HTTPException(status_code=400, detail="Invalid address or API error")
        
        sub_accounts = data.get("sub_accounts", [])
        return [acc["index"] for acc in sub_accounts]


async def create_auth_token(private_key: str, account_index: int, api_key_index: int) -> str:
    """
    Generate authentication token using the Lighter SDK.
    Uses api_private_keys dict format: {api_key_index: private_key}
    """
    client = None
    try:
        # SDK expects api_private_keys as a dict mapping api_key_index -> private_key
        client = lighter.SignerClient(
            url=BASE_URL,
            account_index=account_index,
            api_private_keys={api_key_index: private_key},
        )
        
        err = client.check_client()
        if err is not None:
            raise HTTPException(status_code=400, detail=f"Client verification failed: {err}")
        
        # Create auth token (default 10 min expiry)
        auth_token, err = client.create_auth_token_with_expiry(
            api_key_index=api_key_index
        )
        
        if err is not None:
            raise HTTPException(status_code=400, detail=f"Auth token creation failed: {err}")
        
        return auth_token
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Authentication error: {str(e)}")
    finally:
        if client:
            await client.close()


def determine_side(trade: dict, account_index: int) -> str:
    """
    Determine the trade side (Long/Short, Open/Close) based on trade data.
    """
    is_maker_ask = trade.get("is_maker_ask", False)
    bid_account_id = trade.get("bid_account_id")
    ask_account_id = trade.get("ask_account_id")
    
    is_taker = bid_account_id == account_index if not is_maker_ask else ask_account_id == account_index
    is_buyer = bid_account_id == account_index
    
    # Check if position sign changed (indicates close or flip)
    position_changed = trade.get("taker_position_sign_changed", False)
    
    if is_buyer:
        if position_changed:
            return "Close Short → Long" if not is_taker else "Close Short"
        return "Open Long"
    else:
        if position_changed:
            return "Close Long → Short" if not is_taker else "Close Long"
        return "Open Short"


def calculate_fee_usd(trade: dict, account_index: int, price: float, size: float) -> float:
    """
    Calculate fee in USD. Fee values are in basis points (1 bp = 0.0001).
    """
    is_taker = (trade.get("bid_account_id") == account_index and not trade.get("is_maker_ask")) or \
               (trade.get("ask_account_id") == account_index and trade.get("is_maker_ask"))
    
    fee_bp = trade.get("taker_fee", 0) if is_taker else trade.get("maker_fee", 0)
    fee_rate = fee_bp / 1_000_000  # Convert from micro basis points
    return price * size * fee_rate


def process_trade(trade: dict, account_index: int, market_map: dict) -> TradeData:
    """
    Process a raw trade into display-ready format.
    """
    market_id = trade.get("market_id", 0)
    market_name = market_map.get(market_id, f"ID:{market_id}")
    
    size = float(trade.get("size", 0))
    price = float(trade.get("price", 0))
    trade_value = float(trade.get("usd_amount", 0))
    
    # Determine role
    is_taker = (trade.get("bid_account_id") == account_index and not trade.get("is_maker_ask")) or \
               (trade.get("ask_account_id") == account_index and trade.get("is_maker_ask"))
    role = "Taker" if is_taker else "Maker"
    
    # Calculate fee
    fee_usd = calculate_fee_usd(trade, account_index, price, size)
    
    # Timestamp to UTC datetime
    timestamp_ms = trade.get("timestamp", 0)
    dt_utc = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    datetime_str = dt_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # Determine side
    side = determine_side(trade, account_index)
    
    # Calculate PnL for closing trades (simplified)
    pnl = None
    if "Close" in side:
        # This is a simplified PnL calculation
        # For accurate PnL, we'd need entry price history
        entry_quote = float(trade.get("taker_entry_quote_before", 0) or 0)
        position_before = float(trade.get("taker_position_size_before", 0) or 0)
        if position_before > 0 and entry_quote > 0:
            entry_price = entry_quote / position_before
            if "Short" in side:
                pnl = (entry_price - price) * size  # Profit on short when price drops
            else:
                pnl = (price - entry_price) * size  # Profit on long when price rises
    
    return TradeData(
        trade_id=trade.get("trade_id", 0),
        market=market_name,
        side=side,
        datetime_utc=datetime_str,
        trade_value_usd=round(trade_value, 2),
        size=size,
        price_usd=round(price, 6),
        fee_usd=round(fee_usd, 6),
        role=role,
        trade_type=trade.get("type", "trade"),
        pnl_usd=round(pnl, 4) if pnl is not None else None
    )


async def fetch_trades_for_account(
    auth_token: str,
    account_index: int,
    market_map: dict
) -> list[TradeData]:
    """
    Fetch all trades for a specific account, handling pagination and rate limits.
    """
    all_trades = []
    cursor = None
    page = 0
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            page += 1
            # Build request params
            params = {
                "account_index": account_index,
                "sort_by": "timestamp",
                "limit": TRADES_LIMIT,
                "auth": auth_token
            }
            if cursor:
                params["cursor"] = cursor
            
            # Make request with rate limit handling
            try:
                print(f"[Page {page}] Fetching trades... (cursor: {cursor[:20] if cursor else 'None'}...)")
                response = await client.get(f"{BASE_URL}/api/v1/trades", params=params)
                
                if response.status_code == 429:  # Rate limited
                    print(f"[Page {page}] Rate limited, waiting {RATE_LIMIT_RETRY_DELAY}s...")
                    await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
                    continue
                
                if response.status_code != 200:
                    print(f"[Page {page}] HTTP error: {response.status_code} - {response.text[:200]}")
                    break
                
                data = response.json()
                if data.get("code") != 200:
                    print(f"[Page {page}] API error: code={data.get('code')}")
                    break
                
                trades = data.get("trades", [])
                print(f"[Page {page}] Got {len(trades)} trades")
                
                for trade in trades:
                    processed = process_trade(trade, account_index, market_map)
                    all_trades.append(processed)
                
                # Check for next page
                next_cursor = data.get("next_cursor")
                if not next_cursor:
                    print(f"[Page {page}] No more pages (no next_cursor)")
                    break
                if not trades:
                    print(f"[Page {page}] No more pages (empty trades)")
                    break
                
                cursor = next_cursor
                
                # Rate limit delay
                print(f"[Page {page}] Waiting {RATE_LIMIT_DELAY}s before next request...")
                await asyncio.sleep(RATE_LIMIT_DELAY)
                
            except Exception as e:
                print(f"[Page {page}] Exception: {e}")
                break
    
    print(f"Total trades fetched: {len(all_trades)}")
    return all_trades


# ===== API ENDPOINTS =====

@app.post("/api/lookup-accounts")
async def lookup_accounts(request: LookupAccountsRequest):
    """
    Lookup all account indexes for an L1 address.
    Returns list of account indexes that user can select from.
    """
    account_indexes = await get_account_indexes(request.l1_address)
    if not account_indexes:
        raise HTTPException(status_code=400, detail="No accounts found for this address")
    
    return {
        "success": True,
        "l1_address": request.l1_address,
        "account_indexes": account_indexes
    }


@app.post("/api/fetch-trades")
async def fetch_trades(request: FetchTradesRequest):
    """
    Fetch trades for multiple accounts.
    Each account has its own credentials (account_index, private_key, api_key_index).
    Returns trades grouped by account_index.
    """
    if not request.accounts:
        raise HTTPException(status_code=400, detail="No accounts provided")
    
    # Fetch market details for name mapping
    market_map = await fetch_market_details()
    
    # Results grouped by account
    results = {}
    
    for account in request.accounts:
        account_index = account.account_index
        print(f"\n=== Fetching trades for account {account_index} ===")
        
        try:
            # Generate auth token for this account
            auth_token = await create_auth_token(
                account.private_key,
                account_index,
                account.api_key_index
            )
            
            # Fetch trades
            trades = await fetch_trades_for_account(auth_token, account_index, market_map)
            
            # Sort by datetime (newest first)
            trades.sort(key=lambda x: x.datetime_utc, reverse=True)
            
            results[account_index] = {
                "success": True,
                "total_trades": len(trades),
                "trades": [t.model_dump() for t in trades]
            }
            
        except HTTPException as e:
            print(f"Error with account {account_index}: {e.detail}")
            results[account_index] = {
                "success": False,
                "error": e.detail,
                "total_trades": 0,
                "trades": []
            }
        except Exception as e:
            print(f"Error with account {account_index}: {str(e)}")
            results[account_index] = {
                "success": False,
                "error": str(e),
                "total_trades": 0,
                "trades": []
            }
    
    return {
        "success": True,
        "accounts": results
    }


@app.get("/api/markets")
async def get_markets():
    """
    Get cached market details (for debugging/info).
    """
    market_map = await fetch_market_details()
    return {"markets": market_map}


# ===== STATIC FILES =====
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def root():
    """Serve the main HTML page."""
    return FileResponse("static/index.html")


# ===== RUN =====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

