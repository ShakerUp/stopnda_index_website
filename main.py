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
from fastapi.staticfiles import StaticFiles


from binance_index import get_binance_live_data
from gate_index import get_gate_live_data
from bitget_index import get_bitget_live_data
from bybit_index import get_bybit_live_data

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

CACHE_TTL_SECONDS = 15
STALE_TTL_SECONDS = 60
MAX_CONCURRENT_EXTERNAL_REQUESTS = 3

cache: Dict[Tuple[str, str], Dict[str, Any]] = {}

# Глобальный лок для безопасного создания индивидуальных локов
locks_master_lock = asyncio.Lock()
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
        elif exchange == "bitget":
            return await get_bitget_live_data(symbol)
        elif exchange == "bybit":
            return await get_bybit_live_data(symbol)  # Прямой вызов асинхронной функции

        return await asyncio.to_thread(get_binance_live_data, symbol, "close")


async def get_cached_metrics(symbol: str, exchange: str):
    now = time.time()
    key = (exchange, symbol)

    # 1. Быстрая проверка "свежего" кэша БЕЗ блокировок
    cached = cache.get(key)
    if cached and now - cached["ts"] <= CACHE_TTL_SECONDS:
        data = dict(cached["data"])
        data["_cache"] = "fresh"
        return data

    # Safe lock получение/создание (защита от Race Condition)
    async with locks_master_lock:
        if key not in locks:
            locks[key] = asyncio.Lock()
    
    # 2. Входим в индивидуальный лок монеты
    async with locks[key]:
        now = time.time()
        cached = cache.get(key)

        # Проверяем, возможно пока мы стояли в очереди, предыдущий запрос уже обновил кэш
        if cached and now - cached["ts"] <= CACHE_TTL_SECONDS:
            data = dict(cached["data"])
            data["_cache"] = "fresh_after_lock"
            return data

        try:
            # Идем в сеть за данными
            data = await fetch_exchange_data(symbol, exchange)

            if not data or "error" in data:
                raise Exception(data.get("error", "Unknown error from exchange script"))

            cache[key] = {
                "ts": time.time(),
                "data": data,
            }

            data = dict(data)
            data["_cache"] = "updated"
            return data

        except Exception as e:
            # Если биржа лежит или выдала ошибку — отдаем старый кэш (Stale-while-revalidate)
            if cached and now - cached["ts"] <= STALE_TTL_SECONDS:
                data = dict(cached["data"])
                data["_cache"] = "stale"
                data["_warning"] = str(e)
                return data
            raise


@app.get("/", response_class=HTMLResponse)
async def route_index(request: Request):
    # ИСПРАВЛЕНО: Новый синтаксис, который не вызывает ошибку 500
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/check-auth")
async def api_check_auth():
    return {"authorized": True}


@app.get("/api/metrics")
async def api_metrics(symbol: str, exchange: str = "binance"):
    try:
        exchange = exchange.lower().strip()

        if exchange not in {"binance", "gate", "bitget", "bybit"}:
            raise HTTPException(status_code=400, detail="Unknown exchange")

        symbol_clean = normalize_symbol(symbol, exchange)
        data = await get_cached_metrics(symbol_clean, exchange)

        return data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))