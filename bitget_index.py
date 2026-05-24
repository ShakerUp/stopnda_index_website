import time
import httpx
import asyncio

BITGET_BASE = "https://api.bitget.com"
PRODUCT_TYPE = "usdt-futures"

STEP_SECONDS = 60
GRANULARITY = "1m"

INTEREST_PERCENT = 0.01
CLAMP_PERCENT = 0.05


def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


async def bitget_get(client: httpx.AsyncClient, path: str, params: dict = None):
    url = f"{BITGET_BASE}{path}"

    r = await client.get(url, params=params or {}, timeout=10.0)
    r.raise_for_status()

    payload = r.json()

    if str(payload.get("code")) != "00000":
        raise Exception(payload.get("msg", "Bitget API error"))

    return payload.get("data")


async def get_current_funding(client: httpx.AsyncClient, symbol: str):
    data = await bitget_get(
        client,
        "/api/v2/mix/market/current-fund-rate",
        {
            "symbol": symbol,
            "productType": PRODUCT_TYPE,
        },
    )

    if isinstance(data, list) and data:
        return data[0]

    raise Exception("Bitget не вернул current funding")


async def get_paged_candles(
    client: httpx.AsyncClient,
    path: str,
    symbol: str,
    from_ts: int,
    to_ts: int,
):
    all_rows = []

    cursor_ms = from_ts * 1000
    end_ms = to_ts * 1000

    while cursor_ms < end_ms:
        chunk_end_ms = min(
            cursor_ms + 200 * STEP_SECONDS * 1000,
            end_ms,
        )

        rows = await bitget_get(
            client,
            path,
            {
                "symbol": symbol,
                "productType": PRODUCT_TYPE,
                "granularity": GRANULARITY,
                "startTime": cursor_ms,
                "endTime": chunk_end_ms,
                "limit": 200,
            },
        ) or []

        all_rows.extend(rows)

        cursor_ms = chunk_end_ms

    unique = {}

    for row in all_rows:
        try:
            ts = int(int(row[0]) // 1000)
            close = float(row[4])
            unique[ts] = close
        except Exception:
            continue

    return unique


def calc_funding_from_premium(
    avg_premium_percent,
    interval_hours,
    floor_percent,
    cap_percent,
):
    damped = clamp(
        INTEREST_PERCENT - avg_premium_percent,
        -CLAMP_PERCENT,
        CLAMP_PERCENT,
    )

    raw = (avg_premium_percent + damped) / (8 / interval_hours)

    return clamp(raw, floor_percent, cap_percent)


def get_target_premium_for_funding(target_funding_percent, interval_hours):
    scale = 8 / interval_hours
    target_inside = target_funding_percent * scale

    if target_inside < INTEREST_PERCENT:
        return target_inside - CLAMP_PERCENT

    if target_inside > INTEREST_PERCENT:
        return target_inside + CLAMP_PERCENT

    return INTEREST_PERCENT


async def get_bitget_live_data(symbol: str):
    s = symbol.strip().upper()
    s = s.replace("/", "").replace("-", "").replace(" ", "")
    s = s.replace("_PERP", "").replace("PERP", "")
    s = s.replace("_USDT", "").replace("USDT", "")

    if not s:
        return {"error": "Пустой тикер"}

    symbol_final = f"{s}USDT"

    async with httpx.AsyncClient() as client:
        try:
            funding_info = await get_current_funding(client, symbol_final)

            current_funding_percent = float(funding_info.get("fundingRate", 0)) * 100
            interval_hours = int(float(funding_info.get("fundingRateInterval", 8)))
            next_funding_ts = int(int(funding_info.get("nextUpdate")) // 1000)

            floor_percent = float(funding_info.get("minFundingRate", -0.02)) * 100
            cap_percent = float(funding_info.get("maxFundingRate", 0.02)) * 100

            now_ts_raw = int(time.time())
            cycle_start_raw = next_funding_ts - interval_hours * 3600

            from_ts = cycle_start_raw - (cycle_start_raw % STEP_SECONDS)
            to_ts = now_ts_raw - (now_ts_raw % STEP_SECONDS)

            expected_total_points = interval_hours * 60

            mark_task = get_paged_candles(
                client,
                "/api/v2/mix/market/history-mark-candles",
                symbol_final,
                from_ts,
                to_ts,
            )

            index_task = get_paged_candles(
                client,
                "/api/v2/mix/market/history-index-candles",
                symbol_final,
                from_ts,
                to_ts,
            )

            mark_by_ts, index_by_ts = await asyncio.gather(
                mark_task,
                index_task,
            )

        except Exception as api_err:
            return {"error": f"Bitget API Error: {str(api_err)}"}

    values_percent = []
    chart_points = []

    expected_timestamps = list(range(from_ts, to_ts, STEP_SECONDS))

    for ts in expected_timestamps:
        mark = mark_by_ts.get(ts)
        index = index_by_ts.get(ts)

        if mark is None or index is None or index == 0:
            continue

        premium_percent = ((mark - index) / index) * 100

        values_percent.append(premium_percent)
        chart_points.append({"time": ts, "value": premium_percent})

    if not values_percent:
        return {"error": f"Bitget не вернул данные свечей для {symbol_final}"}

    used_points = len(values_percent)
    last_value = values_percent[-1]

    current_avg_percent = sum(values_percent) / used_points

    if used_points >= expected_total_points:
        projected_avg_percent = current_avg_percent
    else:
        total_sum = sum(values_percent) + (
            expected_total_points - used_points
        ) * last_value

        projected_avg_percent = total_sum / expected_total_points

    projected_funding_percent = calc_funding_from_premium(
        projected_avg_percent,
        interval_hours,
        floor_percent,
        cap_percent,
    )

    if projected_avg_percent < 0:
        target_funding_percent = floor_percent
    else:
        target_funding_percent = cap_percent

    target_avg_percent = get_target_premium_for_funding(
        target_funding_percent,
        interval_hours,
    )

    points_left = expected_total_points - used_points

    if points_left > 0:
        current_sum = sum(values_percent)

        required_deviation_percent = (
            target_avg_percent * expected_total_points - current_sum
        ) / points_left

        req_dev_str = f"{required_deviation_percent:.6f}%"
    else:
        req_dev_str = "0.000000% (Цикл завершен)"

    return {
        "symbol": s,
        "price_mode": f"INTERVAL {interval_hours}h",
        "current_avg": round(current_avg_percent, 6),
        "projected_avg": round(projected_avg_percent, 6),
        "current_funding": round(current_funding_percent, 6),
        "projected_funding": round(projected_funding_percent, 6),
        "required_deviation": req_dev_str,
        "limits": f"{floor_percent:.4f}% / +{cap_percent:.4f}%",
        "time_left": max(0, next_funding_ts - now_ts_raw),
        "points_total": f"{used_points}/{expected_total_points}",
        "chart_data": chart_points,
    }