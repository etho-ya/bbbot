#!/usr/bin/env python3
import os
import requests
import datetime
from config import config

def backup_db():
    db_path = config.DB_PATH
    if not os.path.exists(db_path):
        print(f"Error: DB not found at {db_path}")
        return

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendDocument"
    
    caption = f"🛡️ Risk Engine Daily DB Backup ({datetime.datetime.now().strftime('%Y-%m-%d')})\nIncludes signals, positions, and Dumalka Audit logs."
    
    with open(db_path, "rb") as f:
        files = {"document": ("signals.db", f)}
        data = {
            "chat_id": config.TELEGRAM_CHAT_ID,
            "caption": caption
        }
        try:
            r = requests.post(url, data=data, files=files, timeout=60)
            if r.status_code == 200:
                print("Backup sent successfully.")
            else:
                print(f"Failed to send backup: {r.status_code} {r.text}")
        except Exception as e:
            print(f"Exception during backup: {e}")

if __name__ == "__main__":
    backup_db()
