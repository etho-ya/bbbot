#!/usr/bin/env python3
"""Create Telethon session. Run from project root: ./create_session.sh or venv/bin/python3 create_session.py
   Set TELEGRAM_PHONE in .env so the code is sent to the Telegram app (chat «Telegram»)."""
import asyncio
import sys
import os
from pathlib import Path

# Guarantee .env is loaded from project root (script directory)
_script_dir = Path(__file__).resolve().parent
os.chdir(_script_dir)
if not os.path.isfile(".env"):
    print("Error: .env not found in", _script_dir)
    sys.exit(1)

# Load env before app imports (pydantic will use cwd == project root)
from dotenv import load_dotenv
load_dotenv(".env")

from telethon import TelegramClient
from telethon import types
from telethon.tl.functions.auth import SendCodeRequest
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from app.core.config import settings, get_telegram_session_path


def _build_proxy():
    raw = getattr(settings, "TELEGRAM_PROXY", None) or os.environ.get("TELEGRAM_PROXY") or ""
    raw = raw.strip()
    if not raw:
        return None
    try:
        import socks
        host, port = raw.rsplit(":", 1)
        proxy = (socks.SOCKS5, host, int(port))
        print(f"Using SOCKS5 proxy: {host}:{port}")
        return proxy
    except Exception as e:
        print(f"Warning: Invalid TELEGRAM_PROXY={raw!r} ({e}), connecting directly")
        return None


def get_phone() -> str:
    raw = (getattr(settings, "TELEGRAM_PHONE", None) or os.environ.get("TELEGRAM_PHONE") or "").strip()
    if raw:
        return raw.replace(" ", "").replace("-", "")
    return (input("Enter phone (e.g. +79...): ").strip().replace(" ", "").replace("-", "") or "")


async def main():
    session_path = get_telegram_session_path()
    api_id = settings.TELEGRAM_API_ID
    api_hash = settings.TELEGRAM_API_HASH
    phone = get_phone()
    if not phone:
        print("Error: phone required. Set TELEGRAM_PHONE in .env or enter when prompted.")
        sys.exit(1)
    print(f"Phone: {phone}")
    print(f"Session file: {session_path}.session")

    client = TelegramClient(session_path, api_id, api_hash, proxy=_build_proxy())
    try:
        await client.connect()
    except Exception as e:
        print(f"Connection error: {e}")
        sys.exit(1)

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized: {me.first_name} (@{me.username})")
        await client.disconnect()
        return

    # CodeSettings: allow_app_hash + current_number = code in Telegram app (not SMS)
    # Without allow_firebase so server doesn't prefer Firebase (official apps only)
    code_settings = types.CodeSettings(
        allow_flashcall=True,
        current_number=True,
        allow_app_hash=True,
        allow_missed_call=True,
    )
    try:
        result = await client(SendCodeRequest(phone, api_id, api_hash, code_settings))
    except FloodWaitError as e:
        print(f"Telegram flood wait: {e.seconds} s. Try again later.")
        await client.disconnect()
        sys.exit(1)
    except Exception as e:
        print(f"SendCode error: {e}")
        await client.disconnect()
        sys.exit(1)

    if type(result).__name__ == "SentCodeSuccess":
        print("Already logged in after send_code.")
        await client.disconnect()
        return
    if type(result).__name__ == "SentCodePaymentRequired":
        print("Telegram requires payment for this app. Cannot continue.")
        await client.disconnect()
        sys.exit(1)

    phone_code_hash = result.phone_code_hash
    if result.phone_code_hash:
        client._phone_code_hash[phone] = result.phone_code_hash
    client._phone = phone

    def ask_code() -> str:
        print()
        print(f"Code is sent to the Telegram app on the phone {phone} (not Desktop):")
        print("  Chats → first chat «Telegram» (service) → last message = 5-digit code.")
        print("  No code? Wait 1–2 min or press Enter to resend.")
        print("  (If code never appears: ensure Telegram on this phone is logged in as", phone + ".)")
        print()
        return (input("Enter 5-digit code (or Enter to resend): ").strip() or "").replace(" ", "")

    print(f"Code sent (type: {result.type}).")
    code = ask_code()
    while not code:
        try:
            result = await client(ResendCodeRequest(phone, phone_code_hash))
            if getattr(result, "phone_code_hash", None):
                phone_code_hash = result.phone_code_hash
                client._phone_code_hash[phone] = phone_code_hash
            print("Code resent. Check the «Telegram» chat again.")
        except FloodWaitError as e:
            print(f"Wait {e.seconds} s before resend.")
        except Exception as e:
            print(f"Resend error: {e}")
        code = ask_code()

    try:
        await client.sign_in(phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        pwd = input("2FA password: ").strip()
        if not pwd:
            print("No password entered.")
            try:
                await client.disconnect()
            except Exception:
                pass
            sys.exit(1)
        await client.sign_in(password=pwd)
    except Exception as e:
        print(f"Sign in error: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        sys.exit(1)

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Success: {me.first_name} (@{me.username})")
        print(f"Session: {session_path}.session")
    else:
        print("Authorization failed")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
