import time
import requests

SETTLE = "usdt"
GATE_INTEREST_PERCENT = 0.01
GATE_CLAMP_PERCENT = 0.05
GATE_LIMIT_EXTRA_PERCENT = 0

def gate_request(url, params=None):
    response = requests.get(
        url,
        params=params,
        headers={"Accept": "application/json"},
        timeout=20,
    )
    if response.status_code >= 400:
        raise Exception(f"{response.status_code} {response.text} | url={response.url}")
    return response.json()

def gate_get_contract_info(contract):
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/contracts/{contract.upper()}"
    return gate_request(url)

def gate_get_ticker_info(contract):
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/tickers"
    res = gate_request(url, {"contract": contract.upper()})
    return res[0] if isinstance(res, list) and len(res) > 0 else {}

def gate_get_premium_index_paginated(contract, from_ts, to_ts):
    """Исправленная функция: собирает все точки через пагинацию."""
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/premium_index"
    all_items = []
    current_to = to_ts
    
    while current_to > from_ts:
        params = {
            "contract": contract.upper(),
            "from": from_ts,
            "to": current_to,
            "interval": "1m",
            "limit": 1000
        }
        batch = gate_request(url, params)
        if not batch or not isinstance(batch, list):
            break
        
        all_items.extend(batch)
        # Смещаем окно запроса: берем самый старый timestamp из полученных - 60 секунд
        min_ts = min([item['t'] for item in batch])
        if min_ts <= from_ts:
            break
        current_to = min_ts - 60
        
    unique = {int(item["t"]): item for item in all_items if "t" in item and "c" in item}
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
    return -cap_percent, cap_percent

def calc_weighted_avg(values):
    weighted_sum = sum(i * value for i, value in enumerate(values, start=1))
    weights_sum = sum(range(1, len(values) + 1))
    return weighted_sum / weights_sum if weights_sum else 0.0

def calc_gate_funding_from_premium(avg_premium_percent, interval_hours, floor_percent, cap_percent):
    inside = avg_premium_percent + clamp(
        GATE_INTEREST_PERCENT - avg_premium_percent,
        -GATE_CLAMP_PERCENT,
        GATE_CLAMP_PERCENT,
    )
    raw_funding = inside / (8 / interval_hours)
    return clamp(raw_funding, floor_percent, cap_percent)

def get_gate_target_premium_for_limit(target_funding_percent, interval_hours):
    scale = 8 / interval_hours
    target_inside = target_funding_percent * scale
    if target_inside < GATE_INTEREST_PERCENT:
        return target_inside - GATE_CLAMP_PERCENT - GATE_LIMIT_EXTRA_PERCENT
    if target_inside > GATE_INTEREST_PERCENT:
        return target_inside + GATE_CLAMP_PERCENT + GATE_LIMIT_EXTRA_PERCENT
    return GATE_INTEREST_PERCENT

def get_gate_live_data(contract: str):
    contract = contract.upper()
    contract_info = gate_get_contract_info(contract)
    ticker_info = gate_get_ticker_info(contract)

    funding_interval = int(contract_info.get("funding_interval", 0))
    funding_next_apply = int(float(contract_info.get("funding_next_apply", 0)))

    if funding_interval <= 0 or funding_next_apply <= 0:
        return {"error": "У контракта нет данных по funding cycle"}

    floor_percent, cap_percent = get_gate_funding_limits(contract_info, ticker_info)
    interval_hours = funding_interval / 3600
    
    # Расчет временных меток
    now_ts_raw = int(time.time())
    cycle_start_raw = funding_next_apply - funding_interval
    step_seconds = 60
    
    from_ts = cycle_start_raw - (cycle_start_raw % step_seconds)
    to_ts = now_ts_raw - (now_ts_raw % step_seconds)
    expected_total_points = int(funding_interval // step_seconds)

    # Использование исправленной функции пагинации
    items = gate_get_premium_index_paginated(contract, from_ts, to_ts)

    if not items:
        return {"error": "Gate не вернул premium_index"}

    by_ts = {int(item["t"]): item for item in items}
    expected_timestamps = list(range(cycle_start_raw, funding_next_apply, step_seconds))

    values_percent = []
    chart_points = []

    for ts in expected_timestamps:
        item = by_ts.get(ts)
        if item is None or "c" not in item:
            continue
        value_percent = float(item["c"]) * 100
        values_percent.append(value_percent)
        chart_points.append({"time": ts, "value": value_percent})

    if not values_percent:
        return {"error": "Нет ни одной premium-точки"}

    used_points = len(values_percent)
    last_value_percent = values_percent[-1]
    current_avg_percent = calc_weighted_avg(values_percent)

    points_left = expected_total_points - used_points
    if points_left <= 0:
        projected_avg_percent = current_avg_percent
    else:
        future_values = [last_value_percent] * points_left
        projected_avg_percent = calc_weighted_avg(values_percent + future_values)

    projected_funding_percent = calc_gate_funding_from_premium(
        projected_avg_percent, interval_hours, floor_percent, cap_percent
    )

    target_funding_percent = floor_percent if projected_avg_percent < 0 else cap_percent
    target_avg_percent = get_gate_target_premium_for_limit(target_funding_percent, interval_hours)

    if points_left > 0:
        current_weighted_sum = sum(i * value for i, value in enumerate(values_percent, start=1))
        current_weights_sum = sum(range(1, used_points + 1))
        remaining_weights_sum = sum(range(used_points + 1, expected_total_points + 1))
        total_weights_sum = current_weights_sum + remaining_weights_sum
        required_deviation_percent = (target_avg_percent * total_weights_sum - current_weighted_sum) / remaining_weights_sum
        req_dev_str = f"{required_deviation_percent:.6f}%"
    else:
        required_deviation_percent = 0.0
        req_dev_str = "0.000000% (Цикл завершен)"

    return {
        "symbol": contract,
        "price_mode": f"PREMIUM_INDEX | INTERVAL 1m | WEIGHTED ({interval_hours:.0f}h)",
        "current_avg": round(current_avg_percent, 6),
        "projected_avg": round(projected_avg_percent, 6),
        "current_funding": round(float(contract_info.get("funding_rate", 0)) * 100, 6),
        "projected_funding": round(projected_funding_percent, 6),
        "required_deviation": req_dev_str,
        "required_deviation_raw": round(required_deviation_percent, 6),
        "limits": f"{floor_percent:.4f}% / +{cap_percent:.4f}%",
        "time_left": max(0, funding_next_apply - now_ts_raw),
        "points_total": f"{used_points}/{expected_total_points}",
        "last_premium": round(last_value_percent, 6),
        "chart_data": chart_points,
    }