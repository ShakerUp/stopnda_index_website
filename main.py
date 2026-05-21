import os
import sys
import time
import asyncio
from typing import Dict, Tuple, Any

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from binance_index import get_binance_live_data
from gate_index import get_gate_live_data

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

CACHE_TTL_SECONDS = 15
STALE_TTL_SECONDS = 60
MAX_CONCURRENT_EXTERNAL_REQUESTS = 3

cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
locks: Dict[Tuple[str, str], asyncio.Lock] = {}

external_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTERNAL_REQUESTS)


def normalize_symbol(symbol: str, exchange: str) -> str:
    s = symbol.strip().upper()
    s = s.replace("/", "").replace("-", "").replace(" ", "")
    s = s.replace("_PERP", "").replace("PERP", "")
    s = s.replace("_USDT", "").replace("USDT", "")

    if not s:
        raise ValueError("Пустой тикер")

    if exchange == "gate":
        return f"{s}_USDT"

    return f"{s}USDT"


async def fetch_exchange_data(symbol: str, exchange: str):
    async with external_semaphore:
        if exchange == "gate":
            return await asyncio.to_thread(get_gate_live_data, symbol)

        return await asyncio.to_thread(
            get_binance_live_data,
            symbol,
            "close",
        )


async def get_cached_metrics(symbol: str, exchange: str):
    now = time.time()
    key = (exchange, symbol)

    cached = cache.get(key)

    if cached and now - cached["ts"] <= CACHE_TTL_SECONDS:
        data = dict(cached["data"])
        data["_cache"] = "fresh"
        return data

    if key not in locks:
        locks[key] = asyncio.Lock()

    async with locks[key]:
        now = time.time()
        cached = cache.get(key)

        if cached and now - cached["ts"] <= CACHE_TTL_SECONDS:
            data = dict(cached["data"])
            data["_cache"] = "fresh_after_lock"
            return data

        try:
            data = await fetch_exchange_data(symbol, exchange)

            if "error" in data:
                raise Exception(data["error"])

            cache[key] = {
                "ts": time.time(),
                "data": data,
            }

            data = dict(data)
            data["_cache"] = "updated"
            return data

        except Exception as e:
            if cached and now - cached["ts"] <= STALE_TTL_SECONDS:
                data = dict(cached["data"])
                data["_cache"] = "stale"
                data["_warning"] = str(e)
                return data

            raise


@app.get("/", response_class=HTMLResponse)
async def route_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/check-auth")
async def api_check_auth():
    return {"authorized": True}


@app.get("/api/metrics")
async def api_metrics(symbol: str, exchange: str = "binance"):
    try:
        exchange = exchange.lower().strip()

        if exchange not in {"binance", "gate"}:
            raise HTTPException(status_code=400, detail="Unknown exchange")

        symbol_clean = normalize_symbol(symbol, exchange)
        data = await get_cached_metrics(symbol_clean, exchange)

        return data

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))