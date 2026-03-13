"""
Telegram-бот — мост между пользователем и Claude API.
Поддерживает текст, PDF, DOCX, TXT, изображения.
"""

import os
import base64
import tempfile
from pathlib import Path

import anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from docx import Document
from pypdf import PdfReader

# ─── Настройки ────────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = (
    "Ты — умный помощник Юсупа Вагабова, студента ДГТУ (Дагестанский государственный "
    "технический университет). Ты помогаешь ему с учёбой, магистерской работой на тему "
    "«Создание Gameready 3D-моделей с использованием ZBrush» и любыми другими вопросами. "
    "Отвечай на русском языке. Будь дружелюбным, конкретным и полезным."
)

MAX_HISTORY = 20       # максимум сообщений в истории (старые удаляются)
MAX_FILE_CHARS = 12000  # максимум символов из файла

# ─── Утилиты чтения файлов ─────────────────────────────────────────────────────

def read_pdf(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages).strip()
    except Exception as e:
        return f"[Ошибка чтения PDF: {e}]"


def read_docx(path: Path) -> str:
    try:
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        return f"[Ошибка чтения DOCX: {e}]"


def read_txt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return f"[Ошибка чтения TXT: {e}]"


def image_to_base64(path: Path) -> tuple[str, str]:
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return data, mime


# ─── История разговора ─────────────────────────────────────────────────────────

def get_history(context: ContextTypes.DEFAULT_TYPE) -> list:
    if "messages" not in context.user_data:
        context.user_data["messages"] = []
    return context.user_data["messages"]


def add_to_history(context: ContextTypes.DEFAULT_TYPE, role: str, content):
    history = get_history(context)
    history.append({"role": role, "content": content})
    # Обрезаем старые сообщения чтобы не переполнять контекст
    if len(history) > MAX_HISTORY:
        context.user_data["messages"] = history[-MAX_HISTORY:]


def ask_claude(history: list) -> str:
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    return response.content[0].text


# ─── Обработчики сообщений ─────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")

    add_to_history(context, "user", update.message.text)
    answer = ask_claude(get_history(context))
    add_to_history(context, "assistant", answer)

    await update.message.reply_text(answer)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    file_name = doc.file_name or "file"
    suffix = Path(file_name).suffix.lower()
    caption = update.message.caption or ""

    await update.message.chat.send_action("typing")

    # Скачиваем файл
    suffix_str = Path(file_name).suffix or ".tmp"
    tmp_fd, tmp_str = tempfile.mkstemp(suffix=suffix_str)
    os.close(tmp_fd)
    tmp_path = Path(tmp_str)
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(str(tmp_path))

    try:
        if suffix == ".pdf":
            text = read_pdf(tmp_path)
        elif suffix in (".docx", ".doc"):
            text = read_docx(tmp_path)
        elif suffix == ".txt":
            text = read_txt(tmp_path)
        elif suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            await _handle_image_file(tmp_path, file_name, caption, update, context)
            return
        else:
            await update.message.reply_text(
                f"Формат {suffix} не поддерживается. "
                "Могу читать: PDF, DOCX, TXT, PNG, JPG."
            )
            return

        if not text.strip():
            await update.message.reply_text("Не удалось прочитать файл — он пустой или повреждён.")
            return

        # Обрезаем если слишком длинный
        truncated = len(text) > MAX_FILE_CHARS
        text = text[:MAX_FILE_CHARS]

        user_message = (
            f"Я прислал файл «{file_name}».\n"
            f"{'(Показан фрагмент — файл слишком большой) ' if truncated else ''}"
            f"Содержимое:\n\n{text}"
        )
        if caption:
            user_message += f"\n\nМой вопрос по файлу: {caption}"
        else:
            user_message += "\n\nКратко расскажи о чём этот файл."

        add_to_history(context, "user", user_message)
        answer = ask_claude(get_history(context))
        add_to_history(context, "assistant", answer)

        await update.message.reply_text(answer)

    finally:
        tmp_path.unlink(missing_ok=True)


async def _handle_image_file(
    path: Path, name: str, caption: str,
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    data, mime = image_to_base64(path)
    question = caption if caption else "Что изображено на этом файле?"

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
        {"type": "text", "text": question},
    ]
    add_to_history(context, "user", content)
    answer = ask_claude(get_history(context))
    add_to_history(context, "assistant", answer)
    await update.message.reply_text(answer)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")

    photo = update.message.photo[-1]
    caption = update.message.caption or "Что на этом изображении?"

    tmp_fd, tmp_str = tempfile.mkstemp(suffix=".jpg")
    os.close(tmp_fd)
    tmp_path = Path(tmp_str)
    tg_file = await photo.get_file()
    await tg_file.download_to_drive(str(tmp_path))

    try:
        data, mime = image_to_base64(tmp_path)
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
            {"type": "text", "text": caption},
        ]
        add_to_history(context, "user", content)
        answer = ask_claude(get_history(context))
        add_to_history(context, "assistant", answer)
        await update.message.reply_text(answer)
    finally:
        tmp_path.unlink(missing_ok=True)


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
