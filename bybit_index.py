import time
import httpx
import asyncio

BYBIT_BASE = "https://api.bybit.com"

# Константы официальной спецификации Bybit
INTEREST_RATE_DAILY = 0.03  # Базовая дневная процентная ставка 0.03%


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


async def get_bybit_live_data(symbol: str):
    # 1. Форматирование тикера под стандарт линейных контрактов Bybit (USDT/USDC)
    s = symbol.upper().replace("/", "").replace("-", "").replace(" ", "")
    s = s.replace("_PERP", "").replace("PERP", "")
    
    if not s.endswith("USDT") and not s.endswith("USDC"):
        symbol_clean = f"{s}USDT"
    else:
        symbol_clean = s

    async with httpx.AsyncClient() as client:
        try:
            # Запрашиваем текущий тикер (текущий фандинг, время следующего апдейта)
            ticker_task = bybit_get(
                client,
                "/v5/market/tickers",
                {"category": "linear", "symbol": symbol_clean},
            )
            # Запрашиваем параметры контракта (интервал фандинга)
            instr_task = bybit_get(
                client,
                "/v5/market/instruments-info",
                {"category": "linear", "symbol": symbol_clean},
            )

            ticker_res, instr_res = await asyncio.gather(ticker_task, instr_task)

            ticker_list = ticker_res.get("list", [])
            instr_list = instr_res.get("list", [])

            if not ticker_list or not instr_list:
                return {"error": f"Символ {symbol_clean} не найден в листинге Bybit"}

            ticker_data = ticker_list[0]
            instr_data = instr_list[0]

            # Извлекаем метаданные фандинга
            current_funding_percent = float(ticker_data.get("fundingRate", 0)) * 100
            next_funding_ts = int(int(ticker_data.get("nextFundingTime", time.time() * 1000)) // 1000)

            funding_interval_minutes = int(instr_data.get("fundingInterval", 480))
            interval_hours = int(funding_interval_minutes // 60)

            # Исторические лимиты для большинства линейных контрактов Bybit
            floor_percent = -2.0
            cap_percent = 2.0

            # Синхронизация временной шкалы (шаг 60 секунд)
            now_ts = int(time.time())
            cycle_start_ts = next_funding_ts - (interval_hours * 3600)

            start_ms = cycle_start_ts * 1000
            end_ms = now_ts * 1000
            expected_total_points = interval_hours * 60

            # Запрашиваем готовые минутные свечи Премиум-Индекса
            premium_res = await bybit_get(
                client,
                "/v5/market/premium-index-price-kline",
                {
                    "category": "linear",
                    "symbol": symbol_clean,
                    "interval": "1",
                    "start": start_ms,
                    "end": end_ms,
                    "limit": 1000,
                },
            )

            candles = premium_res.get("list", [])

        except Exception as api_err:
            return {"error": f"Bybit API Error: {str(api_err)}"}

    # 2. Математический расчёт Time-Weighted коэффициентов по формуле Bybit
    # Bybit отдаёт свечи от новых к старым (descending), разворачиваем для хронологического порядка
    candles.reverse()

    weighted_premium_sum = 0.0
    weight_denominator = 0
    chart_points = []
    
    last_premium_percent = 0.0
    minute_index = 1

    for c in candles:
        try:
            ts = int(int(c[0]) // 1000)
            # Извлекаем Close Price премиума и переводим коэффициент в проценты (* 100)
            premium_percent = float(c[4]) * 100  
            last_premium_percent = premium_percent

            # Вес текущей минуты равен её порядковому номеру в цикле (1, 2, 3 ... 480)
            weight = minute_index
            
            weighted_premium_sum += premium_percent * weight
            weight_denominator += weight

            chart_points.append({"time": ts, "value": premium_percent})
            minute_index += 1
        except Exception:
            continue

    if weight_denominator == 0:
        return {"error": "Bybit не вернул валидные исторические точки премиум-индекса"}

    # ТЕКУЩЕЕ ВЗВЕШЕННОЕ СРЕДНЕЕ (P)
    current_avg_percent = weighted_premium_sum / weight_denominator
    used_points = len(chart_points)

    # ПРОГНОЗ ДО КОНЦА ЦИКЛА (заполняем веса оставшихся минут последней точкой)
    if used_points >= expected_total_points:
        projected_avg_percent = current_avg_percent
    else:
        proj_weighted_sum = weighted_premium_sum
        proj_weight_denominator = weight_denominator

        for idx in range(minute_index, expected_total_points + 1):
            weight = idx
            proj_weighted_sum += last_premium_percent * weight
            proj_weight_denominator += weight

        projected_avg_percent = proj_weighted_sum / proj_weight_denominator

    # Расчёт финальной прогнозируемой ставки фандинга (F) по формуле Bybit
    # Процентная ставка на текущий интервал: I = 0.03% / (24 / интервал)
    interval_interest = INTEREST_RATE_DAILY / (24 / interval_hours)
    
    # F = P + clamp(I - P, 0.05%, -0.05%)
    clamp_premium = clamp(interval_interest - projected_avg_percent, -0.05, 0.05)
    projected_funding_percent = clamp(projected_avg_percent + clamp_premium, floor_percent, cap_percent)

    # Вычисление необходимого отклонения до конца текущей эпохи фандинга
    points_left = expected_total_points - used_points
    if points_left > 0:
        target_inside = floor_percent if projected_avg_percent < 0 else cap_percent
        
        # Сумма весов, которая осталась впереди
        remaining_weight_sum = sum(idx for idx in range(minute_index, expected_total_points + 1))
        total_target_denominator = weight_denominator + remaining_weight_sum
        
        # Какое среднее отклонение премиума нужно удерживать в оставшихся весах, чтобы выйти на цель
        required_deviation_percent = (
            (target_inside * total_target_denominator) - weighted_premium_sum
        ) / remaining_weight_sum
        req_dev_str = f"{required_deviation_percent:.6f}%"
    else:
        req_dev_str = "0.000000% (Цикл завершен)"

    return {
        "symbol": s,  # Красивое имя для вывода на фронтенд (например, BTC)
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