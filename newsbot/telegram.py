import datetime as dt
import html
import logging
import re

from .http import ServiceError, request_json


class Telegram:
    def __init__(self, token, chat):
        if not re.fullmatch(r"(?:@[A-Za-z0-9_]{5,32}|-100[0-9]+)", chat):
            raise ServiceError("TELEGRAM_CHAT_ID must be a channel @username or numeric channel ID")
        self.token = token
        self.chat = chat
        self.username = None

    def call(self, method, **payload):
        data = request_json(f"https://api.telegram.org/bot{self.token}/{method}",
                            service=f"Telegram {method}", payload=payload)
        if not isinstance(data, dict) or data.get("ok") is not True or "result" not in data:
            raise ServiceError(f"Telegram {method} rejected the request")
        return data["result"]

    def check(self):
        bot = self.call("getMe")
        chat = self.call("getChat", chat_id=self.chat)
        username = chat.get("username") or ""
        if (chat.get("type") != "channel"
                or type(chat.get("id")) is not int or chat["id"] >= 0
                or (username and not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username))
                or (self.chat.startswith("@") and self.chat[1:].lower() != username.lower())
                or (self.chat.startswith("-") and int(self.chat) != chat["id"])):
            raise ServiceError("Configured Telegram chat is not the expected channel")
        member = self.call("getChatMember", chat_id=chat["id"], user_id=bot["id"])
        if member.get("status") != "administrator" or member.get("can_post_messages") is not True:
            raise ServiceError("Telegram bot lacks channel administrator posting rights")
        self.chat = chat["id"]
        self.username = username.lower() or None
        logging.info("Telegram bot authentication, target channel and posting permissions verified")

    def send(self, story):
        links = " · ".join(
            f'<a href="{html.escape(article.url)}">{html.escape(article.publisher)}</a>'
            for article in story.articles
        )
        title = html.escape(story.title)
        summary = html.escape(story.summary)
        result = self.call("sendMessage", chat_id=self.chat, parse_mode="HTML",
                           link_preview_options={"is_disabled": True},
                           text=f"<b>{title}</b>\n\n{summary}\n\nSources · {links}")
        if result["chat"]["id"] != self.chat:
            raise ServiceError("Telegram returned a message for an unexpected chat")
        return result["message_id"], dt.datetime.fromtimestamp(result["date"], dt.UTC)
