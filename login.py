import asyncio
import os
import sys
from telethon import TelegramClient
from telethon.sessions import StringSession


def read_env(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    result = {}
    for line in open(path).read().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        idx = line.index('=') if '=' in line else -1
        if idx != -1:
            result[line[:idx].strip()] = line[idx+1:].strip().strip('"').strip("'")
    return result


def write_env(path: str, data: dict):
    lines = []
    for k, v in data.items():
        val = f'"{v}"' if ' ' in str(v) else str(v)
        lines.append(f"{k}={val}")
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


async def main() -> None:
    print("=================================================================")
    print("       Telegram Music Addon - Setup                              ")
    print("=================================================================")

    env = read_env(".env")

    has_api_id = "TELEGRAM_API_ID" in env and env["TELEGRAM_API_ID"]
    has_api_hash = "TELEGRAM_API_HASH" in env and env["TELEGRAM_API_HASH"]

    if not has_api_id or not has_api_hash:
        print("\nYou need Telegram API credentials.")
        print("If you don't have them:")
        print("  1. Go to https://my.telegram.org")
        print("  2. Log in with your phone number")
        print("  3. Click 'API development tools' -> create an app")
        print("  4. Copy the App api_id and api_hash\n")

    if not has_api_id:
        val = input("Enter your TELEGRAM_API_ID (numbers only): ").strip()
        try:
            api_id = int(val)
        except ValueError:
            print("Error: TELEGRAM_API_ID must be a valid integer.")
            sys.exit(1)
    else:
        try:
            api_id = int(env["TELEGRAM_API_ID"])
        except ValueError:
            print("Error: TELEGRAM_API_ID in .env must be a valid integer.")
            sys.exit(1)

    if not has_api_hash:
        api_hash = input("Enter your TELEGRAM_API_HASH: ").strip()
        if not api_hash:
            print("Error: TELEGRAM_API_HASH cannot be empty.")
            sys.exit(1)
    else:
        api_hash = env["TELEGRAM_API_HASH"].strip()
        if not api_hash:
            print("Error: TELEGRAM_API_HASH cannot be empty.")
            sys.exit(1)

    print("\nConnecting to Telegram...\n")

    client = TelegramClient(StringSession(""), api_id, api_hash)
    try:
        await client.start(
            phone=lambda: input("Your phone number (with country code, e.g. +91 for India, +1 for USA): "),
            code_callback=lambda: input("Verification code Telegram just sent to your app: "),
            password=lambda: input("Your 2FA password (press Enter if you don't have one): ") or None,
        )
    except Exception as exc:
        print(f"Error connecting to Telegram: {exc}")
        sys.exit(1)

    session_string = client.session.save()

    env["TELEGRAM_API_ID"] = str(api_id)
    env["TELEGRAM_API_HASH"] = api_hash
    env["TELEGRAM_SESSION_STRING"] = session_string

    if "TELEGRAM_CHANNEL" not in env or not env["TELEGRAM_CHANNEL"]:
        print("\nNow configure your music channel.")
        print("You can provide:")
        print("  - Public username (e.g. @mychannel)")
        print("  - Numeric channel ID (e.g. -1001234567890)")
        print("  - Or the exact channel name\n")
        channel = input("Enter your Telegram music channel: ").strip()
        if channel:
            env["TELEGRAM_CHANNEL"] = channel

    if "PORT" not in env or not env["PORT"]:
        env["PORT"] = "3000"

    write_env(".env", env)

    print("\n[✔] Configuration saved to .env")
    print("Start the server with: python main.py\n")

    await client.disconnect()
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
