#!/usr/bin/env python3
"""LAFB_Bot - Honest Telegram Assistant (Cloud)

Rules of this bot:
1. NEVER claim something is done if it was not actually done.
2. NEVER invent facts. If unsure, say "I am not sure".
3. Cannot access the user's Gmail, files, passwords, or accounts.
"""

import io
import os
import sys
import logging
from google import genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = "gemini-3.5-flash-lite"

# How many past messages to remember per user (keeps costs tiny)
MEMORY_LIMIT = 10

# Honest system prompt: the bot must never fake being a doer
SYSTEM_PROMPT = (
    "You are LAFB_Bot, an honest personal assistant on Telegram for Liaqat. "
    "STRICT RULES you must always follow: "
    "1) NEVER claim you did a real-world task (emails, files, accounts, "
    "Gmail cleanup, etc.) unless a tool result in this conversation proves it. "
    "You have no tools. If Liaqat asks you to DO something outside this chat "
    "(organize Gmail, delete files, log in somewhere), clearly say: 'I can "
    "guide you step by step, but I cannot do it myself.' Then give the steps. "
    "2) NEVER invent facts. If you do not know something, say 'I am not sure'. "
    "3) You cannot remember past conversations after a restart, but you DO "
    "remember everything in the current conversation. "
    "4) Be concise, warm and helpful. "
    "5) For research questions, answer from your knowledge and say when "
    "information may be outdated."
)

gemini_client = None
if GEMINI_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_KEY)
        print(f"Gemini: OK ({MODEL})")
    except Exception as e:
        print(f"Gemini error: {e}")

logging.basicConfig(format="%(asctime)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- memory ---

# Memory store: chat_id -> list of {"role": "user"|"model", "text": str}
MEMORY_STORE = {}

def context_history(key: str):
    return MEMORY_STORE.setdefault(key, [])

def set_context_history(key: str, history):
    MEMORY_STORE[key] = history

def build_contents(chat_id: int, user_text: str):
    """Build the full conversation (memory + new message) for Gemini."""
    history = context_history(f"chat_{chat_id}")
    contents = []
    for msg in history:
        if msg["role"] == "user":
            contents.append({"role": "user", "parts": [msg["text"]]})
        else:
            contents.append({"role": "model", "parts": [msg["text"]]})
    contents.append({"role": "user", "parts": [user_text]})
    return contents

# ------------------------------------------------------------- PPT maker ---

def make_pptx(topic: str, outline_text: str) -> bytes:
    """Build a simple .pptx from a topic + outline text. Returns file bytes."""
    from pptx import Presentation
    from pptx.util import Inches, Pt

    prs = Presentation()

    # Title slide
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = topic.title()
    slide.placeholders[1].text = "Made by LAFB_Bot"

    # Content slides: split outline into blocks by blank lines
    blocks = [b.strip() for b in outline_text.split("\n\n") if b.strip()]
    for block in blocks[:8]:  # max 8 slides
        lines = [l.strip("- *• ") for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        title = lines[0][:80]
        bullets = lines[1:][:6]
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
        body = slide.placeholders[1].text_frame
        body.clear()
        if bullets:
            body.text = bullets[0]
            for b in bullets[1:]:
                p = body.add_paragraph()
                p.text = b
                p.font.size = Pt(18)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()

async def ppt_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /ppt topic here"""
    topic = " ".join(context.args).strip()
    if not topic:
        await update.message.reply_text(
            "Usage: /ppt <topic>\nExample: /ppt Benefits of solar energy"
        )
        return
    if not gemini_client:
        await update.message.reply_text("AI is not configured. Check GEMINI_API_KEY.")
        return

    status = await update.message.reply_text(f"Creating presentation: {topic} ...")
    try:
        outline_prompt = (
            f"Create a presentation outline for: {topic}\n"
            "Return 5-8 sections. Each section: a title line, then 3-5 short "
            "bullet lines starting with '- '. Separate sections with a blank "
            "line. No extra commentary."
        )
        outline = gemini_client.models.generate_content(
            model=MODEL, contents=outline_prompt
        ).text.strip()
        pptx_bytes = make_pptx(topic, outline)
        await status.edit_text("Done! Sending your presentation...")
        await update.message.reply_document(
            document=io.BytesIO(pptx_bytes),
            filename=f"{topic[:40].replace(' ', '_')}.pptx",
        )
    except Exception as e:
        logger.error(f"PPT error: {e}")
        await status.edit_text("Sorry, something went wrong making the PPT.")

# ------------------------------------------------------------- handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to LAFB_Bot!\n\n"
        "I am your AI assistant. I remember our conversation while we talk.\n\n"
        "What I CAN do:\n"
        "- Answer questions & chat (with memory)\n"
        "- /ppt <topic> - make a PowerPoint and send it here\n"
        "- Explain any topic\n\n"
        "What I CANNOT do (I will never pretend I can):\n"
        "- Access your Gmail, files, passwords or accounts\n"
        "- Do tasks outside this chat on your computer\n\n"
        f"AI: {'Active' if gemini_client else 'Inactive'}"
    )

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    MEMORY_STORE.pop(f"chat_{update.effective_chat.id}", None)
    await update.message.reply_text("Memory cleared. Fresh start! \U0001F9F9")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not gemini_client:
        await update.message.reply_text("AI is not configured. Check GEMINI_API_KEY.")
        return

    status_msg = await update.message.reply_text("Thinking...")

    try:
        chat_id = update.effective_chat.id
        response = gemini_client.models.generate_content(
            model=MODEL,
            contents=build_contents(chat_id, text),
            config={"system_instruction": SYSTEM_PROMPT},
        )
        reply = response.text.strip()
        # save the exchange into this chat's memory
        key = f"chat_{chat_id}"
        history = context_history(key)
        history.append({"role": "user", "text": text})
        history.append({"role": "model", "text": reply})
        if len(history) > MEMORY_LIMIT:
            del history[:-MEMORY_LIMIT]
        set_context_history(key, history)
        await status_msg.edit_text(reply)
    except Exception as e:
        logger.error(f"AI error: {e}")
        await status_msg.edit_text("Sorry, something went wrong. Please try again.")

# --------------------------------------------------------------- main ---

def main():
    print("=" * 40)
    print("  LAFB_Bot - Cloud Version v2 (honest + memory + PPT)")
    print("=" * 40)
    print(f"  Token: {'OK' if BOT_TOKEN else 'MISSING!'}")
    print(f"  Gemini: {'OK' if gemini_client else 'MISSING!'}")
    print("=" * 40)

    if not BOT_TOKEN:
        print("ERROR: No bot token!")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("ppt", ppt_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("\nBot running! Message @LAFB_Bot\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    if not gemini_client:
        await update.message.reply_text("AI is not configured. Check GEMINI_API_KEY.")
        return
    
    status_msg = await update.message.reply_text("Thinking...")
    
    try:
        prompt = f"You are a helpful assistant. Answer concisely.\n\nUser: {text}"
        response = gemini_client.models.generate_content(
            model="gemini-3.5-flash-lite", 
            contents=prompt
        )
        await status_msg.edit_text(response.text.strip())
    except Exception as e:
        logger.error(f"AI error: {e}")
        await status_msg.edit_text("Sorry, something went wrong. Please try again.")

def main():
    print("=" * 40)
    print("  LAFB_Bot - Cloud Version")
    print("=" * 40)
    print(f"  Token: {'OK' if BOT_TOKEN else 'MISSING!'}")
    print(f"  Gemini: {'OK' if gemini_client else 'MISSING!'}")
    print("=" * 40)

    if not BOT_TOKEN:
        print("ERROR: No bot token!")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("\nBot running! Message @LAFB_Bot\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
