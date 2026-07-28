"""
Сканер пампов/дампов по ВСЕМ USDT-перпетуалам OKX.

Это ДРУГАЯ по духу система, чем "Свинг-тренд v2": там мы долго и методично
бэктестили каждый параметр на истории. Здесь бэктест невозможен по конструкции
задачи — список "интересных" монет каждый раз разный, среди них часто вообще
новые листинги без длинной истории. Поэтому все пороги ниже — это разумные
эвристики (резкий объём + резкое движение цены), не подтверждённый Strategy
Tester'ом рецепт. Относись к сигналам этого сканера как к "второе мнение,
требует твоей проверки", а не как к гарантированному edge.

Схема работы:

  Этап 1 (дёшево, 1 запрос): забираем тикеры ВСЕХ USDT-перпетуалов разом,
  отсекаем совсем неликвидные (риск манипуляции/невозможности выйти из
  позиции) — это единственный фильтр состава. По умолчанию НИКАКОГО топ-N
  ограничения нет: анализируем КАЖДУЮ прошедшую фильтр монету на каждом
  скане (публичный лимит запросов OKX это позволяет — см. maxWorkers ниже),
  так что новый листинг или монета, не попавшая в топ по 24ч-движению (но
  реально пампящаяся прямо сейчас), не потеряются. Сортировка по модулю
  24ч-движения оставлена только для порядка проверки/логов, не для отсечения.

  Этап 2 (по всем инструментам после фильтра ликвидности, параллельно в
  несколько потоков): по каждому забираем последние 15-минутные свечи и
  проверяем два условия одновременно:
    - объём последней свечи заметно выше среднего (реальный, а не мнимый интерес)
    - цена сдвинулась на заметный % за последние несколько свечей (не разовый
      фитиль, а начавшееся движение)
  Если оба условия — это и есть сигнал "памп" (лонг) или "дамп" (шорт).

  Отдельно: уже открытые (просигналенные ранее) виртуальные сделки на каждом
  скане обновляют трейлинг-стоп и проверяются на выход — независимо от того,
  остался ли инструмент в топе кандидатов на этом скане.
"""
from indicators import atr as calc_atr, sma
from okx_client import fetch_candles

PUMP_PARAMS = dict(
    bar="15m",
    minLiquidityUsdt24h=3_000_000,   # отсекаем совсем неликвидные пары (риск манипуляции/невозможности выйти)
    maxCandidates=None,              # None = анализировать ВСЕ монеты после фильтра ликвидности (без обрезки топ-N)
    maxWorkers=4,                    # сколько монет анализировать параллельно (публичный rate-limit OKX, не завышай сильно)
    volLen=20,                       # окно среднего объёма (в барах 15м = 5 часов)
    volSpikeMult=3.0,                # объём последней свечи должен быть в X раз выше среднего
    momentumLookback=3,              # смотрим изменение цены за последние N баров (3*15м = 45 мин)
    momentumThresholdPct=5.0,        # % движения за momentumLookback баров, чтобы считать это пампом/дампом
    atrLen=14,
    stopAtrMult=1.5,                 # стоп ЗАМЕТНО уже, чем в свинг-тренде — мы и так уже "опаздываем" к движению
    trailAtrMult=2.0,                # трейлинг тоже туже — фиксируем прибыль быстрее, пампы разворачиваются резко
    cooldownBars=8,                  # не сигналить повторно по той же монете/направлению N баров после выхода
)


def shortlist_candidates(tickers, params=None):
    """Этап 1: из тикеров всех перпетуалов отбирает кандидатов на памп/дамп —
    единственный фильтр здесь ликвидность (защита от неликвида), а не топ-N
    по 24ч-движению. maxCandidates оставлен как аварийный клапан (например,
    если список инструментов внезапно сильно вырастет и понадобится вручную
    ограничить время скана), по умолчанию не обрезает ничего."""
    p = dict(PUMP_PARAMS)
    if params:
        p.update(params)

    liquid = [
        t for t in tickers
        if t["instId"].endswith("-USDT-SWAP") and t["volCcy24h"] >= p["minLiquidityUsdt24h"] and t["open24h"] > 0
    ]
    for t in liquid:
        t["change24hPct"] = (t["last"] - t["open24h"]) / t["open24h"] * 100.0

    liquid.sort(key=lambda t: abs(t["change24hPct"]), reverse=True)
    if p.get("maxCandidates"):
        liquid = liquid[: p["maxCandidates"]]
    return liquid


def analyze_instrument(inst_id, params=None, total_bars=50):
    """
    Этап 2: забирает свечи кандидата и проверяет условия пампа/дампа на
    ПОСЛЕДНЕЙ закрытой свече. Возвращает dict с результатом или None, если
    не хватило истории (например, монета совсем свежая).
    """
    p = dict(PUMP_PARAMS)
    if params:
        p.update(params)

    candles = fetch_candles(inst_id, p["bar"], total_bars=total_bars)
    if len(candles) < max(p["volLen"], p["atrLen"]) + p["momentumLookback"] + 2:
        return None

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    volumes = [c["volume"] for c in candles]

    atr_val = calc_atr(highs, lows, closes, p["atrLen"])
    vol_avg = sma(volumes, p["volLen"])

    i = len(candles) - 1
    if atr_val[i] != atr_val[i] or vol_avg[i - 1] != vol_avg[i - 1]:  # NaN check без math.isnan импорта
        return None

    av = atr_val[i]
    last_close = closes[i]
    last_vol = volumes[i]
    # среднее считаем ДО текущего бара, чтобы сам всплеск не размывал свою же базу сравнения
    avg_vol_prev = vol_avg[i - 1]
    vol_ratio = (last_vol / avg_vol_prev) if avg_vol_prev else 0

    look = p["momentumLookback"]
    ref_close = closes[i - look]
    momentum_pct = (last_close - ref_close) / ref_close * 100.0 if ref_close else 0

    vol_spike = vol_ratio >= p["volSpikeMult"]
    pump_signal = vol_spike and momentum_pct >= p["momentumThresholdPct"]
    dump_signal = vol_spike and momentum_pct <= -p["momentumThresholdPct"]

    stop_dist = av * p["stopAtrMult"]
    result = dict(
        instId=inst_id,
        ts=candles[i]["ts"],
        close=last_close,
        atr=av,
        volRatio=vol_ratio,
        momentumPct=momentum_pct,
        pumpSignal=pump_signal,
        dumpSignal=dump_signal,
    )
    if pump_signal:
        result["direction"] = "long"
        result["stop"] = last_close - stop_dist
    elif dump_signal:
        result["direction"] = "short"
        result["stop"] = last_close + stop_dist
    return result


def update_open_position(position, params=None, total_bars=60):
    """
    Обновляет трейлинг-стоп уже открытой (просигналенной ранее) виртуальной
    сделки по свежим свечам и проверяет, не пробит ли стоп. position — dict
    с ключами instId, direction, entryPrice, activeStop, extreme, entryTs.
    Возвращает (updated_position, exit_hit: bool, last_close: float или None).
    """
    p = dict(PUMP_PARAMS)
    if params:
        p.update(params)

    candles = fetch_candles(position["instId"], p["bar"], total_bars=total_bars)
    if not candles:
        return position, False, None

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    atr_val = calc_atr(highs, lows, closes, p["atrLen"])

    # обновляем только по свечам НОВЕЕ момента входа
    entry_ts = position["entryTs"]
    new_bars = [(idx, c) for idx, c in enumerate(candles) if c["ts"] > entry_ts]

    exit_hit = False
    last_close = closes[-1]
    for idx, c in new_bars:
        av = atr_val[idx]
        if av != av:  # NaN
            continue
        if position["direction"] == "long":
            position["extreme"] = max(position["extreme"], c["high"])
            position["activeStop"] = max(position["activeStop"], position["extreme"] - av * p["trailAtrMult"])
            if c["close"] < position["activeStop"]:
                exit_hit = True
                last_close = c["close"]
                break
        else:
            position["extreme"] = min(position["extreme"], c["low"])
            position["activeStop"] = min(position["activeStop"], position["extreme"] + av * p["trailAtrMult"])
            if c["close"] > position["activeStop"]:
                exit_hit = True
                last_close = c["close"]
                break

    return position, exit_hit, last_close
