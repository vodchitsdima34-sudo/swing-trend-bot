"""
Главный скрипт: раз в час (запускается GitHub Actions по расписанию), для
КАЖДОЙ монеты из списка COINS ниже:
1) забирает свежие свечи с OKX за 1 час,
2) пересчитывает логику "Свинг-тренд v2" со своим набором параметров,
3) если на ПОСЛЕДНЕЙ закрытой свече появился новый сигнал (вход/выход) —
   которого мы ещё не отправляли — шлёт сообщение в Telegram,
4) запоминает, что этот бар уже обработан (отдельный state-файл на монету),
   чтобы не дублировать уведомления.

Переменные окружения (задаются как GitHub Secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
Необязательные:
  POSITION_MARGIN_USDT  (по умолчанию 100 — маржа на сделку в USDT, для расчёта размера позиции)
  POSITION_LEVERAGE     (по умолчанию 10 — плечо, тоже для расчёта размера позиции)

Список отслеживаемых монет — просто добавь ещё один dict в COINS, когда
протестируешь и подтвердишь параметры для следующей монеты через Strategy
Tester (см. ETH_PARAMS / BTC_PARAMS в strategy.py).
"""
import os
import sys
import json
import datetime

from okx_client import fetch_candles
from strategy import compute, ETH_PARAMS, BTC_PARAMS
from telegram_bot import send_message

HERE = os.path.dirname(__file__)

COINS = [
    dict(inst_id="ETH-USDT-SWAP", bar="1H", params=ETH_PARAMS, state_file=os.path.join(HERE, "state_eth.json")),
    dict(inst_id="BTC-USDT-SWAP", bar="1H", params=BTC_PARAMS, state_file=os.path.join(HERE, "state_btc.json")),
]

# Расчёт размера позиции для каждого сигнала входа — маржа на сделку и плечо
# задаются переменными окружения (по умолчанию 100 USDT маржи, плечо x10).
POSITION_MARGIN_USDT = float(os.environ.get("POSITION_MARGIN_USDT", "100"))
POSITION_LEVERAGE = float(os.environ.get("POSITION_LEVERAGE", "10"))


def load_state(state_file):
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_notified_ts": None}


def save_state(state_file, state):
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fmt_ts(ts_ms):
    dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def build_position_block(rec, price, is_long):
    """Считает размер позиции под фиксированную маржу/плечо из переменных
    окружения — та же логика, что и в HTML-калькуляторе position_calculator.html.
    Возвращает список строк для сообщения, либо [] если не хватает данных."""
    stop = rec.get("activeStop")
    target = rec.get("activeTarget")
    if stop is None or not price:
        return []

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
        f"Стоп: {stop:.2f} (−{stop_dist_price:.2f}, {stop_dist_pct:.2f}%)",
        f"Убыток по стопу: −{loss_usd:.2f} USDT ({loss_pct_of_margin:.0f}% от маржи)",
        f"Прибл. ликвидация*: {liq_price:.2f} (~{liq_dist_pct:.1f}%)",
    ]
    if target is not None:
        tgt_dist_price = abs(target - price)
        tgt_profit = qty * tgt_dist_price
        lines.append(f"Справочная цель (R:R): {target:.2f} (+{tgt_profit:.2f} USDT, ориентир — реально держим трейлингом)")

    if stop_dist_pct >= liq_dist_pct * 0.9:
        lines.append("⚠ Стоп системы близко к цене ликвидации при этом плече — проверь калькулятор перед входом!")

    return lines


def build_message(rec, inst_id):
    lines = []
    price = rec["close"]
    time_str = fmt_ts(rec["ts"])

    is_entry_long = bool(rec.get("longSignal"))
    is_entry_short = bool(rec.get("shortSignal"))

    if is_entry_long:
        kind = rec.get("entryKind", "пробой")
        lines.append(f"🟢 <b>LONG ({kind})</b> — {inst_id}")
    if is_entry_short:
        kind = rec.get("entryKind", "пробой")
        lines.append(f"🔴 <b>SHORT ({kind})</b> — {inst_id}")
    if rec.get("exitLongSignal"):
        lines.append(f"🟡 <b>ВЫХОД из LONG</b> ({rec.get('exitReason', '')})")
    if rec.get("exitShortSignal"):
        lines.append(f"🟡 <b>ВЫХОД из SHORT</b> ({rec.get('exitReason', '')})")

    if not lines:
        return None

    lines.append(f"Цена: {price:.2f}")
    lines.append(f"Время закрытия свечи: {time_str}")
    if rec.get("adx") is not None:
        lines.append(f"ADX: {rec['adx']:.1f}")
    if rec.get("rsi") is not None:
        lines.append(f"RSI: {rec['rsi']:.1f}")
    if rec.get("volRatio") is not None:
        lines.append(f"Объём: {rec['volRatio']:.2f}x от среднего")

    if is_entry_long or is_entry_short:
        lines.extend(build_position_block(rec, price, is_entry_long))
    elif rec.get("activeStop") is not None:
        # для выходов просто покажем последний активный стоп, без расчёта позиции
        lines.append(f"Стоп на момент выхода: {rec['activeStop']:.2f}")

    return "\n".join(lines)


def process_coin(token, chat_id, coin):
    inst_id = coin["inst_id"]
    bar = coin["bar"]
    params = coin["params"]
    state_file = coin["state_file"]

    print(f"=== {inst_id} ===")

    try:
        candles = fetch_candles(inst_id, bar, total_bars=800)
    except Exception as e:
        print(f"ОШИБКА при получении свечей для {inst_id}: {e}", file=sys.stderr)
        return

    if len(candles) < 100:
        print(f"ОШИБКА: получено слишком мало свечей для {inst_id} ({len(candles)}), пропускаем", file=sys.stderr)
        return

    records = compute(candles, params=params)
    last = records[-1]

    state = load_state(state_file)
    last_notified_ts = state.get("last_notified_ts")

    is_first_run = last_notified_ts is None
    is_new_bar = last_notified_ts is None or last["ts"] > last_notified_ts

    if is_first_run:
        # Первый запуск для этой монеты — не шлём уведомление по всей истории
        # разом, просто фиксируем текущий бар как точку отсчёта.
        print(f"Первый запуск для {inst_id}: фиксирую текущий бар без уведомления.")
        save_state(state_file, {"last_notified_ts": last["ts"]})
        return

    if is_new_bar:
        msg = build_message(last, inst_id)
        if msg:
            send_message(token, chat_id, msg)
            print("Отправлено уведомление:\n", msg)
        else:
            print(f"Новый бар {fmt_ts(last['ts'])} закрыт для {inst_id}, но сигналов нет — молчим.")
        save_state(state_file, {"last_notified_ts": last["ts"]})
    else:
        print(f"Бар {fmt_ts(last['ts'])} для {inst_id} уже обработан ранее — ничего не делаем.")


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("ОШИБКА: не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    for coin in COINS:
        process_coin(token, chat_id, coin)


if __name__ == "__main__":
    main()
