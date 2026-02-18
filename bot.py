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

health_app = Flask(__name__)

@health_app.route('/')
def health_check():
    return "OK", 200

threading.Thread(target=lambda: health_app.run(port=8080, host="0.0.0.0", debug=False, use_reloader=False), daemon=True).start()

HINDI_TO_ENGLISH = {
    'अ': 'a', 'ब': 'b', 'स': 'c', 'द': 'd',
}

def clean_question(text: str) -> str:
    text = unescape(text).strip()
    return re.sub(r'^प्रश्न\s+\d+\s*', '', text).strip()

def parse_option(li_text: str) -> Tuple[Optional[str], str]:
    match = re.match(r'^\(([अ-द])\)\s*(.*)', li_text.strip())
    if not match:
        return None, li_text.strip()
    hindi_letter, opt_text = match.groups()
    eng_letter = HINDI_TO_ENGLISH.get(hindi_letter)
    return eng_letter, opt_text.strip()

def extract_answer_and_explanation(div) -> Tuple[Optional[str], Optional[str]]:
    answer = None
    explanation = None
    for strong in div.find_all('strong'):
        strong_text = strong.get_text(strip=True)
        if strong_text.startswith('उत्तर :'):
            answer = strong_text.replace('उत्तर :', '', 1).strip()
        elif strong_text.startswith('व्याख्या :'):
            explanation = strong_text.replace('व्याख्या :', '', 1).strip()
    return answer, explanation

def extract_questions(html: str) -> Tuple[List[str], List[str]]:
    soup = BeautifulSoup(html, 'html.parser')
    main_questions = []
    undetected_questions = []
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
        options = []
        for li in ul.find_all('li'):
            li_text = li.get_text()
            letter, text = parse_option(li_text)
            if letter is None:
                continue
            options.append((letter, text))
        if not options:
            continue
        button = dd.find('button', class_='collapsible')
        explanation_div = button.find_next_sibling('div', class_='rg-c-content') if button else None
        answer_text = None
        explanation_text = None
        if explanation_div:
            answer_text, explanation_text = extract_answer_and_explanation(explanation_div)
        correct_letter = None
        if answer_text:
            normalized_answer = answer_text.strip()
            for letter, opt_text in options:
                if opt_text == normalized_answer:
                    correct_letter = letter
                    break
        lines = [question_text]
        for letter, opt_text in options:
            lines.append(f"({letter}) {opt_text}" + (" *" if letter == correct_letter else ""))
        if explanation_text:
            lines.append(f"Ex: {explanation_text}")
        question_block = "\n".join(lines)
        if correct_letter is None:
            undetected_questions.append(question_block)
        else:
            main_questions.append(question_block)
    return main_questions, undetected_questions

def fetch_url(url: str) -> str:
    resp = requests.get(url, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    return resp.text

@bot.on_message(filters.command("start"))
async def start_command(client, message: Message):
    await message.reply(
        "Send me a URL containing quiz questions in the required HTML format.\n"
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
    status_msg = await message.reply("Fetching and processing...")
    try:
        html = fetch_url(url)
        main_qs, undetected_qs = extract_questions(html)
        if not main_qs and not undetected_qs:
            await status_msg.edit_text("No questions found on the page.")
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
        await status_msg.edit_text(f"Error: {str(e)}")

bot.run()
