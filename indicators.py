"""
Базовые технические индикаторы, реализованные так же, как встроенные функции
Pine Script (ta.ema, ta.rma, ta.atr, ta.rsi, ta.dmi), чтобы расчёты совпадали
с тем, что видно на графике TradingView.

Работаем со списками float (без pandas/numpy — чтобы не тащить лишние
зависимости в GitHub Actions).
"""
import math

NAN = float("nan")


def is_nan(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def ema(values, length):
    """Экспоненциальная скользящая, как ta.ema: alpha = 2/(length+1)."""
    alpha = 2.0 / (length + 1)
    out = [NAN] * len(values)
    seed = None
    for i, v in enumerate(values):
        if seed is None:
            seed = v
            out[i] = v
        else:
            seed = alpha * v + (1 - alpha) * seed
            out[i] = seed
    return out


def rma(values, length):
    """Сглаживание Уайлдера, как ta.rma: alpha = 1/length.
    Первое значение — как обычно в Pine — тоже просто затравка первым значением
    (Pine использует SMA за первые length баров, но на длинной истории разница
    исчезающе мала уже через 3-5*length баров, что нас устраивает)."""
    alpha = 1.0 / length
    out = [NAN] * len(values)
    seed = None
    for i, v in enumerate(values):
        if is_nan(v):
            out[i] = NAN
            continue
        if seed is None:
            seed = v
            out[i] = v
        else:
            seed = alpha * v + (1 - alpha) * seed
            out[i] = seed
    return out


def sma(values, length):
    out = [NAN] * len(values)
    for i in range(len(values)):
        if i + 1 < length:
            continue
        window = values[i - length + 1:i + 1]
        out[i] = sum(window) / length
    return out


def stdev(values, length):
    """Скользящее СТАНДАРТНОЕ ОТКЛОНЕНИЕ (популяционное, как ta.stdev в Pine
    по умолчанию) — нужно для полос Боллинджера/ширины полос (BBW)."""
    out = [NAN] * len(values)
    for i in range(len(values)):
        if i + 1 < length:
            continue
        window = values[i - length + 1:i + 1]
        mean = sum(window) / length
        variance = sum((x - mean) ** 2 for x in window) / length
        out[i] = variance ** 0.5
    return out


def percentile_rank(values, idx, lookback):
    """Процентиль значения values[idx] относительно окна values[idx-lookback+1:idx+1]
    (включая сам бар) — аналог ta.percentrank в Pine. Возвращает NAN, если
    истории не хватает."""
    if idx - lookback + 1 < 0:
        return NAN
    window = values[idx - lookback + 1: idx + 1]
    if any(is_nan(v) for v in window):
        return NAN
    target = values[idx]
    count_below_or_equal = sum(1 for v in window if v <= target)
    return 100.0 * count_below_or_equal / len(window)


def true_range(highs, lows, closes):
    out = [NAN] * len(highs)
    for i in range(len(highs)):
        if i == 0:
            out[i] = highs[i] - lows[i]
        else:
            out[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
    return out


def atr(highs, lows, closes, length):
    tr = true_range(highs, lows, closes)
    return rma(tr, length)


def rsi(closes, length):
    n = len(closes)
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains[i] = max(change, 0.0)
        losses[i] = max(-change, 0.0)
    avg_gain = rma(gains, length)
    avg_loss = rma(losses, length)
    out = [NAN] * n
    for i in range(n):
        if is_nan(avg_gain[i]) or is_nan(avg_loss[i]):
            continue
        if avg_loss[i] == 0:
            out[i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def dmi(highs, lows, closes, length):
    """Возвращает (plusDI, minusDI, adx) — как ta.dmi(length, length) в Pine
    (одна и та же длина и для +DI/-DI, и для сглаживания ADX)."""
    n = len(highs)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    tr = true_range(highs, lows, closes)
    tr_smooth = rma(tr, length)
    plus_dm_smooth = rma(plus_dm, length)
    minus_dm_smooth = rma(minus_dm, length)

    plus_di = [NAN] * n
    minus_di = [NAN] * n
    dx = [NAN] * n
    for i in range(n):
        if is_nan(tr_smooth[i]) or tr_smooth[i] == 0:
            continue
        plus_di[i] = 100.0 * plus_dm_smooth[i] / tr_smooth[i]
        minus_di[i] = 100.0 * minus_dm_smooth[i] / tr_smooth[i]
        denom = plus_di[i] + minus_di[i]
        dx[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denom if denom != 0 else 0.0

    adx = rma(dx, length)
    return plus_di, minus_di, adx


def highest(values, length, shift=0):
    """highest(values[shift], length) — как ta.highest(high[1], len), если shift=1."""
    n = len(values)
    out = [NAN] * n
    for i in range(n):
        end = i - shift
        start = end - length + 1
        if start < 0 or end < 0:
            continue
        window = values[start:end + 1]
        out[i] = max(window)
    return out


def lowest(values, length, shift=0):
    n = len(values)
    out = [NAN] * n
    for i in range(n):
        end = i - shift
        start = end - length + 1
        if start < 0 or end < 0:
            continue
        window = values[start:end + 1]
        out[i] = min(window)
    return out


def barssince_excluding_current(cond):
    """Аналог ta.barssince(cond[1]) — считаем от ПРЕДЫДУЩЕГО бара назад,
    текущий бар в расчёт не берём. Возвращает None, если условие ни разу
    не встречалось раньше."""
    n = len(cond)
    out = [None] * n
    last_true = None
    for i in range(n):
        # на бар i смотрим на историю ДО i (включительно i-1)
        out[i] = None if last_true is None else (i - 1 - last_true)
        if cond[i]:
            last_true = i
    return out


def barssince_including_current(cond):
    """Аналог ta.barssince(cond) — 0, если условие истинно на текущем баре."""
    n = len(cond)
    out = [None] * n
    last_true = None
    for i in range(n):
        if cond[i]:
            last_true = i
        out[i] = None if last_true is None else (i - last_true)
    return out
