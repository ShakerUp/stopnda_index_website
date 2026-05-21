import time
import requests

GATE_BASE = "https://www.gate.com/apiw/v2/futures"
SETTLE = "usdt"

GATE_INTEREST_PERCENT = 0.01
GATE_CLAMP_PERCENT = 0.05

# Практический запас для Gate.
# Если лимит -2%, то цель premium average будет примерно -2.20%.
GATE_LIMIT_EXTRA_PERCENT = 0.15


def gate_get_contract_info(contract):
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/contracts/{contract.upper()}"
    response = requests.get(url, headers={"Accept": "application/json"}, timeout=20)
    response.raise_for_status()
    return response.json()


def gate_get_ticker_info(contract):
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/tickers"
    params = {"contract": contract.upper()}
    response = requests.get(url, params=params, headers={"Accept": "application/json"}, timeout=20)
    response.raise_for_status()
    res = response.json()
    return res[0] if isinstance(res, list) and len(res) > 0 else {}


def gate_get_premium_index(contract, from_ts, to_ts, interval):
    url = f"{GATE_BASE}/{SETTLE}/premium_index"
    params = {
        "contract": contract.upper(),
        "from": from_ts,
        "to": to_ts,
        "interval": interval,
        "limit": 600,
    }
    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    items = payload["data"] if isinstance(payload, dict) and "data" in payload else payload

    unique = {int(item["t"]): item for item in items if "t" in item}
    return [unique[t] for t in sorted(unique.keys())]


def clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


def get_gate_funding_limits(contract_info, ticker_info):
    cap = (
        contract_info.get("funding_rate_limit")
        or contract_info.get("funding_rate_cap")
        or ticker_info.get("funding_rate_limit")
        or ticker_info.get("funding_rate_cap")
        or 0.015
    )

    cap_percent = abs(float(cap)) * 100
    floor_percent = -cap_percent
    return floor_percent, cap_percent


def calc_gate_funding_from_premium(avg_premium_percent, floor_percent, cap_percent):
    interest_component = clamp(
        GATE_INTEREST_PERCENT - avg_premium_percent,
        -GATE_CLAMP_PERCENT,
        GATE_CLAMP_PERCENT,
    )

    funding_percent = avg_premium_percent + interest_component
    return clamp(funding_percent, floor_percent, cap_percent)


def get_gate_target_premium_for_limit(target_funding_percent):
    """
    Gate formula:
    funding = premium_avg + clamp(interest - premium_avg, -0.05%, +0.05%)

    Для отрицательного лимита обычно clamp = +0.05,
    значит premium_avg = target_funding - 0.05.

    Для положительного лимита обычно clamp = -0.05,
    значит premium_avg = target_funding + 0.05.

    Плюс добавляем practical extra 0.20%.
    """
    if target_funding_percent < 0:
        return target_funding_percent - GATE_CLAMP_PERCENT - GATE_LIMIT_EXTRA_PERCENT

    if target_funding_percent > 0:
        return target_funding_percent + GATE_CLAMP_PERCENT + GATE_LIMIT_EXTRA_PERCENT

    return 0.0


def get_gate_live_data(contract: str):
    contract = contract.upper()
    contract_info = gate_get_contract_info(contract)
    ticker_info = gate_get_ticker_info(contract)

    funding_interval = int(contract_info.get("funding_interval", 0))
    funding_next_apply = int(float(contract_info.get("funding_next_apply", 0)))

    floor_percent, cap_percent = get_gate_funding_limits(contract_info, ticker_info)

    if funding_interval <= 0 or funding_next_apply <= 0:
        return {"error": "У контракта нет данных по funding cycle"}

    now_ts_raw = int(time.time())
    cycle_start_raw = funding_next_apply - funding_interval

    period_seconds = now_ts_raw - cycle_start_raw
    if period_seconds <= 600 * 60:
        interval, step_seconds = "1m", 60
    else:
        interval, step_seconds = "5m", 300

    from_ts = cycle_start_raw - (cycle_start_raw % step_seconds)
    to_ts = now_ts_raw - (now_ts_raw % step_seconds)

    expected_total_points = funding_interval // step_seconds

    items = gate_get_premium_index(contract, from_ts, to_ts, interval=interval)
    if not items:
        return {"error": "Gate не вернул данных за этот период"}

    by_ts = {int(item["t"]): item for item in items}
    expected_timestamps = list(range(from_ts, to_ts, step_seconds))

    values = []
    chart_points = []

    for ts in expected_timestamps:
        item = by_ts.get(ts)
        if item is None or "c" not in item:
            continue

        value = float(item["c"])
        values.append(value)
        chart_points.append({"time": ts, "value": value * 100})

    if not values:
        return {"error": "Нет ни одной точки в выбранном диапазоне"}

    used_points = len(values)
    last_value = values[-1]

    current_avg_percent = (sum(values) / used_points) * 100

    if used_points >= expected_total_points:
        projected_avg_percent = current_avg_percent
    else:
        total_sum = sum(values) + (expected_total_points - used_points) * last_value
        projected_avg_percent = (total_sum / expected_total_points) * 100

    projected_funding_percent = calc_gate_funding_from_premium(
        projected_avg_percent,
        floor_percent,
        cap_percent,
    )

    points_left = expected_total_points - used_points

    if projected_avg_percent < 0:
        target_funding_percent = floor_percent
    else:
        target_funding_percent = cap_percent

    target_avg_percent = get_gate_target_premium_for_limit(target_funding_percent)

    if points_left > 0:
        current_sum_percent = sum(values) * 100

        required_deviation_percent = (
            target_avg_percent * expected_total_points - current_sum_percent
        ) / points_left

        req_dev_str = f"{required_deviation_percent:.6f}%"
    else:
        req_dev_str = "0.000000% (Цикл завершен)"

    current_funding = float(contract_info.get("funding_rate", 0)) * 100

    return {
        "symbol": contract,
        "price_mode": f"INTERVAL {interval}",
        "current_avg": round(current_avg_percent, 6),
        "projected_avg": round(projected_avg_percent, 6),
        "current_funding": round(current_funding, 6),
        "projected_funding": round(projected_funding_percent, 6),
        "required_deviation": req_dev_str,
        "limits": f"{floor_percent:.4f}% / +{cap_percent:.4f}%",
        "time_left": max(0, funding_next_apply - now_ts_raw),
        "points_total": f"{used_points}/{expected_total_points}",
        "chart_data": chart_points,
    }