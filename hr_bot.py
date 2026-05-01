import logging
import asyncio
import os
import base64
import json
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import anthropic
import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# НАСТРОЙКИ — на Railway задаются через переменные окружения
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "ВАШ_ТОКЕН")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "ВАШ_КЛЮЧ")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1dEuum4JXYWRStUyi2hhiLNq_ARHw3FLigzX7w6O33KI")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GOOGLE_CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "")
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    # В облаке credentials передаются как JSON-строка в переменной окружения
    if GOOGLE_CREDENTIALS_JSON:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).sheet1


def save_note(employee: str, category: str, note: str):
    sheet = get_sheet()
    sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        employee.strip().capitalize(),
        category.strip(),
        note.strip()
    ])


def get_notes(employee: str) -> list:
    sheet = get_sheet()
    rows = sheet.get_all_records()
    name = employee.strip().capitalize()
    return [r for r in rows if str(r.get("employee", "")).strip().capitalize() == name]


def get_all_employees() -> dict:
    sheet = get_sheet()
    rows = sheet.get_all_records()
    employees = {}
    for r in rows:
        name = str(r.get("employee", "")).strip().capitalize()
        if name:
            employees[name] = employees.get(name, 0) + 1
    return employees


def build_prep(employee: str, notes: list) -> str:
    if not notes:
        return f"По сотруднику {employee} пока нет ни одной заметки."

    notes_text = "\n\n".join(
        f"[{n.get('date','')}] [{n.get('category','общее')}]\n{n.get('note','')}"
        for n in notes
    )

    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        system="""Ты — ассистент HR-менеджера. Анализируешь заметки по сотруднику
и готовишь структурированный план для встречи 1:1.

Твой вывод всегда строго по структуре:

## Сотрудник: {имя}

### Ключевые темы для обсуждения
(3-5 самых важных пунктов — что нельзя пропустить)

### Динамика
(как менялась ситуация — рост, стагнация, тревожные сигналы)

### Вопросы для разговора
(3-5 конкретных открытых вопросов, которые стоит задать)

### На что обратить внимание
(риски, паттерны, то что повторяется в заметках)

Пиши конкретно, без воды. Опирайся только на то что есть в заметках.""",
        messages=[{
            "role": "user",
            "content": f"Сотрудник: {employee}\n\nЗаметки:\n{notes_text}\n\nПодготовь план встречи 1:1."
        }]
    )
    return message.content[0].text


# ── Категории ──────────────────────────────────────────────
CATEGORIES = {
    "ф": "фидбек",
    "фидбек": "фидбек",
    "м": "метрики",
    "метрики": "метрики",
    "н": "настроение",
    "настроение": "настроение",
    "р": "развитие",
    "развитие": "развитие",
    "о": "общее",
    "общее": "общее",
}


def parse_message(text: str):
    """
    Парсит сообщение. Форматы:
      Имя: заметка
      Имя [категория]: заметка
      Имя - заметка
    Возвращает (employee, category, note) или None
    """
    text = text.strip()

    # Формат: Имя [категория]: заметка
    if "[" in text and "]" in text and ":" in text:
        try:
            name_part = text[:text.index("[")].strip()
            cat_part = text[text.index("[")+1:text.index("]")].strip().lower()
            note_part = text[text.index("]")+1:].strip().lstrip(":").strip()
            category = CATEGORIES.get(cat_part, cat_part)
            if name_part and note_part:
                return name_part, category, note_part
        except Exception:
            pass

    # Формат: Имя: заметка
    if ":" in text:
        parts = text.split(":", 1)
        employee = parts[0].strip()
        note = parts[1].strip()
        if employee and note and len(employee.split()) <= 3:
            return employee, "общее", note

    # Формат: Имя - заметка
    if " - " in text:
        parts = text.split(" - ", 1)
        employee = parts[0].strip()
        note = parts[1].strip()
        if employee and note and len(employee.split()) <= 3:
            return employee, "общее", note

    return None


# ── Handlers ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я помогаю готовиться к встречам 1:1.\n\n"
        "*Как добавить заметку:*\n"
        "`Паша: текст` — общая заметка\n"
        "`Паша [фидбек]: текст` — с категорией\n"
        "`/note Паша текст` — через команду\n\n"
        "*Категории:* фидбек · метрики · настроение · развитие · общее\n\n"
        "*Команды:*\n"
        "`/prep Паша` — план встречи 1:1\n"
        "`/list Паша` — все заметки\n"
        "`/team` — список сотрудников\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Формат: `/note Паша текст`", parse_mode="Markdown")
        return
    employee = context.args[0]
    note = " ".join(context.args[1:])
    try:
        save_note(employee, "общее", note)
        await update.message.reply_text(f"✓ Сохранено для {employee.capitalize()}")
    except Exception as e:
        logger.error(f"ОШИБКА: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_prep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: `/prep Паша`", parse_mode="Markdown")
        return
    employee = context.args[0]
    await update.message.reply_text(f"Анализирую заметки по {employee.capitalize()}...")
    try:
        notes = get_notes(employee)
        result = build_prep(employee, notes)
        await update.message.reply_text(result, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"ОШИБКА: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Формат: `/list Паша`", parse_mode="Markdown")
        return
    employee = context.args[0]
    try:
        notes = get_notes(employee)
        if not notes:
            await update.message.reply_text(f"По {employee.capitalize()} заметок пока нет.")
            return
        text = f"*Заметки по {employee.capitalize()}* ({len(notes)} шт.):\n\n"
        for n in notes[-10:]:
            cat = n.get('category', 'общее')
            text += f"_{n.get('date','')}_ [{cat}]\n{n.get('note','')}\n\n"
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"ОШИБКА: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        employees = get_all_employees()
        if not employees:
            await update.message.reply_text("Сотрудников пока нет. Добавь первую заметку!")
            return
        text = "*Твоя команда:*\n\n"
        for name, count in sorted(employees.items()):
            text += f"• {name} — {count} заметок\n"
        text += "\nНапиши `/prep Имя` чтобы подготовиться к встрече."
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"ОШИБКА: {e}")
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parsed = parse_message(text)

    if parsed:
        employee, category, note = parsed
        try:
            save_note(employee, category, note)
            await update.message.reply_text(
                f"✓ Сохранено для {employee.capitalize()} [{category}]"
            )
        except Exception as e:
            logger.error(f"ОШИБКА СОХРАНЕНИЯ: {e}")
            await update.message.reply_text(f"Ошибка при сохранении: {e}")
    else:
        await update.message.reply_text(
            "Не понял формат. Попробуй:\n"
            "`Паша: текст заметки`\n"
            "`Паша [фидбек]: текст`\n"
            "или `/note Паша текст`",
            parse_mode="Markdown"
        )


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("note", handle_note))
    app.add_handler(CommandHandler("prep", handle_prep))
    app.add_handler(CommandHandler("list", handle_list))
    app.add_handler(CommandHandler("team", handle_team))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
