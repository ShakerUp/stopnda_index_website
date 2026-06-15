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
    response.raise_for_status()
    return response.json()


def gate_get_contract_info(contract):
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/contracts/{contract.upper()}"
    return gate_request(url)


def gate_get_ticker_info(contract):
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/tickers"
    res = gate_request(url, {"contract": contract.upper()})
    return res[0] if isinstance(res, list) and len(res) > 0 else {}


def gate_get_mark_price_candles(contract, from_ts, to_ts, interval="1m"):
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/mark_price_candlesticks"
    return gate_request(
        url,
        {
            "contract": contract.upper(),
            "from": from_ts,
            "to": to_ts,
            "interval": interval,
            "limit": 1000,
        },
    )


def gate_get_index_price_candles(contract, from_ts, to_ts, interval="1m"):
    url = f"https://api.gateio.ws/api/v4/futures/{SETTLE}/index_price_candlesticks"
    return gate_request(
        url,
        {
            "contract": contract.upper(),
            "from": from_ts,
            "to": to_ts,
            "interval": interval,
            "limit": 1000,
        },
    )


def gate_get_premium_from_mark_index(contract, from_ts, to_ts, interval="1m"):
    mark_items = gate_get_mark_price_candles(contract, from_ts, to_ts, interval)
    index_items = gate_get_index_price_candles(contract, from_ts, to_ts, interval)

    mark_by_ts = {
        int(item["t"]): item
        for item in mark_items
        if "t" in item and "c" in item
    }

    index_by_ts = {
        int(item["t"]): item
        for item in index_items
        if "t" in item and "c" in item
    }

    result = []

    for ts in sorted(set(mark_by_ts.keys()) & set(index_by_ts.keys())):
        mark_price = float(mark_by_ts[ts]["c"])
        index_price = float(index_by_ts[ts]["c"])

        if index_price <= 0:
            continue

        premium_percent = (mark_price - index_price) / index_price * 100

        result.append({
            "t": ts,
            "c": premium_percent,
            "mark": mark_price,
            "index": index_price,
        })

    return result


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


def calc_weighted_avg(values):
    weighted_sum = 0.0
    weights_sum = 0.0

    for i, value in enumerate(values, start=1):
        weighted_sum += i * value
        weights_sum += i

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

    now_ts_raw = int(time.time())
    cycle_start_raw = funding_next_apply - funding_interval

    interval = "1m"
    step_seconds = 60

    from_ts = cycle_start_raw - (cycle_start_raw % step_seconds)
    to_ts = now_ts_raw - (now_ts_raw % step_seconds)

    expected_total_points = int(funding_interval // step_seconds)

    items = gate_get_premium_from_mark_index(
        contract=contract,
        from_ts=from_ts,
        to_ts=to_ts,
        interval=interval,
    )

    if not items:
        return {"error": "Gate не вернул mark/index данные за этот период"}

    by_ts = {int(item["t"]): item for item in items}
    expected_timestamps = list(range(from_ts, to_ts, step_seconds))

    values_percent = []
    chart_points = []

    for ts in expected_timestamps:
        item = by_ts.get(ts)

        if item is None:
            continue

        value_percent = float(item["c"])

        values_percent.append(value_percent)
        chart_points.append({
            "time": ts,
            "value": value_percent,
            "mark": item.get("mark"),
            "index": item.get("index"),
        })

    if not values_percent:
        return {"error": "Нет ни одной premium-точки в выбранном диапазоне"}

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
        avg_premium_percent=projected_avg_percent,
        interval_hours=interval_hours,
        floor_percent=floor_percent,
        cap_percent=cap_percent,
    )

    target_funding_percent = floor_percent if projected_avg_percent < 0 else cap_percent

    target_avg_percent = get_gate_target_premium_for_limit(
        target_funding_percent=target_funding_percent,
        interval_hours=interval_hours,
    )

    if points_left > 0:
        current_weighted_sum = sum(
            i * value for i, value in enumerate(values_percent, start=1)
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
    else:
        required_deviation_percent = 0.0
        req_dev_str = "0.000000% (Цикл завершен)"

    current_funding = float(contract_info.get("funding_rate", 0)) * 100

    return {
        "symbol": contract,
        "price_mode": f"MARK/INDEX | INTERVAL {interval} | WEIGHTED ({interval_hours:.0f}h)",
        "current_avg": round(current_avg_percent, 6),
        "projected_avg": round(projected_avg_percent, 6),
        "current_funding": round(current_funding, 6),
        "projected_funding": round(projected_funding_percent, 6),
        "required_deviation": req_dev_str,
        "required_deviation_raw": round(required_deviation_percent, 6),
        "limits": f"{floor_percent:.4f}% / +{cap_percent:.4f}%",
        "time_left": max(0, funding_next_apply - now_ts_raw),
        "points_total": f"{used_points}/{expected_total_points}",
        "last_premium": round(last_value_percent, 6),
        "chart_data": chart_points,
    }