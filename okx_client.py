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


if __name__ == "__main__":
    bars = fetch_candles("ETH-USDT-SWAP", "1H", total_bars=10)
    for b in bars:
        print(b)
