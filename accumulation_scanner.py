"""
"Радар раннего предупреждения" — ищет монеты, которые СЕЙЧАС ещё не пампятся
и не дампятся, но по совокупности признаков похожи на "накопление перед
движением". Это НЕ то же самое, что pump_scanner.py (тот ловит УЖЕ идущий
всплеск) — это более ранний, более мягкий и более неопределённый сигнал:
"обрати внимание", а не "входи".

Откуда взяты критерии (см. сопровождающее сообщение с источниками):
  - сжатие волатильности (узкий диапазон дольше обычного) часто предшествует
    сильному движению — направление при этом сжатие само по себе НЕ говорит;
  - рост объёма НА ФОНЕ сжатия — признак реального накопления позиций, а не
    просто затишья на низкой ликвидности;
  - funding rate: близкий к нейтральному/слегка отрицательный на фоне сжатия
    считается более "здоровым" (движение органическое, не на чужом плече);
    очень экстремальный funding (в любую сторону) — риск сквиза именно в эту
    сторону при пробое;
  - открытый интерес: растёт на фоне узкого диапазона — трейдеры набирают
    позиции ДО движения, это меняет "вес" будущего пробоя;
  - общий тренд BTC как фильтр — падающий BTC утаскивает даже красивые
    альт-сетапы вниз, поэтому смотрим на него отдельно.

ВАЖНО: то, что реально запоминается pump-and-dump группами как "удобная
цель" — почти всегда монеты С НИЗКОЙ ликвидностью (по исследованиям: медиана
24ч-объёма таких монет часто ПОД 1 млн USDT). Это прямо противоречит нашему
порогу ликвидности в pump_scanner.py (3 млн USDT), который защищает от
проскальзывания/манипуляции. Поэтому здесь сделано ДВА уровня:
  - "торгуемые" (тот же порог 3 млн USDT, что и в pump_scanner) — по ним
    сообщение включает расчёт размера позиции;
  - "только наблюдение" (порог ниже, например 300 тыс. USDT) — по ним НЕТ
    расчёта позиции и добавлено явное предупреждение о повышенном риске
    манипуляции/невозможности нормально выйти — используй их только как
    "посмотреть глазами", а не как готовый к исполнению сигнал.
"""
from indicators import sma, stdev, percentile_rank, atr as calc_atr, ema, is_nan
from okx_client import fetch_candles

ACCUM_PARAMS = dict(
    bar="1H",
    bbwLen=20,                        # период Bollinger для расчёта ширины полос
    bbwStdMult=2.0,
    bbwHistoryBars=200,               # ~8 дней 1ч-баров для процентильного ранжирования текущей ширины
    bbwPercentileMax=25.0,            # текущая ширина полос должна быть ниже этого процентиля своей же истории = "сжатие"
    volTrendLen=8,                    # окно баров для сравнения "текущий объём" vs "предыдущий период"
    volTrendRatioMin=1.15,            # среднее по последним volTrendLen барам должно быть минимум во столько раз больше предыдущего периода
    minLiquidityUsdtTradeable=3_000_000,
    minLiquidityUsdtWatchlist=300_000,
    fundingNeutralAbsMax=0.03,        # |funding rate|% ниже этого — считаем "нейтральным/органичным"
    fundingExtremeAbsMin=0.08,        # |funding rate|% выше этого — считаем "экстремальным" (риск сквиза)
    oiTrendRatioMin=1.10,             # рост своего же накопленного локального ряда OI против oiTrendLookbackScans назад
    oiTrendLookbackScans=6,           # на скольких скан-циклах назад сравнивать OI (при часовом расписании = 6 часов)
    btcInstId="BTC-USDT-SWAP",
    btcTf="4H",
    btcEmaLen=50,
)


def compute_bbw(closes, length, std_mult):
    """Bollinger Band Width относительно базовой линии (SMA) — безразмерная
    величина, удобно сравнивать процентилем внутри одной и той же монеты."""
    basis = sma(closes, length)
    dev = stdev(closes, length)
    n = len(closes)
    bbw = [float("nan")] * n
    for i in range(n):
        if is_nan(basis[i]) or is_nan(dev[i]) or basis[i] == 0:
            continue
        upper = basis[i] + std_mult * dev[i]
        lower = basis[i] - std_mult * dev[i]
        bbw[i] = (upper - lower) / basis[i]
    return bbw


def interpret_funding(funding_rate, params):
    if funding_rate is None:
        return "нет данных"
    pct = funding_rate * 100
    if abs(pct) >= params["fundingExtremeAbsMin"]:
        return f"ЭКСТРЕМАЛЬНЫЙ ({pct:+.3f}%) — риск сквиза в сторону {'вниз (много лонгов)' if pct > 0 else 'вверх (много шортов)'}"
    if abs(pct) <= params["fundingNeutralAbsMax"]:
        return f"нейтральный ({pct:+.3f}%) — органичный баланс лонг/шорт"
    return f"умеренный ({pct:+.3f}%)"


def get_btc_trend(params):
    """True = BTC не в явном нисходящем тренде на старшем ТФ (шлюз для лонг-сетапов)."""
    try:
        candles = fetch_candles(params["btcInstId"], params["btcTf"], total_bars=params["btcEmaLen"] + 20)
        closes = [c["close"] for c in candles]
        ema_val = ema(closes, params["btcEmaLen"])
        return closes[-1] > ema_val[-1]
    except Exception:
        return True  # если не удалось получить — не блокируем, просто не используем фильтр


def analyze_instrument(inst_id, oi_usd_now, oi_history, params=None):
    """
    Считает сжатие волатильности + тренд объёма по свечам. OI и funding
    добавляются отдельно (funding — точечно, только для уже прошедших этот
    базовый фильтр, см. accumulation_monitor.py).

    Возвращает dict с полями compressed, volRising, bbwPercentile, volRatio,
    close, oiTrendRatio (или None) — или None, если не хватило истории.
    """
    p = dict(ACCUM_PARAMS)
    if params:
        p.update(params)

    candles = fetch_candles(inst_id, p["bar"], total_bars=p["bbwHistoryBars"])
    min_needed = p["bbwLen"] + p["bbwHistoryBars"] // 2  # разумный минимум, не требуем 100% истории
    if len(candles) < max(p["bbwLen"] * 2, p["volTrendLen"] * 2, 30):
        return None

    closes = [c["close"] for c in candles]
    volumes = [c["volume"] for c in candles]

    bbw = compute_bbw(closes, p["bbwLen"], p["bbwStdMult"])
    i = len(candles) - 1
    lookback = min(p["bbwHistoryBars"], i + 1)
    bbw_pctl = percentile_rank(bbw, i, lookback)
    if is_nan(bbw_pctl):
        return None

    recent_vol = sum(volumes[i - p["volTrendLen"] + 1: i + 1]) / p["volTrendLen"]
    prior_start = i - 2 * p["volTrendLen"] + 1
    if prior_start < 0:
        return None
    prior_vol = sum(volumes[prior_start: i - p["volTrendLen"] + 1]) / p["volTrendLen"]
    vol_ratio = (recent_vol / prior_vol) if prior_vol else 0

    oi_trend_ratio = None
    if oi_history and len(oi_history) > p["oiTrendLookbackScans"] and oi_usd_now:
        past_oi = oi_history[-(p["oiTrendLookbackScans"] + 1)]
        if past_oi:
            oi_trend_ratio = oi_usd_now / past_oi

    compressed = bbw_pctl <= p["bbwPercentileMax"]
    vol_rising = vol_ratio >= p["volTrendRatioMin"]

    return dict(
        instId=inst_id,
        ts=candles[i]["ts"],
        close=closes[i],
        bbwPercentile=bbw_pctl,
        volRatio=vol_ratio,
        compressed=compressed,
        volRising=vol_rising,
        oiTrendRatio=oi_trend_ratio,
        qualifies=compressed and vol_rising,
    )
