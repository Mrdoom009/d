import os
import re
import asyncio
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
    text = unescape(text).strip()
    return re.sub(r'^प्रश्न\s+\d+\s*', '', text).strip()

def extract_options_from_ul(ul) -> List[str]:
    options = []
    for li in ul.find_all('li'):
        li_text = li.get_text(strip=True)
        match = re.match(r'^\(([\u0900-\u097F])\)\s*(.*)', li_text)
        if match:
            opt_text = match.group(2).strip()
            options.append(opt_text)
    return options

def extract_answer_and_explanation(div) -> Tuple[Optional[str], Optional[str]]:
    if not div:
        return None, None
    full_text = div.get_text(separator=' ', strip=True)
    answer = None
    explanation = None
    answer_match = re.search(r'उत्तर\s*:\s*(.*?)(?:\s*व्याख्या\s*:|$)', full_text, re.IGNORECASE)
    if answer_match:
        answer = answer_match.group(1).strip()
    expl_match = re.search(r'व्याख्या\s*:\s*(.*)', full_text, re.IGNORECASE)
    if expl_match:
        explanation = expl_match.group(1).strip()
    return answer, explanation

def extract_questions_from_html(html: str) -> Tuple[List[str], List[str]]:
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
        button = dd.find('button', class_='collapsible')
        explanation_div = button.find_next_sibling('div', class_='rg-c-content') if button else None
        answer_text, explanation_text = extract_answer_and_explanation(explanation_div)
        correct_idx = None
        if answer_text:
            normalized_answer = answer_text.strip()
            for i, opt in enumerate(options):
                if opt == normalized_answer:
                    correct_idx = i
                    break
        lines = [question_text]
        for i, opt in enumerate(options):
            letter = chr(ord('a') + i)
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

async def fetch_url(url: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: requests.get(url, timeout=30, allow_redirects=True).text)

@bot.on_message(filters.command("start"))
async def start_command(client, message: Message):
    await message.reply(
        "📤 Send me one or more URLs (one per line) containing quiz questions.\n"
        "I'll extract all questions and return two files:\n"
        "• `questions_main.txt` – questions with detected correct answers.\n"
        "• `questions_undetected.txt` – questions where correct answer could not be detected."
    )

@bot.on_message(filters.text & filters.private)
async def handle_urls(client, message: Message):
    text = message.text.strip()
    urls = [line.strip() for line in text.splitlines() if line.strip()]
    if not urls:
        await message.reply("Please send at least one URL.")
        return
    status_msg = await message.reply(f"⏳ Processing {len(urls)} URL(s)...")
    all_main = []
    all_undetected = []
    failed = []
    for i, url in enumerate(urls, 1):
        if not url.startswith(('http://', 'https://')):
            failed.append(f"{url} (invalid)")
            continue
        try:
            await status_msg.edit_text(f"⏳ Fetching URL {i}/{len(urls)}: {url[:50]}...")
            html = await fetch_url(url)
            main_qs, undetected_qs = extract_questions_from_html(html)
            if main_qs:
                all_main.extend(main_qs)
            if undetected_qs:
                all_undetected.extend(undetected_qs)
        except Exception as e:
            failed.append(f"{url} ({str(e)})")
    if not all_main and not all_undetected and not failed:
        await status_msg.edit_text("❌ No questions found on any URL.")
        return
    sent_any = False
    if all_main:
        main_content = "\n\n".join(all_main)
        with open("questions_main.txt", "w", encoding="utf-8") as f:
            f.write(main_content)
        await message.reply_document("questions_main.txt", caption=f"✅ Main questions ({len(all_main)} total)")
        os.remove("questions_main.txt")
        sent_any = True
    if all_undetected:
        undetected_content = "\n\n".join(all_undetected)
        with open("questions_undetected.txt", "w", encoding="utf-8") as f:
            f.write(undetected_content)
        await message.reply_document("questions_undetected.txt", caption=f"⚠️ Undetected questions ({len(all_undetected)} total)")
        os.remove("questions_undetected.txt")
        sent_any = True
    if failed:
        fail_msg = "Failed URLs:\n" + "\n".join(failed)
        await message.reply(fail_msg[:4096])
    if sent_any:
        await status_msg.delete()
    else:
        await status_msg.edit_text("⚠️ No questions extracted, but some URLs failed.")

bot.run()
