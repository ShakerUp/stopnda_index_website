import time
import requests

GATE_BASE = "https://www.gate.com/apiw/v2/futures"
SETTLE = "usdt"


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

    interest_rate_8h_percent = 0.01
    funding_interval_hours = funding_interval / 3600

    damped = clamp(interest_rate_8h_percent - projected_avg_percent, -0.05, 0.05)
    raw_rate = (projected_avg_percent + damped) / (8 / funding_interval_hours)
    projected_funding_percent = clamp(raw_rate, floor_percent, cap_percent)

    # Минимально нужное среднее отклонение на остаток цикла
    points_left = expected_total_points - used_points

    # Цель — нижний лимит funding, например -2.000000%
    target_avg_percent = floor_percent

    if points_left > 0:
        current_sum_percent = current_avg_percent * used_points

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