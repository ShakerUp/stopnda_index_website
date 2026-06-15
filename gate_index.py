import time
import requests

GATE_BASE = "https://www.gate.com/apiw/v2/futures"
SETTLE = "usdt"

GATE_INTEREST_PERCENT = 0.01
GATE_CLAMP_PERCENT = 0.05

# Практический запас.
# 0 = строго по формуле.
# 0.20 = если хочешь целиться глубже, например -2.20% вместо -2.00%.
GATE_LIMIT_EXTRA_PERCENT = 0

# Интервалы (в часах) для которых используется weighted average.
# Для остальных (например 1h) — простое arithmetic mean.
WEIGHTED_INTERVALS_HOURS = {4, 8}


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
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/premium_index"
    params = {
        "contract": contract.upper(),
        "from": from_ts,
        "to": to_ts,
        "interval": interval,
        "limit": 600,
    }
    response = requests.get(url, params=params, headers={"Accept": "application/json"}, timeout=20)
    response.raise_for_status()

    payload = response.json()
    items = payload if isinstance(payload, list) else []

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


def calc_weighted_avg(values, start_index=1):
    """Взвешенное среднее с нарастающим весом (i = start_index, start_index+1, ...)."""
    weighted_sum = 0.0
    weights_sum = 0.0
    for i, v in enumerate(values, start=start_index):
        weighted_sum += i * v
        weights_sum += i
    return weighted_sum / weights_sum if weights_sum else 0.0


def calc_simple_avg(values):
    """Простое арифметическое среднее."""
    return sum(values) / len(values) if values else 0.0


def calc_gate_funding_from_premium(
    avg_premium_percent,
    last_premium_percent,
    interval_hours,
    floor_percent,
    cap_percent,
):
    """
    Gate formula:
    Funding Rate = Average Premium Index
                  + clamp(Interval Interest – Current Premium Index, −0.05%, +0.05%)

    Где Current Premium Index = последнее (текущее) значение, а не среднее.
    """
    interest_component = clamp(
        GATE_INTEREST_PERCENT - last_premium_percent,
        -GATE_CLAMP_PERCENT,
        GATE_CLAMP_PERCENT,
    )
    raw_funding = avg_premium_percent + interest_component
    return clamp(raw_funding, floor_percent, cap_percent)


def get_gate_target_premium_for_limit(
    target_funding_percent,
    interval_hours,
):
    """
    Обратная функция для вычисления целевого premium,
    при котором фандинг достигает лимита.
    """
    scale = 8 / interval_hours
    target_inside = target_funding_percent * scale

    if target_inside < GATE_INTEREST_PERCENT:
        return (
            target_inside
            - GATE_CLAMP_PERCENT
            - GATE_LIMIT_EXTRA_PERCENT
        )

    if target_inside > GATE_INTEREST_PERCENT:
        return (
            target_inside
            + GATE_CLAMP_PERCENT
            + GATE_LIMIT_EXTRA_PERCENT
        )

    return GATE_INTEREST_PERCENT


def get_gate_live_data(contract: str):
    contract = contract.upper()
    contract_info = gate_get_contract_info(contract)
    ticker_info = gate_get_ticker_info(contract)

    funding_interval = int(contract_info.get("funding_interval", 0))
    funding_next_apply = int(float(contract_info.get("funding_next_apply", 0)))

    floor_percent, cap_percent = get_gate_funding_limits(contract_info, ticker_info)

    if funding_interval <= 0 or funding_next_apply <= 0:
        return {"error": "У контракта нет данных по funding cycle"}

    interval_hours = funding_interval / 3600
    use_weighted = interval_hours in WEIGHTED_INTERVALS_HOURS

    now_ts_raw = int(time.time())
    cycle_start_raw = funding_next_apply - funding_interval

    period_seconds = now_ts_raw - cycle_start_raw
    if period_seconds <= 600 * 60:
        interval, step_seconds = "1m", 60
    else:
        interval, step_seconds = "5m", 300

    from_ts = cycle_start_raw - (cycle_start_raw % step_seconds)
    to_ts = now_ts_raw - (now_ts_raw % step_seconds)

    expected_total_points = int(funding_interval // step_seconds)

    items = gate_get_premium_index(contract, from_ts, to_ts, interval=interval)
    if not items:
        return {"error": "Gate не вернул данных за этот период"}

    by_ts = {int(item["t"]): item for item in items}
    expected_timestamps = list(range(from_ts, to_ts, step_seconds))

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
        return {"error": "Нет ни одной точки в выбранном диапазоне"}

    used_points = len(values_percent)
    last_value_percent = values_percent[-1]

    # --- Текущее среднее ---
    if use_weighted:
        current_avg_percent = calc_weighted_avg(values_percent)
    else:
        current_avg_percent = calc_simple_avg(values_percent)

    # --- Проецирование до конца цикла ---
    points_left = expected_total_points - len(expected_timestamps)

    if len(expected_timestamps) >= expected_total_points:
        projected_avg_percent = current_avg_percent
    else:
        if use_weighted:
            # Добираем оставшиеся точки последним значением,
            # продолжая нарастающий вес с того индекса, где остановились
            future_values = [last_value_percent] * (expected_total_points - used_points)
            all_values = values_percent + future_values
            projected_avg_percent = calc_weighted_avg(all_values)
        else:
            # Простое среднее: добавляем константу last для оставшихся точек
            remaining = expected_total_points - used_points
            projected_avg_percent = (
                sum(values_percent) + last_value_percent * remaining
            ) / expected_total_points

    projected_funding_percent = calc_gate_funding_from_premium(
        projected_avg_percent,
        last_value_percent,
        interval_hours,
        floor_percent,
        cap_percent,
    )

    if projected_avg_percent < 0:
        target_funding_percent = floor_percent
    else:
        target_funding_percent = cap_percent

    target_avg_percent = get_gate_target_premium_for_limit(
        target_funding_percent,
        interval_hours,
    )

    if points_left > 0 and use_weighted:
        # Weighted: веса оставшихся точек продолжают нарастать
        current_weighted_sum = sum(
            i * v for i, v in enumerate(values_percent, start=1)
        )
        current_weights_sum = sum(range(1, used_points + 1))

        remaining_start = used_points + 1
        remaining_end = expected_total_points + 1
        remaining_weights_sum = sum(range(remaining_start, remaining_end))
        total_weights_sum = current_weights_sum + remaining_weights_sum

        required_deviation_percent = (
            target_avg_percent * total_weights_sum - current_weighted_sum
        ) / remaining_weights_sum
        req_dev_str = f"{required_deviation_percent:.6f}%"

    elif points_left > 0 and not use_weighted:
        # Simple avg: нужно чтобы итоговое среднее = target_avg
        remaining = expected_total_points - used_points
        required_deviation_percent = (
            target_avg_percent * expected_total_points - sum(values_percent)
        ) / remaining
        req_dev_str = f"{required_deviation_percent:.6f}%"

    else:
        req_dev_str = "0.000000% (Цикл завершен)"

    current_funding = float(contract_info.get("funding_rate", 0)) * 100

    avg_method = "WEIGHTED" if use_weighted else "SIMPLE"

    return {
        "symbol": contract,
        "price_mode": f"INTERVAL {interval} | {avg_method} ({interval_hours:.0f}h)",
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