"""
Главный скрипт: раз в час (запускается GitHub Actions по расписанию)
1) забирает свежие свечи ETHUSDT.P (OKX) за 1 час,
2) пересчитывает логику "Свинг-тренд v2",
3) если на ПОСЛЕДНЕЙ закрытой свече появился новый сигнал (вход/выход) —
   которого мы ещё не отправляли — шлёт сообщение в Telegram,
4) запоминает, что этот бар уже обработан (state.json), чтобы не дублировать.

Переменные окружения (задаются как GitHub Secrets):
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
Необязательные:
  OKX_INST_ID   (по умолчанию ETH-USDT-SWAP)
  OKX_BAR       (по умолчанию 1H)
  STATE_FILE    (по умолчанию state.json рядом со скриптом)
"""
import os
import sys
import json
import datetime

from okx_client import fetch_candles
from strategy import compute
from telegram_bot import send_message

STATE_FILE = os.environ.get("STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_notified_ts": None}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fmt_ts(ts_ms):
    dt = datetime.datetime.utcfromtimestamp(ts_ms / 1000)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def build_message(rec, inst_id):
    lines = []
    price = rec["close"]
    time_str = fmt_ts(rec["ts"])

    if rec.get("longSignal"):
        kind = rec.get("entryKind", "пробой")
        lines.append(f"🟢 <b>LONG ({kind})</b> — {inst_id}")
    if rec.get("shortSignal"):
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
    if rec.get("activeStop") is not None:
        lines.append(f"Стоп: {rec['activeStop']:.2f}")
    if rec.get("adx") is not None:
        lines.append(f"ADX: {rec['adx']:.1f}")
    if rec.get("rsi") is not None:
        lines.append(f"RSI: {rec['rsi']:.1f}")
    if rec.get("volRatio") is not None:
        lines.append(f"Объём: {rec['volRatio']:.2f}x от среднего")

    return "\n".join(lines)


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    inst_id = os.environ.get("OKX_INST_ID", "ETH-USDT-SWAP")
    bar = os.environ.get("OKX_BAR", "1H")

    if not token or not chat_id:
        print("ОШИБКА: не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    candles = fetch_candles(inst_id, bar, total_bars=800)
    if len(candles) < 100:
        print(f"ОШИБКА: получено слишком мало свечей ({len(candles)}), пропускаем этот запуск", file=sys.stderr)
        sys.exit(1)

    records = compute(candles)
    last = records[-1]

    state = load_state()
    last_notified_ts = state.get("last_notified_ts")

    is_first_run = last_notified_ts is None
    is_new_bar = last_notified_ts is None or last["ts"] > last_notified_ts

    if is_first_run:
        # Первый запуск — не шлём уведомление по всей истории разом,
        # просто фиксируем текущий бар как точку отсчёта.
        print("Первый запуск: фиксирую текущий бар без уведомления.")
        save_state({"last_notified_ts": last["ts"]})
        return

    if is_new_bar:
        msg = build_message(last, inst_id)
        if msg:
            send_message(token, chat_id, msg)
            print("Отправлено уведомление:\n", msg)
        else:
            print(f"Новый бар {fmt_ts(last['ts'])} закрыт, но сигналов нет — молчим.")
        save_state({"last_notified_ts": last["ts"]})
    else:
        print(f"Бар {fmt_ts(last['ts'])} уже обработан ранее — ничего не делаем.")


if __name__ == "__main__":
    main()
