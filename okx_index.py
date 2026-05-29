import time
import asyncio
import httpx

OKX_BASE = "https://www.okx.com"

STEP_SECONDS = 60
CLAMP_PERCENT = 0.05
DAILY_INTEREST_PERCENT = 0.03


def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


def normalize_okx_symbol(symbol: str):
    s = symbol.strip().upper()
    s = s.replace("/", "").replace("-", "").replace("_", "").replace(" ", "")
    s = s.replace("PERP", "").replace("SWAP", "")
    s = s.replace("USDT", "")

    if not s:
        raise ValueError("Пустой тикер")

    return f"{s}-USDT-SWAP", s, f"{s}-USDT"


async def okx_get(client: httpx.AsyncClient, path: str, params: dict = None):
    url = f"{OKX_BASE}{path}"

    r = await client.get(
        url,
        params=params or {},
        timeout=15.0,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
        },
    )

    r.raise_for_status()
    payload = r.json()

    if str(payload.get("code")) != "0":
        raise Exception(payload.get("msg", "OKX API error"))

    return payload.get("data", [])


async def get_okx_funding_info(client: httpx.AsyncClient, inst_id: str):
    data = await okx_get(
        client,
        "/api/v5/public/funding-rate",
        {"instId": inst_id},
    )

    if not data:
        raise Exception(f"OKX не вернул funding-rate для {inst_id}")

    return data[0]


async def get_okx_premium_history(
    client: httpx.AsyncClient,
    inst_id: str,
    from_ts: int,
    to_ts: int,
):
    all_items = []
    cursor_ms = to_ts * 1000
    from_ms = from_ts * 1000

    for _ in range(20):
        data = await okx_get(
            client,
            "/api/v5/public/premium-history",
            {
                "instId": inst_id,
                "after": cursor_ms,  # after = старше cursor_ms (движение назад)
                "limit": 100,
            },
        )

        if not data:
            break

        all_items.extend(data)

        timestamps = []
        for item in data:
            try:
                timestamps.append(int(item.get("ts")))
            except Exception:
                pass

        if not timestamps:
            break

        oldest_ms = min(timestamps)

        if oldest_ms <= from_ms:
            break

        cursor_ms = oldest_ms - 1

    by_minute = {}

    for item in all_items:
        try:
            ts = int(int(item.get("ts")) // 1000)
            minute_ts = ts - (ts % STEP_SECONDS)

            premium_raw = item.get("premium")

            if premium_raw is None:
                continue

            premium_percent = float(premium_raw) * 100

            # Не перезаписываем — оставляем первое (самое свежее при обратном обходе)
            if minute_ts not in by_minute:
                by_minute[minute_ts] = premium_percent
        except Exception:
            continue

    return {
        ts: value
        for ts, value in by_minute.items()
        if from_ts <= ts < to_ts
    }


async def get_okx_history_candles(
    client: httpx.AsyncClient,
    path: str,
    inst_id: str,
    from_ts: int,
    to_ts: int,
):
    all_rows = []
    cursor_ms = to_ts * 1000
    from_ms = from_ts * 1000

    for _ in range(30):
        data = await okx_get(
            client,
            path,
            {
                "instId": inst_id,
                "bar": "1m",
                "after": cursor_ms,  # after = старше cursor_ms (движение назад)
                "limit": 100,
            },
        )

        if not data:
            break

        all_rows.extend(data)

        timestamps = []
        for row in data:
            try:
                timestamps.append(int(row[0]))
            except Exception:
                pass

        if not timestamps:
            break

        oldest_ms = min(timestamps)

        if oldest_ms <= from_ms:
            break

        cursor_ms = oldest_ms - 1

    by_minute = {}

    for row in all_rows:
        try:
            ts = int(int(row[0]) // 1000)
            minute_ts = ts - (ts % STEP_SECONDS)
            close = float(row[4])
            by_minute[minute_ts] = close
        except Exception:
            continue

    return {
        ts: value
        for ts, value in by_minute.items()
        if from_ts <= ts < to_ts
    }


async def get_okx_premium_from_mark_index(
    client: httpx.AsyncClient,
    swap_inst_id: str,
    index_inst_id: str,
    from_ts: int,
    to_ts: int,
):
    mark_task = get_okx_history_candles(
        client,
        "/api/v5/market/history-mark-price-candles",
        swap_inst_id,
        from_ts,
        to_ts,
    )

    index_task = get_okx_history_candles(
        client,
        "/api/v5/market/history-index-candles",
        index_inst_id,
        from_ts,
        to_ts,
    )

    mark_by_ts, index_by_ts = await asyncio.gather(mark_task, index_task)

    premium_by_ts = {}

    for ts, mark in mark_by_ts.items():
        index = index_by_ts.get(ts)

        if index is None or index == 0:
            continue

        premium_by_ts[ts] = ((mark - index) / index) * 100

    return premium_by_ts


def calc_okx_funding_from_premium(
    avg_premium_percent: float,
    interval_hours: float,
    floor_percent: float,
    cap_percent: float,
):
    interest_percent = DAILY_INTEREST_PERCENT / (24 / interval_hours)

    interest_component = clamp(
        interest_percent - avg_premium_percent,
        -CLAMP_PERCENT,
        CLAMP_PERCENT,
    )

    raw_funding = avg_premium_percent + interest_component

    return clamp(raw_funding, floor_percent, cap_percent)


def get_okx_target_premium_for_limit(
    target_funding_percent: float,
    interval_hours: float,
):
    interest_percent = DAILY_INTEREST_PERCENT / (24 / interval_hours)

    if target_funding_percent < interest_percent:
        return target_funding_percent - CLAMP_PERCENT

    if target_funding_percent > interest_percent:
        return target_funding_percent + CLAMP_PERCENT

    return interest_percent


async def get_okx_live_data(symbol: str):
    try:
        inst_id, display_symbol, index_inst_id = normalize_okx_symbol(symbol)
    except Exception as e:
        return {"error": str(e)}

    async with httpx.AsyncClient() as client:
        try:
            funding_info = await get_okx_funding_info(client, inst_id)

            current_funding_percent = float(funding_info.get("fundingRate", 0)) * 100

            funding_time_ms = int(funding_info.get("fundingTime", 0))
            next_funding_time_ms = int(funding_info.get("nextFundingTime", 0) or 0)

            if funding_time_ms <= 0:
                return {"error": "OKX не вернул fundingTime"}

            if next_funding_time_ms > funding_time_ms:
                interval_seconds = (next_funding_time_ms - funding_time_ms) // 1000
            else:
                interval_seconds = 8 * 3600

            interval_hours = interval_seconds / 3600

            floor_percent = float(funding_info.get("minFundingRate", -0.02)) * 100
            cap_percent = float(funding_info.get("maxFundingRate", 0.02)) * 100

            now_ts_raw = int(time.time())

            next_funding_ts = funding_time_ms // 1000
            cycle_start_ts = next_funding_ts - interval_seconds

            from_ts = cycle_start_ts - (cycle_start_ts % STEP_SECONDS)
            to_ts = now_ts_raw - (now_ts_raw % STEP_SECONDS)

            expected_total_points = int(interval_seconds // STEP_SECONDS)

            premium_by_ts = await get_okx_premium_history(
                client,
                inst_id,
                from_ts,
                to_ts,
            )

            if not premium_by_ts:
                premium_by_ts = await get_okx_premium_from_mark_index(
                    client,
                    inst_id,
                    index_inst_id,
                    from_ts,
                    to_ts,
                )

        except Exception as api_err:
            return {"error": f"OKX API Error: {str(api_err)}"}

    expected_timestamps = list(range(from_ts, to_ts, STEP_SECONDS))

    values_percent = []
    chart_points = []

    weighted_sum = 0.0
    weights_sum = 0.0

    for i, ts in enumerate(expected_timestamps, start=1):
        value = premium_by_ts.get(ts)

        if value is None:
            continue

        values_percent.append(value)
        chart_points.append({"time": ts, "value": value})

        weighted_sum += i * value
        weights_sum += i

    if not values_percent or weights_sum == 0:
        return {"error": f"OKX не вернул premium/mark-index данные для {inst_id}"}

    used_points = len(values_percent)
    last_value_percent = values_percent[-1]

    current_avg_percent = weighted_sum / weights_sum

    if len(expected_timestamps) >= expected_total_points:
        projected_avg_percent = current_avg_percent
    else:
        projected_weighted_sum = weighted_sum
        projected_weights_sum = weights_sum

        last_known_index = len(expected_timestamps)

        for i in range(last_known_index + 1, expected_total_points + 1):
            projected_weighted_sum += i * last_value_percent
            projected_weights_sum += i

        projected_avg_percent = projected_weighted_sum / projected_weights_sum

    projected_funding_percent = calc_okx_funding_from_premium(
        projected_avg_percent,
        interval_hours,
        floor_percent,
        cap_percent,
    )

    if projected_avg_percent < 0:
        target_funding_percent = floor_percent
    else:
        target_funding_percent = cap_percent

    target_avg_percent = get_okx_target_premium_for_limit(
        target_funding_percent,
        interval_hours,
    )

    points_left = expected_total_points - len(expected_timestamps)

    if points_left > 0:
        remaining_weights_sum = sum(
            i for i in range(len(expected_timestamps) + 1, expected_total_points + 1)
        )

        total_weights_sum = weights_sum + remaining_weights_sum

        required_deviation_percent = (
            target_avg_percent * total_weights_sum - weighted_sum
        ) / remaining_weights_sum

        req_dev_str = f"{required_deviation_percent:.6f}%"
    else:
        req_dev_str = "0.000000% (Цикл завершен)"

    return {
        "symbol": display_symbol,
        "price_mode": f"INTERVAL {interval_hours:g}h | WEIGHTED",
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