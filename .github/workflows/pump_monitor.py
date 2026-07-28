"""
Главный скрипт сканера пампов/дампов — отдельный от swing-тренд бота
(monitor.py), запускается СВОИМ, более частым расписанием (по умолчанию раз в
15 минут, см. .github/workflows/pump_monitor.yml).

Что делает при каждом запуске:
  1. Обновляет уже открытые (просигналенные ранее) виртуальные сделки —
     подтягивает трейлинг-стоп по свежим свечам, и если стоп пробит — шлёт
     сообщение "ВЫХОД" в Telegram.
  2. Забирает тикеры ВСЕХ USDT-перпетуалов OKX одним запросом, отбирает топ
     кандидатов по силе движения за 24ч (см. pump_scanner.shortlist_candidates).
  3. Для кандидатов, по которым сейчас нет открытой позиции и не действует
     cooldown после недавнего выхода, проверяет условия памп/дамп на свечах
     (объём + движение цены) — если сработало, шлёт "ВХОД" в Telegram и
     начинает отслеживать эту виртуальную сделку.

ВАЖНО: пороги в pump_scanner.py — это эвристика, не бэктестированный рецепт
(бэктест здесь в принципе невозможен — состав "интересных" монет каждый раз
разный, среди них часто совсем новые листинги). Это инструмент "быстро узнать
и решить самому", а не гарантированная система с подтверждённым edge, как
свинг-тренд. Особенно осторожно — с новыми/маленькими монетами: даже после
фильтра по ликвидности риск резкой манипуляции цены выше, чем у ETH/BTC.

Переменные окружения (те же секреты, что и у monitor.py — можно переиспользовать):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
Необязательные:
  POSITION_MARGIN_USDT  (по умолчанию 100)
  POSITION_LEVERAGE     (по умолчанию 10)
  PUMP_STATE_FILE       (по умолчанию pump_state.json рядом со скриптом)
"""
import os
import sys
import json
import datetime
import concurrent.futures

from okx_client import fetch_tickers
from pump_scanner import PUMP_PARAMS, shortlist_candidates, analyze_instrument, update_open_position
from telegram_bot import send_message

HERE = os.path.dirname(__file__)
STATE_FILE = os.environ.get("PUMP_STATE_FILE", os.path.join(HERE, "pump_state.json"))

POSITION_MARGIN_USDT = float(os.environ.get("POSITION_MARGIN_USDT", "100"))
POSITION_LEVERAGE = float(os.environ.get("POSITION_LEVERAGE", "10"))

BAR_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1H": 3_600_000}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"open_positions": {}, "cooldown_until": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fmt_ts(ts_ms):
    return datetime.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d %H:%M UTC")


def position_block(price, stop, is_long):
    margin = POSITION_MARGIN_USDT
    lev = POSITION_LEVERAGE
    notional = margin * lev
    qty = notional / price

    stop_dist_price = abs(price - stop)
    stop_dist_pct = (stop_dist_price / price) * 100
    loss_usd = qty * stop_dist_price
    loss_pct_of_margin = (loss_usd / margin) * 100 if margin else 0

    liq_dist_pct = (1 / lev) * 100 if lev else 0
    liq_price = price * (1 - 1 / lev) if is_long else price * (1 + 1 / lev)

    lines = [
        "",
        f"💰 Позиция (маржа {margin:.0f} USDT, плечо x{lev:.0f}):",
        f"Размер: {notional:.0f} USDT (~{qty:.5f})",
        f"Стоп: {stop:.6g} (−{stop_dist_price:.6g}, {stop_dist_pct:.2f}%)",
        f"Убыток по стопу: −{loss_usd:.2f} USDT ({loss_pct_of_margin:.0f}% от маржи)",
        f"Прибл. ликвидация*: {liq_price:.6g} (~{liq_dist_pct:.1f}%)",
    ]
    if stop_dist_pct >= liq_dist_pct * 0.9:
        lines.append("⚠ ВНИМАНИЕ: стоп близко к цене ликвидации при этом плече — на такой волатильной монете снизь плечо/размер!")
    return lines


def bar_ms(bar):
    return BAR_MS.get(bar, 900_000)


def process_exits(token, chat_id, state, params):
    still_open = {}
    for inst_id, pos in state["open_positions"].items():
        updated, exit_hit, last_close = update_open_position(pos, params=params)
        if exit_hit:
            direction_txt = "LONG (памп)" if updated["direction"] == "long" else "SHORT (дамп)"
            pnl_pct = ((last_close - updated["entryPrice"]) / updated["entryPrice"] * 100) if updated["direction"] == "long" \
                else ((updated["entryPrice"] - last_close) / updated["entryPrice"] * 100)
            msg = (
                f"🟡 <b>ВЫХОД по трейлингу</b> — {inst_id} ({direction_txt})\n"
                f"Вход: {updated['entryPrice']:.6g} → Выход: {last_close:.6g}\n"
                f"Результат по цене: {pnl_pct:+.2f}%\n"
                f"Время входа: {fmt_ts(updated['entryTs'])}"
            )
            send_message(token, chat_id, msg)
            print("Отправлено уведомление о выходе:\n", msg)
            state["cooldown_until"][inst_id] = updated["ts"] if "ts" in updated else 0
            state["cooldown_until"][inst_id] = int(datetime.datetime.utcnow().timestamp() * 1000) + params["cooldownBars"] * bar_ms(params["bar"])
        else:
            still_open[inst_id] = updated
    state["open_positions"] = still_open


def _safe_analyze(inst_id, params):
    try:
        return inst_id, analyze_instrument(inst_id, params=params)
    except Exception as e:
        print(f"ОШИБКА анализа {inst_id}: {e}", file=sys.stderr)
        return inst_id, None


def process_entries(token, chat_id, state, params):
    try:
        tickers = fetch_tickers("SWAP")
    except Exception as e:
        print(f"ОШИБКА при получении тикеров: {e}", file=sys.stderr)
        return

    candidates = shortlist_candidates(tickers, params=params)
    now_ms = int(datetime.datetime.utcnow().timestamp() * 1000)

    # заранее отсеиваем то, что уже открыто или на cooldown — не тратим на них запросы
    to_check = []
    for t in candidates:
        inst_id = t["instId"]
        if inst_id in state["open_positions"]:
            continue
        cooldown_until = state["cooldown_until"].get(inst_id)
        if cooldown_until and now_ms < cooldown_until:
            continue
        to_check.append(inst_id)

    print(f"Проверяю {len(to_check)} монет из {len(candidates)} прошедших фильтр ликвидности "
          f"(остальные уже в открытых позициях или на cooldown)...")

    results = []
    max_workers = max(1, params.get("maxWorkers", 4))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for inst_id, result in pool.map(lambda i: _safe_analyze(i, params), to_check):
            results.append((inst_id, result))

    for inst_id, result in results:
        if not result or not (result["pumpSignal"] or result["dumpSignal"]):
            continue

        direction = result["direction"]
        is_long = direction == "long"
        kind_txt = "🟢 <b>ПАМП — вход LONG</b>" if is_long else "🔴 <b>ДАМП — вход SHORT</b>"
        lines = [
            f"{kind_txt} — {inst_id}",
            f"Цена: {result['close']:.6g}",
            f"Движение за {params['momentumLookback']} баров: {result['momentumPct']:+.2f}%",
            f"Объём: {result['volRatio']:.1f}x от среднего",
            f"Время закрытия свечи: {fmt_ts(result['ts'])}",
            "⚠ Эвристический сигнал без бэктеста — проверь график сам перед входом.",
        ]
        lines.extend(position_block(result["close"], result["stop"], is_long))
        msg = "\n".join(lines)
        send_message(token, chat_id, msg)
        print("Отправлено уведомление о входе:\n", msg)

        state["open_positions"][inst_id] = dict(
            instId=inst_id,
            direction=direction,
            entryPrice=result["close"],
            activeStop=result["stop"],
            extreme=result["close"],
            entryTs=result["ts"],
        )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ОШИБКА: не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    params = dict(PUMP_PARAMS)
    state = load_state()

    process_exits(token, chat_id, state, params)
    process_entries(token, chat_id, state, params)

    save_state(state)


if __name__ == "__main__":
    main()
