import time
import requests

BINANCE_FAPI_BASE = "https://fapi.binance.com"
STEP_SECONDS = 60
KLINE_INTERVAL = "1m"


def binance_get(path, params=None):
    url = f"{BINANCE_FAPI_BASE}{path}"
    response = requests.get(url, params=params or {}, timeout=20)
    response.raise_for_status()
    return response.json()


def binance_get_symbol_funding_config(symbol):
    try:
        payload = binance_get("/fapi/v1/fundingInfo")
        for item in payload:
            if str(item.get("symbol", "")).upper() == symbol.upper():
                return {
                    "funding_interval_hours": int(item.get("fundingIntervalHours", 8)),
                    "cap": float(item.get("adjustedFundingRateCap", 0.02)),
                    "floor": float(item.get("adjustedFundingRateFloor", -0.02)),
                }
    except Exception:
        pass

    return {
        "funding_interval_hours": 8,
        "cap": 0.02,
        "floor": -0.02,
    }


def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


def get_target_premium_for_funding(
    target_funding_percent,
    interest_rate_8h_percent,
    funding_interval_hours,
):
    """
    Binance formula:
    funding = (premium_avg + clamp(interest - premium_avg, -0.05%, +0.05%)) / (8 / interval_hours)

    Возвращает target Average Premium Index в процентах,
    который нужен, чтобы получить target funding.
    """
    scale = 8 / funding_interval_hours
    target_inside = target_funding_percent * scale

    # Для сильного отрицательного funding clamp обычно +0.05%
    if target_inside < interest_rate_8h_percent:
        return target_inside - 0.05

    # Для сильного положительного funding clamp обычно -0.05%
    if target_inside > interest_rate_8h_percent:
        return target_inside + 0.05

    return interest_rate_8h_percent


def get_required_deviation_for_target_avg(
    values_percent,
    used_points,
    expected_total_points,
    target_avg_percent,
    funding_interval_hours,
):
    points_left = expected_total_points - used_points

    if points_left <= 0:
        return "0.000000% (Цикл завершен)"

    if funding_interval_hours <= 1:
        current_sum = sum(values_percent)
        required_val = (
            target_avg_percent * expected_total_points - current_sum
        ) / points_left
    else:
        known_weighted_sum = 0.0
        for i, value in enumerate(values_percent, start=1):
            known_weighted_sum += i * value

        total_weights_sum = expected_total_points * (expected_total_points + 1) / 2

        remaining_weights_sum = 0.0
        for i in range(used_points + 1, expected_total_points + 1):
            remaining_weights_sum += i

        required_val = (
            target_avg_percent * total_weights_sum - known_weighted_sum
        ) / remaining_weights_sum

    return f"{required_val:.6f}%"


def get_binance_live_data(symbol: str, price_mode: str = "close"):
    symbol = symbol.upper()
    price_mode = price_mode.lower()

    premium_info = binance_get("/fapi/v1/premiumIndex", {"symbol": symbol})
    funding_cfg = binance_get_symbol_funding_config(symbol)

    next_funding_ts = int(premium_info["nextFundingTime"]) // 1000
    interest_rate_8h_percent = float(premium_info.get("interestRate", 0.0001)) * 100
    current_funding_percent = float(premium_info.get("lastFundingRate", "0")) * 100

    funding_interval_hours = funding_cfg["funding_interval_hours"]
    cap_percent = funding_cfg["cap"] * 100
    floor_percent = funding_cfg["floor"] * 100

    now_ts_raw = int(time.time())
    cycle_start_raw = next_funding_ts - (funding_interval_hours * 3600)

    from_ts = cycle_start_raw - (cycle_start_raw % STEP_SECONDS)
    to_ts = now_ts_raw - (now_ts_raw % STEP_SECONDS)

    expected_total_points = funding_interval_hours * 60

    klines = binance_get(
        "/fapi/v1/premiumIndexKlines",
        {
            "symbol": symbol,
            "interval": KLINE_INTERVAL,
            "startTime": from_ts * 1000,
            "endTime": to_ts * 1000,
            "limit": 1500,
        },
    )

    if not klines:
        return {"error": "Binance не вернул premiumIndexKlines"}

    by_ts = {int(k[0] // 1000): k for k in klines}
    expected_timestamps = list(range(from_ts, to_ts, STEP_SECONDS))

    values_percent = []
    chart_points = []

    for ts in expected_timestamps:
        k = by_ts.get(ts)
        if k is None:
            continue

        if price_mode == "open":
            value = float(k[1])
        elif price_mode == "mid":
            value = (float(k[1]) + float(k[4])) / 2
        else:
            value = float(k[4])

        val_pct = value * 100
        values_percent.append(val_pct)
        chart_points.append({"time": ts, "value": val_pct})

    if not values_percent:
        return {"error": "Нет точек в выбранном диапазоне"}

    used_points = len(values_percent)
    last_value = values_percent[-1]

    if funding_interval_hours <= 1:
        current_avg_percent = sum(values_percent) / used_points
    else:
        weighted_sum = 0.0
        weights_sum = 0.0

        for i, value in enumerate(values_percent, start=1):
            weighted_sum += i * value
            weights_sum += i

        current_avg_percent = weighted_sum / weights_sum

    if used_points >= expected_total_points:
        projected_avg_percent = current_avg_percent
    else:
        if funding_interval_hours <= 1:
            total_sum = sum(values_percent) + (
                expected_total_points - used_points
            ) * last_value
            projected_avg_percent = total_sum / expected_total_points
        else:
            weighted_sum = 0.0
            weights_sum = 0.0

            for i, value in enumerate(values_percent, start=1):
                weighted_sum += i * value
                weights_sum += i

            for i in range(used_points + 1, expected_total_points + 1):
                weighted_sum += i * last_value
                weights_sum += i

            projected_avg_percent = weighted_sum / weights_sum

    damped = clamp(interest_rate_8h_percent - projected_avg_percent, -0.05, 0.05)
    raw_rate = (projected_avg_percent + damped) / (8 / funding_interval_hours)
    projected_funding_percent = clamp(raw_rate, floor_percent, cap_percent)

    if projected_avg_percent < 0:
        target_funding_percent = floor_percent
    else:
        target_funding_percent = cap_percent

    target_avg_percent = get_target_premium_for_funding(
        target_funding_percent=target_funding_percent,
        interest_rate_8h_percent=interest_rate_8h_percent,
        funding_interval_hours=funding_interval_hours,
    )

    req_dev_str = get_required_deviation_for_target_avg(
        values_percent=values_percent,
        used_points=used_points,
        expected_total_points=expected_total_points,
        target_avg_percent=target_avg_percent,
        funding_interval_hours=funding_interval_hours,
    )

    return {
        "symbol": symbol,
        "price_mode": price_mode.upper(),
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