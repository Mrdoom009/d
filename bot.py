import os
import re
import asyncio
import threading
import logging
from html import unescape
from typing import List, Tuple, Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import Flask
from pyrogram import Client, filters, enums
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified

# ==================== CONFIGURATION ====================
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MAX_CONCURRENT = 3  # Number of concurrent URL fetches
REQUEST_TIMEOUT = 45
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ==================== LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== HEALTH CHECK ====================
health_app = Flask(__name__)

@health_app.route('/')
def health_check():
    return "OK", 200

threading.Thread(target=lambda: health_app.run(port=8080, host="0.0.0.0", debug=False, use_reloader=False), daemon=True).start()

# ==================== BOT CLIENT ====================
bot = Client("quiz_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# ==================== USER STATE MANAGEMENT ====================
class UserState:
    def __init__(self):
        self.urls: List[str] = []
        self.processing = False
        self.cancel_requested = False
        self.results: Dict[str, Any] = {}

user_states: Dict[int, UserState] = {}

def get_state(user_id: int) -> UserState:
    if user_id not in user_states:
        user_states[user_id] = UserState()
    return user_states[user_id]

def clear_state(user_id: int):
    if user_id in user_states:
        del user_states[user_id]

# ==================== PARSING FUNCTIONS ====================
def clean_question(text: str) -> str:
    text = unescape(text).strip()
    # Remove "प्रश्न X" or "Question X" prefix
    text = re.sub(r'^(प्रश्न|Question)\s*\d+[\s:.-]*', '', text, flags=re.IGNORECASE)
    return text.strip()

def extract_options_from_element(element) -> List[str]:
    """Extract option texts from any element containing list items or divs."""
    options = []
    # Try to find <li> first
    for li in element.find_all('li'):
        li_text = li.get_text(strip=True)
        match = re.match(r'^\(([\u0900-\u097F])\)\s*(.*)', li_text)  # Hindi letters
        if match:
            options.append(match.group(2).strip())
            continue
        match = re.match(r'^\(([a-d])\)\s*(.*)', li_text, re.IGNORECASE)  # English a-d
        if match:
            options.append(match.group(2).strip())
            continue
        # If no parentheses, just take the text (fallback)
        if not options and li_text:
            options.append(li_text)
    if options:
        return options

    # Fallback: look for divs with option-like classes or patterns
    for div in element.find_all('div', class_=re.compile(r'option|answer', re.I)):
        div_text = div.get_text(strip=True)
        if div_text:
            options.append(div_text)
    return options

def extract_answer_and_explanation(div) -> Tuple[Optional[str], Optional[str]]:
    if not div:
        return None, None
    full_text = div.get_text(separator=' ', strip=True)
    answer = None
    explanation = None
    # Extract answer after 'उत्तर :' or 'Answer :'
    answer_match = re.search(r'(?:उत्तर|Answer)\s*:\s*(.*?)(?:\s*(?:व्याख्या|Explanation)\s*:|$)', full_text, re.IGNORECASE)
    if answer_match:
        answer = answer_match.group(1).strip()
    # Extract explanation after 'व्याख्या :' or 'Explanation :'
    expl_match = re.search(r'(?:व्याख्या|Explanation)\s*:\s*(.*)', full_text, re.IGNORECASE)
    if expl_match:
        explanation = expl_match.group(1).strip()
    return answer, explanation

def extract_questions_from_html(html: str, url: str = "") -> Tuple[List[str], List[str]]:
    """Returns (main_questions, undetected_questions) as list of formatted strings."""
    soup = BeautifulSoup(html, 'html.parser')
    main_qs = []
    undetected_qs = []
    question_blocks = soup.find_all(['dl', 'div'], class_=re.compile(r'question', re.I))  # Fallback to any question-like container
    if not question_blocks:
        question_blocks = soup.find_all(['dl', 'div'])  # Last resort

    for block in question_blocks:
        # Try to find question text
        dt = block.find('dt') or block.find('h3') or block.find('p', class_=re.compile(r'question', re.I))
        if not dt:
            continue
        question_text = clean_question(dt.get_text())

        # Find options container (often a <ul> or <div> after question)
        dd = block.find('dd') or block.find('div', class_=re.compile(r'options?|answers?', re.I))
        if not dd:
            # If no dedicated container, search for list within block
            ul = block.find('ul')
            if ul:
                dd = ul.parent  # use parent as container
            else:
                continue

        options = extract_options_from_element(dd)
        if len(options) < 2:  # Need at least 2 options to be a valid question
            continue

        # Find explanation button/content
        button = dd.find('button', class_='collapsible') or block.find('button', text=re.compile(r'उत्तर|Answer', re.I))
        explanation_div = None
        if button:
            explanation_div = button.find_next_sibling('div', class_=re.compile(r'content|answer', re.I))
        if not explanation_div:
            # Look for a div containing "उत्तर" or "Answer"
            for div in block.find_all('div'):
                if re.search(r'उत्तर|Answer', div.get_text(), re.I):
                    explanation_div = div
                    break

        answer_text, explanation_text = extract_answer_and_explanation(explanation_div)

        # Determine correct option index by matching answer text with options
        correct_idx = None
        if answer_text:
            # Normalize answer text: remove extra spaces, punctuation
            norm_answer = re.sub(r'\s+', ' ', answer_text).strip().lower()
            for i, opt in enumerate(options):
                norm_opt = re.sub(r'\s+', ' ', opt).strip().lower()
                if norm_opt == norm_answer or norm_answer in norm_opt or norm_opt in norm_answer:
                    correct_idx = i
                    break

        # Build output (no question number prefix)
        lines = [question_text]
        for i, opt in enumerate(options[:4]):  # limit to first 4 options
            letter = chr(ord('a') + i)
            line = f"({letter}) {opt}"
            if i == correct_idx:
                line += " *"
            lines.append(line)
        if explanation_text:
            lines.append(f"Ex: {explanation_text}")

        block_text = "\n".join(lines)
        if correct_idx is None:
            undetected_qs.append(block_text)
        else:
            main_qs.append(block_text)

    return main_qs, undetected_qs

async def fetch_url(session: requests.Session, url: str, retries: int = 3) -> Optional[str]:
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            logger.warning(f"Attempt {attempt+1} failed for {url}: {e}")
            if attempt == retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)  # exponential backoff
    return None

# ==================== COMMAND HANDLERS ====================
@bot.on_message(filters.command("start"))
async def start_command(client, message: Message):
    text = (
        "✨ **Welcome to Quiz Extractor Bot!**\n\n"
        "I can extract quiz questions from webpages in a specific format and provide them as text files.\n\n"
        "**Commands:**\n"
        "/extract – Start the extraction process\n"
        "/cancel – Cancel current operation\n\n"
        "**How it works:**\n"
        "1. Send /extract\n"
        "2. Paste one or more URLs (one per line)\n"
        "3. Confirm to begin extraction\n"
        "4. Receive two files: `questions_main.txt` (with detected answers) and `questions_undetected.txt` (where answer could not be determined)."
    )
    await message.reply(text, parse_mode=enums.ParseMode.MARKDOWN)

@bot.on_message(filters.command("cancel"))
async def cancel_command(client, message: Message):
    user_id = message.from_user.id
    state = get_state(user_id)
    if state.processing:
        state.cancel_requested = True
        await message.reply("⏸️ Cancellation requested... Please wait.")
    else:
        clear_state(user_id)
        await message.reply("✅ No active process. State cleared.")

@bot.on_message(filters.command("extract"))
async def extract_command(client, message: Message):
    user_id = message.from_user.id
    state = get_state(user_id)
    if state.processing:
        await message.reply("⚠️ You already have an extraction in progress. Use /cancel to stop it first.")
        return
    clear_state(user_id)  # start fresh
    state = get_state(user_id)
    state.processing = True  # Mark as in URL collection phase
    await message.reply(
        "📝 **Send me the URLs** (one per line) from which you want to extract questions.\n\n"
        "Example:\n"
        "`https://example.com/page1`\n"
        "`https://example.com/page2`\n\n"
        "You can also send me just one URL.\n"
        "Type /cancel to abort.",
        parse_mode=enums.ParseMode.MARKDOWN
    )

@bot.on_message(filters.text & filters.private)
async def handle_text(client, message: Message):
    user_id = message.from_user.id
    state = get_state(user_id)
    # Only process if user is in URL collection phase (processing True but not actively fetching)
    if not state.processing or state.cancel_requested:
        return

    # Collect URLs
    lines = message.text.strip().splitlines()
    urls = [line.strip() for line in lines if line.strip()]
    if not urls:
        await message.reply("No URLs found. Please send at least one valid URL.")
        return

    # Validate URLs
    valid_urls = []
    invalid = []
    for url in urls:
        if url.startswith(('http://', 'https://')):
            valid_urls.append(url)
        else:
            invalid.append(url)

    if not valid_urls:
        await message.reply("❌ No valid URLs found. Please send URLs starting with http:// or https://")
        return

    state.urls = valid_urls
    # Show preview and ask for confirmation
    preview = "\n".join(valid_urls[:5])
    if len(valid_urls) > 5:
        preview += f"\n... and {len(valid_urls)-5} more"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="confirm_extract"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel_extract")]
    ])
    await message.reply(
        f"🔍 **Found {len(valid_urls)} URL(s)**\n\n{preview}\n\nProceed with extraction?",
        reply_markup=keyboard,
        parse_mode=enums.ParseMode.MARKDOWN
    )

@bot.on_callback_query()
async def callback_handler(client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    state = get_state(user_id)
    data = callback_query.data

    if data == "confirm_extract":
        if not state.urls:
            await callback_query.answer("No URLs to process.", show_alert=True)
            return
        await callback_query.message.edit_text("⏳ Starting extraction... This may take a while.")
        await callback_query.answer()
        # Process in background to avoid blocking callback
        asyncio.create_task(process_urls(client, callback_query.message, user_id))

    elif data == "cancel_extract":
        clear_state(user_id)
        await callback_query.message.edit_text("🚫 Extraction cancelled.")
        await callback_query.answer()

async def process_urls(client, status_message: Message, user_id: int):
    state = get_state(user_id)
    urls = state.urls.copy()
    total = len(urls)
    all_main = []
    all_undetected = []
    failed = []

    # Create a requests session with headers
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})

    try:
        for idx, url in enumerate(urls, 1):
            if state.cancel_requested:
                await status_message.edit_text(f"⏸️ Cancelled at URL {idx}/{total}.")
                break

            # Update status
            try:
                await status_message.edit_text(
                    f"🔄 **Processing** {idx}/{total}\n"
                    f"`{url[:60]}...`\n\n"
                    f"Main: {len(all_main)} | Undetected: {len(all_undetected)}"
                )
            except MessageNotModified:
                pass

            try:
                html = await fetch_url(session, url)
                if html is None:
                    failed.append(f"{url} (failed after retries)")
                    continue
                main_qs, undetected_qs = extract_questions_from_html(html, url)
                if main_qs:
                    all_main.extend(main_qs)
                if undetected_qs:
                    all_undetected.extend(undetected_qs)
            except Exception as e:
                logger.exception(f"Error processing {url}")
                failed.append(f"{url} ({str(e)})")

            # Small delay to be gentle
            await asyncio.sleep(0.5)

        # Prepare final message
        final_text = f"✅ **Extraction completed!**\n"
        final_text += f"• Main questions: {len(all_main)}\n"
        final_text += f"• Undetected: {len(all_undetected)}\n"
        if failed:
            final_text += f"• Failed: {len(failed)}"

        await status_message.edit_text(final_text)

        # Send files
        sent_any = False
        if all_main:
            content = "\n\n".join(all_main)
            with open(f"main_{user_id}.txt", "w", encoding="utf-8") as f:
                f.write(content)
            await client.send_document(
                chat_id=user_id,
                document=f"main_{user_id}.txt",
                caption=f"✅ **Main Questions** ({len(all_main)})"
            )
            os.remove(f"main_{user_id}.txt")
            sent_any = True

        if all_undetected:
            content = "\n\n".join(all_undetected)
            with open(f"undetected_{user_id}.txt", "w", encoding="utf-8") as f:
                f.write(content)
            await client.send_document(
                chat_id=user_id,
                document=f"undetected_{user_id}.txt",
                caption=f"⚠️ **Undetected Questions** ({len(all_undetected)})"
            )
            os.remove(f"undetected_{user_id}.txt")
            sent_any = True

        if failed:
            fail_msg = "**Failed URLs:**\n" + "\n".join(failed)
            # Split if too long
            if len(fail_msg) > 4096:
                parts = [fail_msg[i:i+4096] for i in range(0, len(fail_msg), 4096)]
                for part in parts:
                    await client.send_message(user_id, part)
            else:
                await client.send_message(user_id, fail_msg)

        if not sent_any and not failed:
            await client.send_message(user_id, "⚠️ No questions were extracted from any URL.")

    finally:
        # Clean up state
        clear_state(user_id)
        session.close()

# ==================== START BOT ====================
bot.run()
