"""
SFPD Reports Bot — Telegram бот для проверки отчётов Police Academy.
Размещается на Railway.app, работает 24/7.
"""

import os
import json
import base64
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
FIREBASE_PROJECT = os.environ.get("FIREBASE_PROJECT", "")

# Админы: telegram_id → никнейм в игре
ADMINS: dict = {
    8378932761: "Ralph Rosenthal",
}

# Множество уже отправленных в Telegram отчётов
_notified: set = set()

# Ожидание причины отказа: user_id -> report_id
_pending_reject: dict = {}

# Firebase Admin SDK для /makeadmin
_auth_app = None


# ─── Firebase Admin (lazy init) ────────────────────────────────
def _get_firebase_admin():
    global _auth_app
    if _auth_app is not None:
        return _auth_app
    if _auth_app is False:
        return None  # уже пробовали, не вышло

    cred_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
    if not cred_json:
        log.warning("FIREBASE_SERVICE_ACCOUNT не задан. /makeadmin недоступен.")
        _auth_app = False
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials, auth as fb_auth

        # Поддержка: чистый JSON, JSON с лишними пробелами/переносами, base64
        cred_dict = None
        for attempt in [cred_json.strip(), " ".join(cred_json.split()), cred_json.replace("\n", " ")]:
            try:
                cred_dict = json.loads(attempt)
                break
            except (json.JSONDecodeError, ValueError):
                continue
        if cred_dict is None:
            try:
                cred_dict = json.loads(base64.b64decode(cred_json.strip()).decode("utf-8"))
            except Exception:
                pass

        if not isinstance(cred_dict, dict):
            log.error("FIREBASE_SERVICE_ACCOUNT: не удалось распарсить JSON. Длина: %d", len(cred_json))
            _auth_app = False
            return None

        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        _auth_app = fb_auth
        log.info("Firebase Admin SDK инициализирован.")
        return _auth_app
    except Exception as e:
        log.error("Ошибка инициализации Firebase Admin: %s", e)
        _auth_app = False
        return None


# ─── Firebase REST helpers ─────────────────────────────────────
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
        f"<b>📋 ОТЧЁТ</b> <code>#{rid[:8]}</code>\n\n"
        f"<b>👤 Экзаменатор:</b> <b>{r.get('examinerNick', '—')}</b>\n"
        f"<b>🎓 Кадет:</b> <b>{r.get('cadetNick', '—')}</b>\n"
        f"<b>📝 Экзамен:</b> <b>{r.get('examType', '—')}</b>\n"
        f"<b>{em} Итог:</b> <b>{r.get('examResult', '—')}</b>\n"
        f"<b>📅 Дата:</b> <b>{r.get('examDate', '—')}</b>\n"
    )
    if evidence:
        text += f'<b>📎 Доказательства:</b> <a href="{evidence}">📎 Открыть</a>\n'
    reviewed = r.get("reviewedBy")
    if reviewed:
        text += f"\n<b>🔧 Проверил:</b> <b>{reviewed}</b>"
    reason = r.get("rejectReason")
    if reason:
        text += f"\n<b>⚠️ Причина:</b> <b>{reason}</b>"
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
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{rid}"),
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

    uid = update.effective_user.id
    if uid not in ADMINS:
        await query.answer("⛔ Нет прав.", show_alert=True)
        return

    action, rid = query.data.split("_", 1)
    admin_name = ADMINS[uid]

    if action == "reject":
        _pending_reject[uid] = rid
        log.info("Ожидание причины от admin %s для отчёта %s", uid, rid)
        try:
            await query.message.edit_text(
                "❌ <b>ОТКЛОНЕНИЕ ОТЧЁТА</b>\n\n"
                "📋 <b>Отчёт</b> <code>#{}</code>\n\n"
                "📝 <b>Введите причину отказа</b> следующим сообщением.\n\n"
                "<i>Например: Недостоверные\n"
                "Например: Неполные доказательства, скриншот не открывается</i>".format(rid[:8]),
                parse_mode="HTML",
            )
        except Exception as e:
            log.error("Ошибка редактирования: %s", e)
        return

    # ── action == "approve" ──
    now = datetime.now(timezone.utc).isoformat()
    ok = fb_patch(f"reports/{rid}", {
        "status": "approved",
        "reviewedBy": admin_name,
        "reviewedAt": now,
    })
    if not ok:
        await query.answer("⚠️ Ошибка обновления.", show_alert=True)
        return

    log.info("Отчёт %s утверждён: %s", rid[:8], admin_name)
    report = fb_get(f"reports/{rid}") or {}
    report.update({"status": "approved", "reviewedBy": admin_name})
    text = format_report(rid, report)

    try:
        await query.message.edit_text(
            "✅ <b>ОДОБРЕНО</b>\n\n{}".format(text),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ─── Обработка текстовых сообщений (причина отказа) ───────────
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    if uid not in ADMINS:
        return

    # Проверяем, ожидаем ли мы причину от этого админа
    rid = _pending_reject.pop(uid, None)
    if not rid:
        return

    reason = update.message.text.strip()
    admin_name = ADMINS[uid]
    now = datetime.now(timezone.utc).isoformat()

    log.info("Отклонение отчёта %s: %s — причина: %s", rid[:8], admin_name, reason)

    ok = fb_patch(f"reports/{rid}", {
        "status": "rejected",
        "reviewedBy": admin_name,
        "reviewedAt": now,
        "rejectReason": reason,
    })
    if not ok:
        log.error("Ошибка записи в Firebase для отчёта %s", rid)
        await update.message.reply_text("⚠️ Ошибка обновления в базе данных. Попробуйте ещё раз.")
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
            "❌ <b>ОТКЛОНЕНО</b>\n\n{}".format(text),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.error("Ошибка отправки: %s", e)


# ─── /start ───────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid in ADMINS:
        await update.message.reply_text(
            "<b>🛡 SFPD POLICE ACADEMY</b>\n"
            "<b>━━━━━━━━━━━━━━━━━━━</b>\n\n"
            "<b>👋 Привет, {}!</b>\n\n"
            "<b>Я бот отчётов Police Academy.</b>\n"
            "<b>Новые отчёты будут приходить</b>\n"
            "<b>автоматически в этот чат.</b>\n\n"
            "<b>УПРАВЛЕНИЕ:</b>\n"
            "<b>• ✅ Одобрить</b> — <b>подтвердить отчёт</b>\n"
            "<b>• ❌ Отклонить</b> — <b>указать причину</b>\n"
            "<b>• /makeadmin</b> — <b>назначить админа</b>".format(ADMINS[uid]),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("<b>⛔ У вас нет доступа к этому боту.</b>", parse_mode="HTML")


# ─── /makeadmin <email> ───────────────────────────────────────
async def cmd_makeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid not in ADMINS:
        await update.message.reply_text("<b>⛔ Нет прав.</b>", parse_mode="HTML")
        return

    if not context.args:
        await update.message.reply_text(
            "<b>📝 Использование:</b>\n"
            "<code>/makeadmin email@example.com</code>\n\n"
            "<i>Пользователь с этим email получит\n"
            "права администратора в админ-панели.</i>",
            parse_mode="HTML",
        )
        return

    email = context.args[0].strip()
    await update.message.reply_text(
        "<b>⏳ Назначение прав...</b>\n"
        f"<b>Email:</b> <code>{email}</code>",
        parse_mode="HTML",
    )

    fb_auth = _get_firebase_admin()
    if fb_auth is None:
        await update.message.reply_text(
            "<b>❌ Ошибка:</b> Firebase Admin не настроен.\n\n"
            "<b>Добавь переменную FIREBASE_SERVICE_ACCOUNT</b>\n"
            "<b>в Railway (вкладка Variables).</b>\n\n"
            "<b>Значение — содержимое JSON-файла ключа</b>\n"
            "<b>из Firebase Console → Project Settings →\n"
            "Service Accounts → Generate new private key.</b>",
            parse_mode="HTML",
        )
        return

    try:
        user = fb_auth.get_user_by_email(email)
        fb_auth.set_custom_user_claims(user.uid, {"admin": True})
        log.info("Админ назначен: %s (%s)", email, user.uid)
        await update.message.reply_text(
            f"<b>✅ Готово!</b>\n\n"
            f"<b>Email:</b> <code>{email}</code>\n"
            f"<b>Права администратора назначены.</b>\n\n"
            f"<b>Теперь можно войти в админ-панель</b>\n"
            f"<b>по адресу: твой-сайт/admin.html</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        log.error("Ошибка makeadmin: %s", e)
        err_msg = str(e)
        if "not found" in err_msg.lower():
            err_msg = (
                f"Пользователь <code>{email}</code> не найден.\n\n"
                "Сначала создай его:\n"
                "Firebase Console → Authentication → Users → Add user"
            )
        await update.message.reply_text(f"<b>❌ Ошибка:</b> {err_msg}", parse_mode="HTML")


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
    app.add_handler(CommandHandler("makeadmin", cmd_makeadmin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Проверяем новые отчёты каждые 8 секунд
    app.job_queue.run_repeating(job_check_reports, interval=8)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
