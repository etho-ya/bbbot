#!/bin/bash
# Create Telethon session (code will be sent to Telegram app if TELEGRAM_PHONE is set in .env)
set -e
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  python3 -m venv venv
  venv/bin/pip install --upgrade pip
  venv/bin/pip install -r requirements.txt
fi

venv/bin/python3 create_session.py
