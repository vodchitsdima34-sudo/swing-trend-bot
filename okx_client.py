"""
Получение исторических свечей с публичного API биржи OKX.
Не требует ключей/подписи — это публичные рыночные данные.

Документация: https://www.okx.com/docs-v5/en/#public-data-rest-api-get-candlesticks-history
"""
import time
import urllib.request
import json

OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"
OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
OKX_TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
OKX_OPEN_INTEREST_URL = "https://www.okx.com/api/v5/public/open-interest"
OKX_FUNDING_RATE_URL = "https://www.okx.com/api/v5/public/funding-rate"


def _http_get_json(url, params, timeout=15):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    full_url = f"{url}?{query}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != "0":
        raise RuntimeError(f"OKX API error: {data}")
    return data["data"]


def fetch_candles(inst_id: str, bar: str, total_bars: int = 800, pause_sec: float = 0.25):
    """
    Возвращает список свечей в ХРОНОЛОГИЧЕСКОМ порядке (от старых к новым),
    только ПОДТВЕРЖДЁННЫЕ (закрытые) свечи.

    Каждая свеча — dict: ts (int, ms), open, high, low, close, volume (float).

    inst_id: например "ETH-USDT-SWAP"
    bar: "1H", "4H", "1D" и т.д. (формат OKX)
    """
    collected = []
    after = None
    # OKX отдаёт максимум 100 свечей за один вызов history-candles.
    while len(collected) < total_bars:
        params = {"instId": inst_id, "bar": bar, "limit": "100"}
        if after is not None:
            params["after"] = str(after)
        raw = _http_get_json(OKX_HISTORY_URL, params)
        if not raw:
            break
        collected.extend(raw)
        # raw[-1] — самая старая свеча в этой пачке; берём её ts как границу для следующего запроса
        oldest_ts = int(raw[-1][0])
        after = oldest_ts
        if len(raw) < 100:
            break
        time.sleep(pause_sec)

    # Формат каждой свечи OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    candles = []
    for row in collected:
        ts, o, h, l, c, vol = row[0], row[1], row[2], row[3], row[4], row[5]
        confirm = row[8] if len(row) > 8 else "1"
        if confirm != "1":
            continue  # пропускаем ещё не закрытую (текущую формирующуюся) свечу
        candles.append({
            "ts": int(ts),
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": float(vol),
        })

    # убираем дубликаты по ts и сортируем по времени (старые -> новые)
    dedup = {c["ts"]: c for c in candles}
    result = sorted(dedup.values(), key=lambda c: c["ts"])
    if total_bars and len(result) > total_bars:
        result = result[-total_bars:]
    return result


def fetch_tickers(inst_type: str = "SWAP"):
    """
    Один запрос — тикеры ВСЕХ инструментов заданного типа разом (для SWAP это
    все бессрочные перпетуалы OKX). Используется как быстрый первый проход
    сканера пампов/дампов — не нужно дёргать свечи по каждой монете, чтобы
    понять, где вообще есть аномальное движение за последние 24ч.

    Возвращает список dict: instId, last, open24h, high24h, low24h,
    vol24h (в базовой монете), volCcy24h (в валюте котировки, обычно USDT).
    """
    raw = _http_get_json(OKX_TICKERS_URL, {"instType": inst_type})
    tickers = []
    for row in raw:
        try:
            tickers.append({
                "instId": row["instId"],
                "last": float(row["last"]),
                "open24h": float(row["open24h"]),
                "high24h": float(row["high24h"]),
                "low24h": float(row["low24h"]),
                "vol24h": float(row["vol24h"]),
                "volCcy24h": float(row["volCcy24h"]),
            })
        except (KeyError, ValueError, TypeError):
            continue  # пропускаем инструменты с неполными/битыми данными
    return tickers


def fetch_open_interest(inst_type: str = "SWAP"):
    """
    Один запрос — открытый интерес ВСЕХ инструментов заданного типа разом
    (как fetch_tickers, но для открытого интереса). Возвращает dict
    instId -> oiUsd (открытый интерес в долларах). Если для какого-то
    инструмента поле не пришло/битое — просто пропускаем его.

    Официальный путь эндпоинта подтверждён по стороннему обзору API (не
    протестирован живьём из-за сетевых ограничений текущей среды) —
    структуру ответа стоит перепроверить по первому реальному логу запуска.
    """
    raw = _http_get_json(OKX_OPEN_INTEREST_URL, {"instType": inst_type})
    out = {}
    for row in raw:
        try:
            inst_id = row["instId"]
            oi_usd = float(row.get("oiUsd") or row.get("oiCcy") or 0)
            out[inst_id] = oi_usd
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_funding_rate(inst_id: str):
    """
    Текущая ставка финансирования (funding rate) по ОДНОМУ инструменту —
    в отличие от тикеров/OI, этот эндпоинт не отдаёт список сразу по всем
    инструментам, поэтому дёргаем точечно, только для уже отфильтрованных
    кандидатов (иначе слишком много запросов на весь список монет).

    Возвращает float (например 0.0001 = 0.01% за период) или None, если
    не удалось получить/распарсить.

    Не протестировано против живого API из-за сетевых ограничений текущей
    среды — если структура ответа окажется другой, эта функция просто
    вернёт None (обрабатывается вызывающим кодом как "нет данных"), а не
    уронит скрипт.
    """
    try:
        raw = _http_get_json(OKX_FUNDING_RATE_URL, {"instId": inst_id})
        if not raw:
            return None
        return float(raw[0]["fundingRate"])
    except Exception:
        return None


if __name__ == "__main__":
    bars = fetch_candles("ETH-USDT-SWAP", "1H", total_bars=10)
    for b in bars:
        print(b)
