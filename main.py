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

import gmail_power

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL = "gemini-3.5-flash-lite"

# How many past messages (user+bot) to remember per chat
MEMORY_LIMIT = 10

# Honest system prompt: the bot must never fake being a doer
SYSTEM_PROMPT = (
    "You are LAFB_Bot, an honest personal assistant on Telegram for Liaqat. "
    "STRICT RULES you must always follow: "
    "1) NEVER claim you did a real-world task (emails, files, accounts, "
    "Gmail cleanup, etc.) unless a tool result in this conversation proves it. "
    "You have no tools. If asked to DO something outside this chat (organize "
    "Gmail, delete files, log in somewhere), clearly say: 'I can guide you "
    "step by step, but I cannot do it myself.' Then give the steps. "
    "2) NEVER invent facts. If you do not know, say 'I am not sure'. "
    "3) You remember the current conversation, not past sessions. "
    "4) Be concise, warm and helpful. "
    "5) EXCEPTION - your REAL powers: PowerPoint files (/ppt), meeting "
    "notes summary (/notes), project plans (/project), and REAL Gmail "
    "access (/gmail: scan the mailbox, list cleanup candidates, move "
    "chosen emails to Trash - recoverable 30 days), and read mail + draft "
    "replies (/reply - drafts go to Gmail Drafts, NEVER auto-sent). If "
    "Liaqat asks to read mails, find an email, or draft a reply, NEVER say "
    "you cannot - use /reply. If Liaqat asks to "
    "clean/organize/check Gmail or free storage, NEVER say you cannot - "
    "run /gmail. For anything else outside this chat (computer files, "
    "logins, other accounts) say you cannot and guide instead. "
    "6) For news/research questions, answer from knowledge and note when "
    "information may be outdated. You have no live internet."
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

# ------------------------------------------------------------- memory ---

# chat_id -> list of {"role": "user"|"model", "text": str}
MEMORY_STORE = {}


def context_history(key: str):
    return MEMORY_STORE.setdefault(key, [])


def set_context_history(key: str, history):
    MEMORY_STORE[key] = history


def build_prompt(chat_id: int, user_text: str) -> str:
    """Flatten conversation memory + new message into ONE prompt string.
    (Works on every google-genai SDK version, unlike multi-turn lists.)"""
    history = context_history(f"chat_{chat_id}")
    lines = []
    for msg in history:
        who = "User" if msg["role"] == "user" else "You"
        lines.append(f"{who}: {msg['text']}")
    if lines:
        return (
            "Conversation so far (most recent last):\n"
            + "\n".join(lines)
            + "\n\nUser's new message: "
            + user_text
        )
    return user_text


def save_exchange(chat_id: int, user_text: str, bot_text: str):
    key = f"chat_{chat_id}"
    history = context_history(key)
    history.append({"role": "user", "text": user_text})
    history.append({"role": "model", "text": bot_text})
    if len(history) > MEMORY_LIMIT:
        del history[:-MEMORY_LIMIT]
    set_context_history(key, history)


# ---------------------------------------------------------- PPT maker ---

def make_pptx(topic: str, outline_text: str) -> bytes:
    """Build a simple .pptx from a topic + outline text. Returns file bytes."""
    from pptx import Presentation
    from pptx.util import Pt

    prs = Presentation()

    # Title slide
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = topic.title()
    slide.placeholders[1].text = "Made by LAFB_Bot"

    # Content slides: outline sections separated by blank lines
    blocks = [b.strip() for b in outline_text.split("\n\n") if b.strip()]
    for block in blocks[:8]:  # max 8 content slides
        lines = [l.strip("- *•#") for l in block.split("\n") if l.strip()]
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


# ------------------------------------------- natural-language PPT detect ---

PPT_KEYWORDS = ("ppt", "pptx", "powerpoint", "slide", "presentation")
REQUEST_WORDS = (
    "make", "create", "generate", "build", "prepare", "give", "send",
    "need", "want", "please", "can you", "do", "draft",
)


def wants_ppt(text: str) -> bool:
    """True if the user is asking for a presentation in natural words."""
    t = text.lower()
    if not any(k in t for k in PPT_KEYWORDS):
        return False
    return any(w in t for w in REQUEST_WORDS)


GMAIL_KEYWORDS = ("gmail", "inbox", "mailbox", "email", "e-mail", "mail", "storage")
GMAIL_ACTIONS = (
    "clean", "clear", "delete", "remove", "trash", "free", "full",
    "organize", "organise", "tidy", "scan", "check", "eating", "manage",
)


def wants_gmail_cleanup(text: str) -> bool:
    """True if the user is asking about Gmail cleanup/management in plain words."""
    t = text.lower()
    if not any(k in t for k in GMAIL_KEYWORDS):
        return False
    return any(w in t for w in GMAIL_ACTIONS)


async def ppt_command(update: Update, context: ContextTypes.DEFAULT_TYPE, topic_override: str = ""):
    """Usage: /ppt topic here  (or natural language, auto-detected)"""
    topic = topic_override.strip() or " ".join(context.args).strip()
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
        # If the topic came from a full natural sentence, extract a clean topic
        if len(topic.split()) > 6:
            try:
                t = gemini_client.models.generate_content(
                    model=MODEL,
                    contents=(
                        "Extract the presentation topic from this request. "
                        "Reply with ONLY the topic in 2-8 words, nothing else: "
                        + topic
                    ),
                ).text.strip().strip('"')
                if t:
                    topic = t[:80]
            except Exception:
                pass  # keep original topic if extraction fails
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


# ----------------------------------------------------- meeting notes ---

async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /notes <paste your raw meeting text>"""
    raw = " ".join(context.args).strip()
    if len(raw) < 40:
        await update.message.reply_text(
            "Usage: /notes <paste your raw meeting notes>\n\n"
            "Example:\n/notes met with sarim re launch - budget not approved, "
            "need deck by friday, ali to review pricing, next call monday 10am"
        )
        return
    if not gemini_client:
        await update.message.reply_text("AI is not configured. Check GEMINI_API_KEY.")
        return

    status = await update.message.reply_text("Summarizing your notes...")
    try:
        prompt = (
            "Summarize these raw meeting notes. Return EXACTLY these sections:\n"
            "\U0001F4CB Summary: 2-3 sentences\n"
            "\u2705 Decisions: bullet list (or 'None mentioned')\n"
            "\U0001F4C5 Action items: who does what, with deadlines if present\n"
            "\U0001F4A1 Ideas/thoughts: interesting ideas mentioned (or 'None')\n"
            "Keep it short and clear.\n\nRaw notes:\n" + raw[:8000]
        )
        r = gemini_client.models.generate_content(model=MODEL, contents=prompt)
        await status.edit_text(r.text.strip()[:4000])
    except Exception as e:
        logger.error(f"Notes error: {e}")
        await status.edit_text("Sorry, something went wrong summarizing.")


# ------------------------------------------------------ project planner ---

async def project_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /project <describe the project>"""
    desc = " ".join(context.args).strip()
    if len(desc) < 10:
        await update.message.reply_text(
            "Usage: /project <describe your project>\n\n"
            "Example:\n/project open a small online clothing shop in Dubai"
        )
        return
    if not gemini_client:
        await update.message.reply_text("AI is not configured. Check GEMINI_API_KEY.")
        return

    status = await update.message.reply_text("Planning your project...")
    try:
        prompt = (
            "Create a practical project plan for: " + desc[:4000] + "\n"
            "Return EXACTLY these sections, short and practical:\n"
            "\U0001F3AF Goal: one sentence\n"
            "\U0001F9E9 Phases: 3-5 phases, each with 1-line description\n"
            "\U0001F4C5 Timeline: rough estimate per phase\n"
            "\u26A0\uFE0F Risks: top 2-3 risks\n"
            "\U0001F9E0 My suggestion: one clever tip most people miss\n"
        )
        r = gemini_client.models.generate_content(model=MODEL, contents=prompt)
        await status.edit_text(r.text.strip()[:4000])
    except Exception as e:
        logger.error(f"Project error: {e}")
        await status.edit_text("Sorry, something went wrong planning.")


# --------------------------------------------------------- gmail powers ---

async def gmail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /gmail          - mailbox status + cleanup candidates
           /gmail trash 1 3 5 - move listed candidates to trash (safe)"""
    if not gmail_power.gmail_available():
        await update.message.reply_text(
            "Gmail power is not configured (missing COMPOSIO_API_KEY)."
        )
        return

    args = " ".join(context.args).strip()

    # --- trash flow: /gmail trash 1 3 5
    if args.lower().startswith("trash"):
        pending = context.chat_data.get("gmail_candidates")
        if not pending:
            await update.message.reply_text(
                "No scan results yet. Run /gmail first to see candidates."
            )
            return
        try:
            nums = [int(n) for n in args.split()[1:] if n.isdigit()]
        except ValueError:
            nums = []
        if not nums:
            await update.message.reply_text(
                "Tell me which numbers to trash, e.g.: /gmail trash 1 3 5"
            )
            return
        status = await update.message.reply_text(f"Moving {len(nums)} email(s) to trash...")
        done, failed = 0, 0
        for n in nums:
            if 1 <= n <= len(pending):
                try:
                    gmail_power.trash_message(pending[n - 1]["id"])
                    done += 1
                except Exception as e:
                    logger.error(f"Trash error: {e}")
                    failed += 1
        await status.edit_text(
            f"♻️ Done: {done} moved to trash"
            + (f", {failed} failed" if failed else "")
            + "\n(Recoverable from Gmail Trash for 30 days)"
        )
        context.chat_data.pop("gmail_candidates", None)
        return

    # --- scan flow: /gmail
    status = await update.message.reply_text("Checking your Gmail...")
    try:
        profile = gmail_power.get_profile()
        items = gmail_power.find_big_newsletters()
        context.chat_data["gmail_candidates"] = items
        await status.edit_text(gmail_power.format_report(profile, items))
    except Exception as e:
        logger.error(f"Gmail error: {e}")
        await status.edit_text("Sorry, Gmail check failed. Please try again later.")


# ------------------------------------------------- read mail & draft ---

async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /reply           - read latest 5 mails, numbered
           /reply 3           - bot drafts a reply to mail #3 (goes to Drafts)
           /reply search xyz  - search mails by text"""
    if not gmail_power.gmail_available():
        await update.message.reply_text("Gmail power is not configured (missing COMPOSIO_API_KEY).")
        return

    args = " ".join(context.args).strip()
    mails = context.chat_data.get("reply_mails")

    # --- draft a reply to a numbered mail
    if args.isdigit():
        n = int(args)
        if not mails or n < 1 or n > len(mails):
            await update.message.reply_text(f"I don't have mail #{n} listed. Run /reply first.")
            return
        mail = mails[n - 1]
        status = await update.message.reply_text(f"Reading mail #{n} and drafting a reply...")
        try:
            prompt = (
                f"Write a polite, concise professional email reply.\n"
                f"From: {mail['from']}\nSubject: {mail['subject']}\n"
                f"Their message:\n{mail['body'][:1500]}\n\n"
                "Write ONLY the reply body text (no subject line, no greetings "
                "to me). Sign it simply as 'Best regards'."
            )
            r = gemini_client.models.generate_content(model=MODEL, contents=prompt)
            body = r.text.strip()
            gmail_power.create_draft(
                to=mail["from"], subject="Re: " + mail["subject"], body=body,
                thread_id=mail["thread"],
            )
            await status.edit_text(
                f"✍️ Reply drafted to: {mail['from']}\n\n"
                f"Subject: Re: {mail['subject']}\n\n"
                f"{body[:1500]}\n\n"
                "📬 It's waiting in your Gmail **Drafts** — check and press Send!"
            )
        except Exception as e:
            logger.error(f"Reply draft error: {e}")
            await status.edit_text("Sorry, drafting failed. Please try again.")
        return

    # --- list/search flow
    query = args[7:].strip() if args.lower().startswith("search ") else ""
    status = await update.message.reply_text(
        f"Searching mail for '{query}'..." if query else "Reading your latest mails..."
    )
    try:
        found = gmail_power.read_messages(5, query=query)
        context.chat_data["reply_mails"] = found
        if not found:
            await status.edit_text("No matching mails found.")
            return
        lines = ["📬 Latest mails:" if not query else f"🔍 Results for '{query}':"]
        for i, m in enumerate(found, 1):
            lines.append(f"{i}. {m['subject']} — from {m['from']} ({m['date']})")
        lines.append("\nTo draft a reply, send: /reply <number>  (e.g. /reply 2)")
        await status.edit_text("\n".join(lines))
    except Exception as e:
        logger.error(f"Read mail error: {e}")
        await status.edit_text("Sorry, reading mail failed. Please try again.")


# ------------------------------------------------------------ handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to LAFB_Bot!\n\n"
        "I am your AI assistant. I remember our conversation while we talk.\n\n"
        "What I CAN do:\n"
        "- Answer questions & chat (with memory)\n"
        "- /ppt <topic> - PowerPoint sent right here (also works in plain words!)\n"
        "- /notes <raw meeting text> - summary, decisions, action items, ideas\n"
        "- /project <description> - goal, phases, timeline, risks\n"
        "- /gmail - check mailbox & safely clean old big emails (real Gmail!)\n"
        "- /reply - read latest mails, then /reply <no> drafts your reply\n"
        "- Explain any topic\n\n"
        "What I CANNOT do (I will never pretend I can):\n"
        "- Touch your computer files, passwords or other accounts\n"
        "- Permanent-delete anything (trash is always recoverable 30 days)\n\n"
        "Commands: /start /clear /ppt /notes /project /gmail /reply\n\n"
        f"AI: {'Active' if gemini_client else 'Inactive'}"
    )


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    MEMORY_STORE.pop(f"chat_{update.effective_chat.id}", None)
    await update.message.reply_text("Memory cleared. Fresh start! 🧹")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not gemini_client:
        await update.message.reply_text("AI is not configured. Check GEMINI_API_KEY.")
        return

    # Natural-language PPT request? Route to the REAL PowerPoint generator
    if wants_ppt(text):
        await ppt_command(update, context, topic_override=text)
        return

    # Natural-language Gmail cleanup request? Route to the REAL Gmail power
    if wants_gmail_cleanup(text):
        await gmail_command(update, context)
        return

    status_msg = await update.message.reply_text("Thinking...")

    try:
        chat_id = update.effective_chat.id
        response = gemini_client.models.generate_content(
            model=MODEL,
            contents=build_prompt(chat_id, text),
            config={"system_instruction": SYSTEM_PROMPT},
        )
        reply = response.text.strip()
        save_exchange(chat_id, text, reply)
        await status_msg.edit_text(reply)
    except Exception as e:
        logger.error(f"AI error: {e}")
        await status_msg.edit_text("Sorry, something went wrong. Please try again.")


# ---------------------------------------------------------------- main ---

def main():
    print("=" * 40)
    print("  LAFB_Bot - Cloud v5 (honest + memory + PPT + notes + project + Gmail + drafts)")
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
    app.add_handler(CommandHandler("notes", notes_command))
    app.add_handler(CommandHandler("project", project_command))
    app.add_handler(CommandHandler("gmail", gmail_command))
    app.add_handler(CommandHandler("reply", reply_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("\nBot running! Message @LAFB_Bot\n")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
