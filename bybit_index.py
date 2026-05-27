import time
import httpx
import asyncio

BYBIT_BASE = "https://api.bybit.com"

INTEREST_RATE_DAILY_PERCENT = 0.03
CLAMP_PERCENT = 0.05
STEP_SECONDS = 60


def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


async def bybit_get(client: httpx.AsyncClient, path: str, params: dict = None):
    url = f"{BYBIT_BASE}{path}"
    r = await client.get(url, params=params or {}, timeout=10.0)
    r.raise_for_status()

    payload = r.json()

    if payload.get("retCode") != 0:
        raise Exception(payload.get("retMsg", "Bybit API error"))

    return payload.get("result", {})


def normalize_symbol(symbol: str):
    s = symbol.upper().replace("/", "").replace("-", "").replace(" ", "")
    s = s.replace("_PERP", "").replace("PERP", "")

    if not s.endswith("USDT") and not s.endswith("USDC"):
        s = f"{s}USDT"

    return s


def get_interval_interest_percent(interval_hours: int, symbol: str):
    # Bybit: Interest Rate = 0.03% / (24 / funding_interval)
    # Для некоторых пар типа USDCUSDT / ETHBTCUSDT interest может быть 0%.
    if symbol in {"USDCUSDT", "ETHBTCUSDT"}:
        return 0.0

    return INTEREST_RATE_DAILY_PERCENT / (24 / interval_hours)


def calc_bybit_funding_from_premium(
    avg_premium_percent: float,
    interval_interest_percent: float,
    floor_percent: float,
    cap_percent: float,
):
    inner = clamp(
        interval_interest_percent - avg_premium_percent,
        -CLAMP_PERCENT,
        CLAMP_PERCENT,
    )

    raw_funding = avg_premium_percent + inner

    return clamp(raw_funding, floor_percent, cap_percent)


def get_target_premium_for_funding(
    target_funding_percent: float,
    interval_interest_percent: float,
):
    """
    Bybit:
    F = P + clamp(I - P, -0.05%, +0.05%)

    Для сильного отрицательного funding clamp обычно +0.05%:
    F = P + 0.05 => P = F - 0.05

    Для сильного положительного funding clamp обычно -0.05%:
    F = P - 0.05 => P = F + 0.05
    """

    if target_funding_percent < interval_interest_percent:
        return target_funding_percent - CLAMP_PERCENT

    if target_funding_percent > interval_interest_percent:
        return target_funding_percent + CLAMP_PERCENT

    return interval_interest_percent


async def get_bybit_live_data(symbol: str):
    symbol_clean = normalize_symbol(symbol)

    async with httpx.AsyncClient() as client:
        try:
            ticker_task = bybit_get(
                client,
                "/v5/market/tickers",
                {
                    "category": "linear",
                    "symbol": symbol_clean,
                },
            )

            instr_task = bybit_get(
                client,
                "/v5/market/instruments-info",
                {
                    "category": "linear",
                    "symbol": symbol_clean,
                },
            )

            ticker_res, instr_res = await asyncio.gather(ticker_task, instr_task)

            ticker_list = ticker_res.get("list", [])
            instr_list = instr_res.get("list", [])

            if not ticker_list or not instr_list:
                return {"error": f"Символ {symbol_clean} не найден в листинге Bybit"}

            ticker_data = ticker_list[0]
            instr_data = instr_list[0]

            current_funding_percent = float(ticker_data.get("fundingRate", 0)) * 100
            next_funding_ts = int(int(ticker_data.get("nextFundingTime")) // 1000)

            funding_interval_minutes = int(instr_data.get("fundingInterval", 480))
            interval_hours = funding_interval_minutes // 60

            # ВАЖНО: лимиты берём из instruments-info, а не хардкодим -2/+2
            cap_percent = float(instr_data.get("upperFundingRate", 0.02)) * 100
            floor_percent = float(instr_data.get("lowerFundingRate", -0.02)) * 100

            now_ts = int(time.time())
            cycle_start_ts = next_funding_ts - interval_hours * 3600

            from_ts = cycle_start_ts - (cycle_start_ts % STEP_SECONDS)
            to_ts = now_ts - (now_ts % STEP_SECONDS)

            expected_total_points = interval_hours * 60

            premium_res = await bybit_get(
                client,
                "/v5/market/premium-index-price-kline",
                {
                    "category": "linear",
                    "symbol": symbol_clean,
                    "interval": "1",
                    "start": from_ts * 1000,
                    "end": to_ts * 1000,
                    "limit": 1000,
                },
            )

            candles = premium_res.get("list", [])

        except Exception as api_err:
            return {"error": f"Bybit API Error: {str(api_err)}"}

    # Bybit отдаёт свечи обычно от новых к старым
    candles.reverse()

    by_ts = {}

    for c in candles:
        try:
            ts = int(int(c[0]) // 1000)
            premium_percent = float(c[4]) * 100
            by_ts[ts] = premium_percent
        except Exception:
            continue

    values_percent = []
    chart_points = []

    weighted_sum = 0.0
    weights_sum = 0.0

    expected_timestamps = list(range(from_ts, to_ts, STEP_SECONDS))

    for i, ts in enumerate(expected_timestamps, start=1):
        value = by_ts.get(ts)

        if value is None:
            continue

        values_percent.append(value)
        chart_points.append({"time": ts, "value": value})

        # Bybit TWAP: чем ближе к settlement, тем больше вес
        weighted_sum += i * value
        weights_sum += i

    if not values_percent or weights_sum == 0:
        return {"error": "Bybit не вернул валидные premium-index точки"}

    used_points = len(values_percent)
    last_value = values_percent[-1]

    current_avg_percent = weighted_sum / weights_sum

    if used_points >= expected_total_points:
        projected_avg_percent = current_avg_percent
    else:
        projected_weighted_sum = weighted_sum
        projected_weights_sum = weights_sum

        # Важно: веса продолжаются по реальному номеру минуты в funding cycle
        for i in range(len(expected_timestamps) + 1, expected_total_points + 1):
            projected_weighted_sum += i * last_value
            projected_weights_sum += i

        projected_avg_percent = projected_weighted_sum / projected_weights_sum

    interval_interest_percent = get_interval_interest_percent(interval_hours, symbol_clean)

    projected_funding_percent = calc_bybit_funding_from_premium(
        projected_avg_percent,
        interval_interest_percent,
        floor_percent,
        cap_percent,
    )

    # ===== REQUIRED DEVIATION =====
    points_left = expected_total_points - len(expected_timestamps)

    if projected_avg_percent < 0:
        target_funding_percent = floor_percent
    else:
        target_funding_percent = cap_percent

    target_premium_percent = get_target_premium_for_funding(
        target_funding_percent,
        interval_interest_percent,
    )

    if points_left > 0:
        remaining_weights_sum = sum(
            i for i in range(len(expected_timestamps) + 1, expected_total_points + 1)
        )

        total_weights_sum = weights_sum + remaining_weights_sum

        required_deviation_percent = (
            target_premium_percent * total_weights_sum - weighted_sum
        ) / remaining_weights_sum

        req_dev_str = f"{required_deviation_percent:.6f}%"
    else:
        req_dev_str = "0.000000% (Цикл завершен)"

    display_symbol = symbol_clean.replace("USDT", "").replace("USDC", "")

    return {
        "symbol": display_symbol,
        "price_mode": f"INTERVAL {interval_hours}h",
        "current_avg": round(current_avg_percent, 6),
        "projected_avg": round(projected_avg_percent, 6),
        "current_funding": round(current_funding_percent, 6),
        "projected_funding": round(projected_funding_percent, 6),
        "required_deviation": req_dev_str,
        "limits": f"{floor_percent:.4f}% / +{cap_percent:.4f}%",
        "time_left": max(0, next_funding_ts - now_ts),
        "points_total": f"{used_points}/{expected_total_points}",
        "chart_data": chart_points,
    }