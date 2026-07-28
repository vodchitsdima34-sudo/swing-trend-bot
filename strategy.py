"""
Точная копия логики индикатора "Свинг-тренд v2" (swing_trend_start.pine) на
Python, для расчёта сигналов вне TradingView.

Подтверждённый рабочий рецепт (см. дефолты в самом .pine файле):
  rangeLen=15, rangeAtrMult=1.0, breakoutOnWick=True
  emaTrendLen=50, adxLen=14, adxMin=20, requireAdxRising=False
  volLen=20, volMult=0.8, minBodyAtrMult=0.35
  rsiLen=14, maxRsiEntry=92
  useContinuationEntry=True, contEmaBufferAtr=0.5, contLookback=3
  atrLen=14, stopAtrMult=2.5, useTrailingStop=True, trailAtrMult=4.0
  exitOnOpposite=True, minHoldBars=4, cooldownBars=3
  climaxAtrMult=3.0, climaxLookback=4
  HTF-фильтр: оба "игнор" включены -> фильтр фактически отключён,
  поэтому здесь для простоты не запрашиваем данные 4ч вообще.
  Направление: Long и Short (allowShort = True).
"""
from indicators import (
    ema, rma, sma, atr, rsi, dmi, highest, lowest,
    barssince_excluding_current, barssince_including_current, is_nan,
)

# Подтверждённый рецепт для ETH (см. дефолты в swing_trend_start.pine)
ETH_PARAMS = dict(
    rangeLen=15,
    rangeAtrMult=1.0,
    breakoutOnWick=True,
    emaTrendLen=50,
    adxLen=14,
    adxMin=20.0,
    requireAdxRising=False,
    volLen=20,
    volMult=0.8,
    minBodyAtrMult=0.35,
    rsiLen=14,
    maxRsiEntry=92.0,
    useContinuationEntry=True,
    contEmaBufferAtr=0.5,
    contLookback=3,
    atrLen=14,
    stopAtrMult=2.5,
    rrTarget=2.0,
    useTrailingStop=True,
    trailAtrMult=4.0,
    exitOnOpposite=True,
    minHoldBars=4,
    cooldownBars=3,
    climaxAtrMult=3.0,
    climaxLookback=4,
    allowShort=True,
)

# Подтверждённый рецепт для BTC (см. дефолты в swing_trend_BTC.pine) —
# отдельно оттюненный на BTCUSDT.P: ADX мягче, объём строже, окно "догона"
# шире, стоп уже, чем у ETH.
BTC_PARAMS = dict(ETH_PARAMS)
BTC_PARAMS.update(
    adxMin=10.0,
    volMult=1.0,
    contLookback=15,
    stopAtrMult=2.0,
)

# Обратная совместимость: старое имя PARAMS = рецепт ETH (используется по
# умолчанию, если compute() вызван без явных params).
PARAMS = ETH_PARAMS


def compute(candles, params=None):
    """
    candles: список dict с ключами ts, open, high, low, close, volume,
             отсортированный по времени (старые -> новые).
    Возвращает список dict — по одной записи на бар — с сигналами и
    диагностикой (то же самое, что таблица диагностики в индикаторе).
    """
    p = dict(PARAMS)
    if params:
        p.update(params)

    n = len(candles)
    opens = [c["open"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    atr_val = atr(highs, lows, closes, p["atrLen"])
    ema_trend = ema(closes, p["emaTrendLen"])
    plus_di, minus_di, adx_val = dmi(highs, lows, closes, p["adxLen"])
    vol_avg = sma(volumes, p["volLen"])
    rsi_val = rsi(closes, p["rsiLen"])

    range_high = highest(highs, p["rangeLen"], shift=1)
    range_low = lowest(lows, p["rangeLen"], shift=1)

    bar_body = [abs(closes[i] - opens[i]) for i in range(n)]
    bar_range = [highs[i] - lows[i] for i in range(n)]

    is_climax = [False] * n
    for i in range(n):
        if is_nan(atr_val[i]):
            continue
        is_climax[i] = bar_range[i] > atr_val[i] * p["climaxAtrMult"]
    bs_climax = barssince_excluding_current(is_climax)

    # догон: недавний откат к EMA тренда
    pulled_back_long_cond = [False] * n
    pulled_back_short_cond = [False] * n
    for i in range(n):
        if is_nan(ema_trend[i]) or is_nan(atr_val[i]):
            continue
        pulled_back_long_cond[i] = lows[i] <= ema_trend[i] + atr_val[i] * p["contEmaBufferAtr"]
        pulled_back_short_cond[i] = highs[i] >= ema_trend[i] - atr_val[i] * p["contEmaBufferAtr"]
    bs_pulled_long = barssince_including_current(pulled_back_long_cond)
    bs_pulled_short = barssince_including_current(pulled_back_short_cond)

    records = [None] * n

    # состояние (как var-переменные в Pine)
    state = 0
    active_stop = None
    active_target = None
    extreme_since_entry = None
    entry_bar = None
    last_stop_exit_bar = None

    for i in range(n):
        rec = {"ts": candles[i]["ts"], "close": closes[i]}
        records[i] = rec

        if i == 0 or is_nan(atr_val[i]) or is_nan(ema_trend[i]) or is_nan(adx_val[i]) \
                or is_nan(vol_avg[i]) or is_nan(rsi_val[i]) or is_nan(range_high[i]) or is_nan(range_low[i]):
            rec.update(dict(longSignal=False, shortSignal=False,
                             exitLongSignal=False, exitShortSignal=False,
                             state=state, warmup=True))
            continue

        av = atr_val[i]
        et = ema_trend[i]
        adxv = adx_val[i]

        trend_ok_long = closes[i] > et
        trend_ok_short = closes[i] < et

        trend_strong = adxv > p["adxMin"]
        adx_rising = True if not p["requireAdxRising"] else (i > 0 and not is_nan(adx_val[i - 1]) and adxv > adx_val[i - 1])

        vol_ok = volumes[i] > vol_avg[i] * p["volMult"]

        rsi_ok_long = rsi_val[i] < p["maxRsiEntry"]
        rsi_ok_short = rsi_val[i] > (100 - p["maxRsiEntry"])

        range_width = range_high[i] - range_low[i]
        is_tight_range = range_width < av * p["rangeAtrMult"]

        prev_ok = i > 0 and not is_nan(range_high[i - 1]) and not is_nan(range_low[i - 1])
        if p["breakoutOnWick"]:
            breakout_long = prev_ok and highs[i] > range_high[i] and highs[i - 1] <= range_high[i - 1]
            breakout_short = prev_ok and lows[i] < range_low[i] and lows[i - 1] >= range_low[i - 1]
        else:
            breakout_long = prev_ok and closes[i] > range_high[i] and closes[i - 1] <= range_high[i - 1]
            breakout_short = prev_ok and closes[i] < range_low[i] and closes[i - 1] >= range_low[i - 1]

        strong_body_long = closes[i] > opens[i] and bar_body[i] > av * p["minBodyAtrMult"]
        strong_body_short = closes[i] < opens[i] and bar_body[i] > av * p["minBodyAtrMult"]

        no_recent_climax = bs_climax[i] is None or bs_climax[i] >= p["climaxLookback"]

        raw_long_setup = trend_ok_long and is_tight_range and breakout_long and trend_strong and adx_rising and vol_ok and rsi_ok_long and strong_body_long
        raw_short_setup = p["allowShort"] and trend_ok_short and is_tight_range and breakout_short and trend_strong and adx_rising and vol_ok and rsi_ok_short and strong_body_short

        pulled_back_long = bs_pulled_long[i] is not None and bs_pulled_long[i] <= p["contLookback"]
        pulled_back_short = bs_pulled_short[i] is not None and bs_pulled_short[i] <= p["contLookback"]

        cont_long_setup = p["useContinuationEntry"] and trend_ok_long and trend_strong and adx_rising and vol_ok and rsi_ok_long and strong_body_long and pulled_back_long and closes[i] > et
        cont_short_setup = p["useContinuationEntry"] and p["allowShort"] and trend_ok_short and trend_strong and adx_rising and vol_ok and rsi_ok_short and strong_body_short and pulled_back_short and closes[i] < et

        long_stop_candidate = min(closes[i] - av * p["stopAtrMult"], range_low[i] - av * 0.3)
        long_tgt_candidate = closes[i] + av * p["stopAtrMult"] * p["rrTarget"]
        short_stop_candidate = max(closes[i] + av * p["stopAtrMult"], range_high[i] + av * 0.3)
        short_tgt_candidate = closes[i] - av * p["stopAtrMult"] * p["rrTarget"]

        # ----- выходы -----
        exit_long_by_stop = state == 1 and active_stop is not None and closes[i] < active_stop
        exit_short_by_stop = state == -1 and active_stop is not None and closes[i] > active_stop

        hold_ok = entry_bar is None or (i - entry_bar) >= p["minHoldBars"]
        exit_long_by_opposite = p["exitOnOpposite"] and state == 1 and raw_short_setup and hold_ok
        exit_short_by_opposite = p["exitOnOpposite"] and state == -1 and raw_long_setup and hold_ok

        exit_long_signal = exit_long_by_stop or exit_long_by_opposite
        exit_short_signal = exit_short_by_stop or exit_short_by_opposite

        exit_reason = None
        if exit_long_signal:
            exit_reason = "стоп" if exit_long_by_stop else "встречный сигнал (флип в Short)"
            state = 0
        if exit_short_signal:
            exit_reason = "стоп" if exit_short_by_stop else "встречный сигнал (флип в Long)"
            state = 0

        if exit_long_by_stop or exit_short_by_stop:
            last_stop_exit_bar = i

        cooldown_ok = last_stop_exit_bar is None or (i - last_stop_exit_bar > p["cooldownBars"])

        long_signal = (raw_long_setup or cont_long_setup) and state == 0 and cooldown_ok and no_recent_climax
        short_signal = (raw_short_setup or cont_short_setup) and state == 0 and cooldown_ok and no_recent_climax

        entry_kind = None
        if long_signal:
            entry_kind = "догон" if not raw_long_setup else "пробой"
            state = 1
            entry_bar = i
            extreme_since_entry = closes[i]
            active_target = long_tgt_candidate
            active_stop = long_stop_candidate
        if short_signal:
            entry_kind = "догон" if not raw_short_setup else "пробой"
            state = -1
            entry_bar = i
            extreme_since_entry = closes[i]
            active_target = short_tgt_candidate
            active_stop = short_stop_candidate

        if state == 1 and p["useTrailingStop"]:
            extreme_since_entry = max(extreme_since_entry, highs[i])
            active_stop = max(active_stop, extreme_since_entry - av * p["trailAtrMult"])
        if state == -1 and p["useTrailingStop"]:
            extreme_since_entry = min(extreme_since_entry, lows[i])
            active_stop = min(active_stop, extreme_since_entry + av * p["trailAtrMult"])

        rec.update(dict(
            warmup=False,
            state=state,
            longSignal=long_signal,
            shortSignal=short_signal,
            entryKind=entry_kind,
            exitLongSignal=exit_long_signal,
            exitShortSignal=exit_short_signal,
            exitReason=exit_reason,
            activeStop=active_stop,
            activeTarget=active_target,
            stopAtrMult=p["stopAtrMult"],
            rrTarget=p["rrTarget"],
            adx=adxv,
            rsi=rsi_val[i],
            volRatio=(volumes[i] / vol_avg[i] if vol_avg[i] else None),
            emaTrend=et,
            atr=av,
            isTightRange=is_tight_range,
            rawLongSetup=raw_long_setup,
            rawShortSetup=raw_short_setup,
            contLongSetup=cont_long_setup,
            contShortSetup=cont_short_setup,
        ))

    return records
