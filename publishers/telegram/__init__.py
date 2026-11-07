"""Telegram channel publisher: uploads the approved video through the Bot API."""

from __future__ import annotations

import os

from publishers.base import Publisher, check_response

# Bot API limit for files uploaded through the public api.telegram.org server.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class TelegramPublisher(Publisher):
    channel = "telegram"
    credential_env = "TELEGRAM_BOT_TOKEN"

    def _readiness_token(self):
        from publishers.oauth import _get_secret
        return _get_secret(self.credential_env)

    def _probe_request(self, token: str):
        from adapters.telegram import api_root
        return (f"{api_root()}/bot{token}/getMe", {})

    def channel_readiness(self, chat_id: str) -> dict:
        """The bot is valid and may post to this channel. Nothing is sent."""
        from adapters.telegram import api_root
        from publishers.base import publisher_mock_enabled
        from publishers.oauth import _get_secret

        state = self.readiness()
        if not state["ready"] or publisher_mock_enabled():
            return state
        refused = lambda reason: {**state, "ready": False, "reason": reason}  # noqa: E731
        if not chat_id:
            return refused("Вкажіть канал у Налаштування → Публікація")
        token = _get_secret(self.credential_env)
        try:
            response = self.transport.get(f"{api_root()}/bot{token}/getChatMember",
                                          params={"chat_id": chat_id, "user_id": token.split(":")[0]}, timeout=15)
        except Exception:  # noqa: BLE001 - a transport error can quote the URL with the token
            return refused("Платформа недоступна: доступ до каналу не перевірено")
        if response.status_code >= 400:
            return refused("Бот не має доступу до каналу або канал не знайдено")
        member = response.json().get("result") or {}
        if member.get("status") in {None, "left", "kicked"} or member.get("can_post_messages") is False:
            return refused("Бот не має права публікувати в каналі")
        return state

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        from adapters.telegram import api_root
        from publishers.oauth import _get_secret

        chat_id = metadata.get("telegram_chat_id") or metadata.get("target")
        if not chat_id:
            raise ValueError("Вкажіть канал у Налаштування → Публікація")
        size = os.path.getsize(video_path)
        if size > MAX_UPLOAD_BYTES:
            raise ValueError(f"Telegram Bot API accepts videos up to 50 MB, got {size} bytes")
        caption = metadata.get("description", "") or metadata.get("topic", "")
        with open(video_path, "rb") as video:
            response = self.transport.post(
                f"{api_root()}/bot{_get_secret(self.credential_env)}/sendVideo",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"video": video},
                timeout=600,
            )
        check_response(response)
        message = response.json().get("result") or {}
        message_id = message.get("message_id")
        username = (message.get("chat") or {}).get("username")
        receipt = {"channel": self.channel, "status": "PUBLISHED", "id": str(message_id),
                   "remote_id": str(message_id), "upload": {"mode": "bot-api", "bytes": size}}
        if username and message_id:
            return {**receipt, "url": f"https://t.me/{username}/{message_id}"}
        # A private channel has no public address for its posts.
        return {**receipt, "url": "", "permalink_available": False}
