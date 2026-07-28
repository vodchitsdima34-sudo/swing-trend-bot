"""
Главный скрипт "радара накопления" — запускается РАЗ В ЧАС (медленнее, чем
pump_monitor.py, потому что здесь на каждую монету уходит больше запросов:
свечи для расчёта сжатия волатильности + funding rate у прошедших фильтр
кандидатов). Открытый интерес по ВСЕМ монетам забирается одним запросом.

Каждый запуск:
  1. Тикеры всех USDT-перпетуалов (для списка + ликвидности) — 1 запрос.
  2. Открытый интерес всех USDT-перпетуалов — 1 запрос, копится в
     accumulation_state.json, чтобы за несколько часов накопить свой ряд
     "растёт/падает" (у OKX нет простого бесплатного эндпоинта истории OI).
  3. Тренд BTC на 4ч (EMA50) — 1 запрос, для контекста в сообщениях.
  4. Для каждой монеты, прошедшей минимальный порог ликвидности "только
     наблюдение" — свечи 1ч, расчёт сжатия волатильности (BBW percentile) и
     тренда объёма.
  5. Для монет, где сжатие+рост объёма совпали — точечно funding rate.
  6. В Telegram — ОДНО сводное сообщение "просканировано / прошло фильтр /
     в списке наблюдения" на каждый запуск (это и есть "тест", который ты
     просил), плюс отдельное сообщение по каждой НОВОЙ монете в списке
     наблюдения (не дублируем на каждый час, пока она там же).

Переменные окружения — те же секреты, что у остальных ботов:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""
import os
import sys
import json
import datetime

from okx_client import fetch_tickers, fetch_open_interest, fetch_funding_rate
from accumulation_scanner import ACCUM_PARAMS, analyze_instrument, interpret_funding, get_btc_trend
from telegram_bot import send_message

HERE = os.path.dirname(__file__)
STATE_FILE = os.environ.get("ACCUM_STATE_FILE", os.path.join(HERE, "accumulation_state.json"))

OI_HISTORY_MAX_LEN = ACCUM_PARAMS["oiTrendLookbackScans"] + 4


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"oi_history": {}, "watchlist": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fmt_ts(ts_ms):
    return datetime.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M UTC")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ОШИБКА: не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    params = dict(ACCUM_PARAMS)
    state = load_state()
    state.setdefault("oi_history", {})
    state.setdefault("watchlist", {})

    try:
        tickers = fetch_tickers("SWAP")
    except Exception as e:
        print(f"ОШИБКА при получении тикеров: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        oi_now = fetch_open_interest("SWAP")
    except Exception as e:
        print(f"ОШИБКА при получении открытого интереса: {e}", file=sys.stderr)
        oi_now = {}

    btc_bullish = get_btc_trend(params)

    liquid_watchlist_tier = [
        t for t in tickers
        if t["instId"].endswith("-USDT-SWAP") and t["volCcy24h"] >= params["minLiquidityUsdtWatchlist"]
    ]
    print(f"Всего USDT-перпетуалов: {len(tickers)}, прошли порог ликвидности "
          f"'только наблюдение' (>= {params['minLiquidityUsdtWatchlist']:,.0f} USDT/24ч): {len(liquid_watchlist_tier)}")

    # обновляем локальную историю OI (свой собственный ряд, раз нет готового API истории)
    for t in liquid_watchlist_tier:
        inst_id = t["instId"]
        hist = state["oi_history"].setdefault(inst_id, [])
        hist.append(oi_now.get(inst_id))
        if len(hist) > OI_HISTORY_MAX_LEN:
            del hist[: len(hist) - OI_HISTORY_MAX_LEN]

    qualifying = []
    checked = 0
    for t in liquid_watchlist_tier:
        inst_id = t["instId"]
        try:
            result = analyze_instrument(inst_id, oi_now.get(inst_id), state["oi_history"].get(inst_id), params=params)
        except Exception as e:
            print(f"ОШИБКА анализа {inst_id}: {e}", file=sys.stderr)
            continue
        checked += 1
        if result and result["qualifies"]:
            result["volCcy24h"] = t["volCcy24h"]
            qualifying.append(result)

    print(f"Успешно проанализировано (хватило истории): {checked}. "
          f"Прошли сжатие+рост объёма: {len(qualifying)}")

    # funding rate — только для тех, кто уже прошёл более дешёвый фильтр
    for r in qualifying:
        r["fundingRate"] = fetch_funding_rate(r["instId"])

    # сводное сообщение-"тест" на каждый запуск
    summary_lines = [
        "📡 <b>Радар накопления — скан завершён</b>",
        f"Инструментов всего: {len(tickers)}",
        f"Прошли ликвидность (>= {params['minLiquidityUsdtWatchlist']:,.0f} USDT/24ч): {len(liquid_watchlist_tier)}",
        f"Проанализировано (хватило истории): {checked}",
        f"В списке наблюдения (сжатие + рост объёма): {len(qualifying)}",
        f"Тренд BTC (4ч): {'бычий' if btc_bullish else 'МЕДВЕЖИЙ — осторожнее с лонг-сетапами'}",
    ]
    send_message(token, chat_id, "\n".join(summary_lines))
    print("Отправлена сводка скана.")

    qualifying_ids = {r["instId"] for r in qualifying}
    old_watchlist = state["watchlist"]

    # новые кандидаты — шлём подробное сообщение
    for r in qualifying:
        inst_id = r["instId"]
        if inst_id in old_watchlist:
            continue  # уже сообщали, пока монета не выходила из списка — не спамим повторно

        tier = "торгуемая" if r["volCcy24h"] >= params["minLiquidityUsdtTradeable"] else "ТОЛЬКО НАБЛЮДЕНИЕ (низкая ликвидность!)"
        oi_txt = f"{r['oiTrendRatio']:.2f}x за {params['oiTrendLookbackScans']}ч" if r.get("oiTrendRatio") else "недостаточно истории ещё"
        lines = [
            f"🔎 <b>ВОЗМОЖНОЕ НАКОПЛЕНИЕ</b> — {inst_id}",
            f"Ликвидность: {tier} (24ч оборот {r['volCcy24h']:,.0f} USDT)",
            f"Цена: {r['close']:.6g}",
            f"Сжатие волатильности: {r['bbwPercentile']:.0f}-й процентиль своей истории (чем ниже — уже сжатие)",
            f"Объём: {r['volRatio']:.2f}x к предыдущему периоду",
            f"Открытый интерес: {oi_txt}",
            f"Funding rate: {interpret_funding(r.get('fundingRate'), params)}",
            f"Тренд BTC (4ч): {'бычий' if btc_bullish else 'медвежий — осторожнее с идеей лонга здесь'}",
            "⚠ Это НЕ сигнал на вход — сжатие говорит \"скоро может тряхнуть\", но не говорит куда. "
            "Смотри график сам и жди реального пробоя (тот пришлёт отдельный сканер пампов/дампов).",
            f"Время: {fmt_ts(r['ts'])}",
        ]
        send_message(token, chat_id, "\n".join(lines))
        print(f"Отправлено уведомление о накоплении: {inst_id}")

    # обновляем watchlist: оставляем только тех, кто ещё качественно проходит фильтр
    state["watchlist"] = {r["instId"]: True for r in qualifying}

    save_state(state)


if __name__ == "__main__":
    main()
