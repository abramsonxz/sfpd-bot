"""
SFPD Reports Bot — Telegram бот для проверки отчётов Police Academy.
Размещается на Railway.app, работает 24/7.
"""

import os
import logging
import requests
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─── Логирование ───────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# ─── Конфигурация (из переменных окружения Railway) ────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
FIREBASE_URL = os.environ.get("FIREBASE_URL", "").rstrip("/")

# Админы: telegram_id → никнейм в игре
ADMINS: dict = {
    8378932761: "Ralph Rosenthal",
}

# Множество уже отправленных в Telegram отчётов
_notified: set = set()

# Временное хранилище: chat_id → report_id (ожидание причины отказа)
_pending_reject: dict = {}


# ─── Firebase helpers ──────────────────────────────────────────
def fb_get(path: str):
    try:
        r = requests.get(f"{FIREBASE_URL}/{path}.json", timeout=10)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        log.error("Firebase GET %s: %s", path, e)
        return None


def fb_patch(path: str, data: dict) -> bool:
    try:
        r = requests.patch(f"{FIREBASE_URL}/{path}.json", json=data, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.error("Firebase PATCH %s: %s", path, e)
        return False


# ─── Форматирование отчёта ────────────────────────────────────
def _exam_emoji(result: str) -> str:
    return "✅" if result == "Успешно сдан" else "❌"


def format_report(rid: str, r: dict) -> str:
    em = _exam_emoji(r.get("examResult", ""))
    evidence = (r.get("evidence") or "").strip()
    text = (
        f"📋 <b>Отчёт</b> <code>#{rid[:8]}</code>\n\n"
        f"👤 Экзаменатор: <b>{r.get('examinerNick', '—')}</b>\n"
        f"🎓 Кадет: <b>{r.get('cadetNick', '—')}</b>\n"
        f"📝 Экзамен: {r.get('examType', '—')}\n"
        f"{em} Итог: {r.get('examResult', '—')}\n"
        f"📅 Дата: {r.get('examDate', '—')}\n"
    )
    if evidence:
        text += f'📎 <a href="{evidence}">Доказательства</a>\n'
    reviewed = r.get("reviewedBy")
    if reviewed:
        text += f"\n🔧 Проверил: <b>{reviewed}</b>"
    reason = r.get("rejectReason")
    if reason:
        text += f"\n⚠️ Причина: <i>{reason}</i>"
    return text


# ─── Периодическая проверка новых отчётов ─────────────────────
async def job_check_reports(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = fb_get("reports")
    if not isinstance(data, dict):
        return

    for rid, r in data.items():
        if rid in _notified:
            continue
        if r.get("status") != "pending":
            _notified.add(rid)
            continue
        _notified.add(rid)

        text = format_report(rid, r)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Проверить", callback_data=f"approve_{rid}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{rid}"),
            ]
        ])

        for admin_id in ADMINS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                log.error("Не удалось отправить админу %s: %s", admin_id, e)


# ─── Обработка кнопок ✅ / ❌ ─────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    if uid not in ADMINS:
        await query.answer("⛔ Нет прав.", show_alert=True)
        return

    action, rid = query.data.split("_", 1)
    admin_name = ADMINS[uid]

    if action == "reject":
        # Запрашиваем причину отказа
        _pending_reject[query.message.chat_id] = rid
        try:
            await query.message.edit_text(
                f"❌ <b>ОТКЛОНЕНИЕ ОТЧЁТА</b>\n\n"
                f"📋 Отчёт <code>#{rid[:8]}</code>\n\n"
                "📝 Напишите <b>причину отклонения</b> следующим сообщением:\n\n"
                "<i>Например: Неполные доказательства, скриншот не читается</i>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # ── action == "approve" ──
    now = datetime.now(timezone.utc).isoformat()
    ok = fb_patch(f"reports/{rid}", {
        "status": "approved",
        "reviewedBy": admin_name,
        "reviewedAt": now,
        "rejectReason": None,
    })
    if not ok:
        await query.answer("⚠️ Ошибка обновления.", show_alert=True)
        return

    report = fb_get(f"reports/{rid}") or {}
    report.update({"status": "approved", "reviewedBy": admin_name})
    text = format_report(rid, report)

    try:
        await query.message.edit_text(
            f"✅ <b>ПРОВЕРЕНО</b>\n\n{text}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ─── Обработка текстовых сообщений (причина отказа) ───────────
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.from_user.id
    chat_id = update.effective_chat.id

    if uid not in ADMINS:
        return

    if chat_id not in _pending_reject:
        return

    rid = _pending_reject.pop(chat_id)
    reason = update.message.text.strip()
    admin_name = ADMINS[uid]
    now = datetime.now(timezone.utc).isoformat()

    ok = fb_patch(f"reports/{rid}", {
        "status": "rejected",
        "reviewedBy": admin_name,
        "reviewedAt": now,
        "rejectReason": reason,
    })
    if not ok:
        await update.message.reply_text("⚠️ Ошибка обновления в базе данных.")
        return

    report = fb_get(f"reports/{rid}") or {}
    report.update({
        "status": "rejected",
        "reviewedBy": admin_name,
        "rejectReason": reason,
    })
    text = format_report(rid, report)

    try:
        await update.message.reply_text(
            f"❌ <b>ОТКЛОНЕНО</b>\n\n{text}",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ─── /start ───────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid in ADMINS:
        await update.message.reply_text(
            f"👋 Привет, <b>{ADMINS[uid]}</b>!\n\n"
            "Я бот отчётов SFPD Police Academy.\n"
            "Новые отчёты будут приходить автоматически.\n\n"
            "• ✅ — проверить отчёт\n"
            "• ❌ — отклонить (потребуется указать причину)",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")


# ─── Запуск ───────────────────────────────────────────────────
def main() -> None:
    if not BOT_TOKEN:
        log.critical("BOT_TOKEN не задан!")
        return
    if not FIREBASE_URL:
        log.critical("FIREBASE_URL не задан!")
        return

    log.info("Запуск SFPD Reports Bot...")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Проверяем новые отчёты каждые 8 секунд
    app.job_queue.run_repeating(job_check_reports, interval=8)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
