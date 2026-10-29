import asyncio
import sys
from telethon import TelegramClient
from telethon.sessions import StringSession
from config import config


async def main() -> None:
    print("=================================================================")
    print("         Telegram Music Addon - Interactive Login                ")
    print("=================================================================\n")

    api_id = config.TELEGRAM_API_ID
    api_hash = config.TELEGRAM_API_HASH

    if not api_id:
        val = input("Enter your TELEGRAM_API_ID (numbers only): ").strip()
        try:
            api_id = int(val)
        except ValueError:
            print("Error: TELEGRAM_API_ID must be a valid integer.")
            sys.exit(1)

    if not api_hash:
        api_hash = input("Enter your TELEGRAM_API_HASH: ").strip()

    if not api_id or not api_hash:
        print("Error: Both TELEGRAM_API_ID and TELEGRAM_API_HASH are required.")
        sys.exit(1)

    print("\nConnecting to Telegram to authorize your session...")
    client = TelegramClient(StringSession(), api_id, api_hash)

    try:
        await client.start()
        session_string = client.session.save()

        print("\n=================================================================")
        print("                      LOGIN SUCCESSFUL!                          ")
        print("=================================================================\n")
        print("Copy the following session string to your .env file as TELEGRAM_SESSION_STRING:\n")
        print(f"TELEGRAM_SESSION_STRING={session_string}\n")
        print("=================================================================")
        print("Keep this session string private and secure. Do not share it.\n")
    except Exception as exc:
        print(f"\nAuthentication failed: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
