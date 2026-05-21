import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from binance_index import get_binance_live_data
from gate_index import get_gate_live_data

load_dotenv()
app = FastAPI()
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def route_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/check-auth")
async def api_check_auth():
    return {"authorized": True}
  
def normalize_symbol(symbol: str, exchange: str) -> str:
    s = symbol.strip().upper()
    s = s.replace("/", "").replace("-", "").replace(" ", "")
    s = s.replace("_PERP", "").replace("PERP", "")
    s = s.replace("_USDT", "").replace("USDT", "")

    if exchange.lower() == "gate":
        return f"{s}_USDT"

    return f"{s}USDT"

@app.get("/api/metrics")
async def api_metrics(symbol: str, exchange: str = "binance"):
    try:
        exchange = exchange.lower()
        symbol_clean = normalize_symbol(symbol, exchange)

        if exchange == "gate":
            data = get_gate_live_data(symbol_clean)
        else:
            data = get_binance_live_data(symbol_clean, price_mode="close")

        if "error" in data:
            raise HTTPException(status_code=400, detail=data["error"])

        return data

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))