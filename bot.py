import os
import re
import threading
from html import unescape
from typing import List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
from flask import Flask
from pyrogram import Client, filters, enums
from pyrogram.types import Message

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

bot = Client("quiz_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# Health check server
health_app = Flask(__name__)
@health_app.route('/')
def health_check():
    return "OK", 200
threading.Thread(target=lambda: health_app.run(port=8080, host="0.0.0.0", debug=False, use_reloader=False), daemon=True).start()

def clean_question(text: str) -> str:
    """Remove 'प्रश्न X' prefix and clean."""
    text = unescape(text).strip()
    return re.sub(r'^प्रश्न\s+\d+\s*', '', text).strip()

def extract_options_from_ul(ul) -> List[str]:
    """Extract option texts from <ul> containing <li> with (अ) etc."""
    options = []
    for li in ul.find_all('li'):
        li_text = li.get_text(strip=True)
        # Match any Devanagari character between parentheses
        match = re.match(r'^\(([\u0900-\u097F])\)\s*(.*)', li_text)
        if match:
            opt_text = match.group(2).strip()
            options.append(opt_text)
    return options

def extract_answer_and_explanation(div) -> Tuple[Optional[str], Optional[str]]:
    """Extract answer and explanation from the div using regex on full text."""
    if not div:
        return None, None
    full_text = div.get_text(separator=' ', strip=True)
    answer = None
    explanation = None
    # Extract answer after 'उत्तर :'
    answer_match = re.search(r'उत्तर\s*:\s*(.*?)(?:\s*व्याख्या\s*:|$)', full_text, re.IGNORECASE)
    if answer_match:
        answer = answer_match.group(1).strip()
    # Extract explanation after 'व्याख्या :'
    expl_match = re.search(r'व्याख्या\s*:\s*(.*)', full_text, re.IGNORECASE)
    if expl_match:
        explanation = expl_match.group(1).strip()
    return answer, explanation

def extract_questions(html: str) -> Tuple[List[str], List[str]]:
    """Parse HTML and return (main_questions, undetected_questions) as text blocks."""
    soup = BeautifulSoup(html, 'html.parser')
    main_qs = []
    undetected_qs = []
    for dl in soup.find_all('dl'):
        dt = dl.find('dt')
        if not dt:
            continue
        question_text = clean_question(dt.get_text())
        dd = dl.find('dd')
        if not dd:
            continue
        ul = dd.find('ul')
        if not ul:
            continue
        options = extract_options_from_ul(ul)
        if not options:
            continue
        # Find explanation button and content
        button = dd.find('button', class_='collapsible')
        explanation_div = button.find_next_sibling('div', class_='rg-c-content') if button else None
        answer_text, explanation_text = extract_answer_and_explanation(explanation_div)
        # Determine correct option index (0-based)
        correct_idx = None
        if answer_text:
            normalized_answer = answer_text.strip()
            for i, opt in enumerate(options):
                if opt == normalized_answer:
                    correct_idx = i
                    break
        # Build output lines (no question number prefix)
        lines = [question_text]
        for i, opt in enumerate(options):
            letter = chr(ord('a') + i)  # a, b, c, d...
            line = f"({letter}) {opt}"
            if i == correct_idx:
                line += " *"
            lines.append(line)
        if explanation_text:
            lines.append(f"Ex: {explanation_text}")
        block = "\n".join(lines)
        if correct_idx is None:
            undetected_qs.append(block)
        else:
            main_qs.append(block)
    return main_qs, undetected_qs

def fetch_url(url: str) -> str:
    resp = requests.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    return resp.text

@bot.on_message(filters.command("start"))
async def start_command(client, message: Message):
    await message.reply(
        "📤 Send me a URL containing quiz questions in the required HTML format.\n"
        "I'll extract them and return two text files:\n"
        "• `questions_main.txt` – questions with detected correct answers.\n"
        "• `questions_undetected.txt` – questions where correct answer could not be detected."
    )

@bot.on_message(filters.text & filters.private)
async def handle_url(client, message: Message):
    url = message.text.strip()
    if not url.startswith(('http://', 'https://')):
        await message.reply("Please send a valid URL.")
        return
    status_msg = await message.reply("⏳ Fetching and processing...")
    try:
        html = fetch_url(url)
        main_qs, undetected_qs = extract_questions(html)
        if not main_qs and not undetected_qs:
            await status_msg.edit_text("❌ No questions found on the page.")
            return
        if main_qs:
            main_content = "\n\n".join(main_qs)
            with open("questions_main.txt", "w", encoding="utf-8") as f:
                f.write(main_content)
            await message.reply_document("questions_main.txt", caption="✅ Questions with detected correct answers.")
        if undetected_qs:
            undetected_content = "\n\n".join(undetected_qs)
            with open("questions_undetected.txt", "w", encoding="utf-8") as f:
                f.write(undetected_content)
            await message.reply_document("questions_undetected.txt", caption="⚠️ Questions where correct answer could not be detected.")
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"❌ Error: {str(e)}")

bot.run()
