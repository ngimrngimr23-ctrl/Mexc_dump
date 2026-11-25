"""Одноразово получить строку сессии для автопроверки халяльности.

Запускается у тебя, не на Render и не в репозитории:
    pip install telethon
    python make_session.py

Спросит api_id и api_hash (my.telegram.org → API development tools), потом
номер телефона, код из Telegram и пароль двухэтапной проверки, если он есть.
В конце напечатает строку — её в Render → Environment как TG_SESSION.

ВАЖНО: строка сессии даёт полный доступ к аккаунту. Никому её не показывай,
не коммить в git и не вставляй в чаты. Лучше заведи для этого отдельный аккаунт.
"""

import asyncio

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(input("api_id: ").strip())
    api_hash = input("api_hash: ").strip()
    # async with сам спросит телефон, код и пароль 2FA
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = await client.get_me()
        print("\nВошёл как", "@" + me.username if me.username else me.id)
        print("\nTG_SESSION (скопируй целиком, это секрет):\n")
        print(client.session.save())
        print("\nTG_API_ID =", api_id)
        print("TG_API_HASH = (тот, что ты ввёл)")


asyncio.run(main())
