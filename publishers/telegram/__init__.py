"""Telegram channel publisher: uploads the approved video through the Bot API.

var.0.0.4.86 (#486): the channel is published by its **own** bot
(``TELEGRAM_PUBLISHER_BOT_TOKEN`` / ``telegram_publisher_bot_token``), never by the
bot that takes user requests (``telegram_bot_token``). The target is the Telegram
channel of the job's brand.
"""

from __future__ import annotations

import os

from publishers.base import Publisher, check_response

# Bot API limit for files uploaded through the public api.telegram.org server.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class TelegramPublisher(Publisher):
    channel = "telegram"
    credential_env = "TELEGRAM_PUBLISHER_BOT_TOKEN"

    def _readiness_token(self):
        from publishers.oauth import _get_secret
        return _get_secret(self.credential_env)

    def _probe_request(self, token: str):
        from adapters.telegram import api_root
        return (f"{api_root()}/bot{token}/getMe", {})

    def channel_readiness(self, chat_id: str) -> dict:
        """The bot is valid and may post to this channel. Nothing is sent."""
        return self._post_right(self.readiness(), chat_id)

    def _post_right(self, state: dict, chat_id: str) -> dict:
        """Given a bot readiness ``state``, check the right to post in ``chat_id``."""
        from adapters.telegram import api_root
        from publishers.base import publisher_mock_enabled
        from publishers.oauth import _get_secret

        if not state["ready"] or publisher_mock_enabled():
            return state
        refused = lambda reason: {**state, "ready": False, "reason": reason}  # noqa: E731
        if not chat_id:
            return refused("Додайте Telegram-канал бренду (Бренди → Канали)")
        token = _get_secret(self.credential_env)
        try:
            response = self.transport.get(f"{api_root()}/bot{token}/getChatMember",
                                          params={"chat_id": chat_id, "user_id": token.split(":")[0]}, timeout=15)
        except Exception:  # noqa: BLE001 - a transport error can quote the URL with the token
            return refused("Платформа недоступна: доступ до каналу не перевірено")
        if response.status_code >= 400:
            return refused("Бот не має доступу до каналу або канал не знайдено")
        try:
            member = response.json().get("result")
        except (ValueError, AttributeError):
            member = None
        # Only a proven right counts: the channel owner, or an administrator whose
        # answer states can_post_messages=true. A member, a restricted bot, an
        # administrator without that right or an unreadable answer is not ready.
        if not isinstance(member, dict) or not (
                member.get("status") == "creator"
                or (member.get("status") == "administrator" and member.get("can_post_messages") is True)):
            return refused("Бот не має права публікувати в каналі")
        return state

    def ready(self, metadata: dict | None = None) -> bool:
        """Credentials alone are not a publication target (var.0.0.4.86).

        With a task's metadata the target is its brand channel; without it (the
        CORE overview) at least one brand must have an enabled Telegram channel.
        """
        if not super().ready(metadata):
            return False
        from publishers.base import publisher_mock_enabled
        if publisher_mock_enabled():
            return True
        metadata = metadata or {}
        if metadata.get("telegram_chat_id") or metadata.get("target"):
            return True
        try:
            from core.publishing_targets import all_targets
            return bool(all_targets("telegram"))
        except Exception:  # noqa: BLE001 - outside CORE there is no brand catalogue
            return False

    def targets_readiness(self, chat_ids: list[str]) -> dict:
        """Ready only when the publisher bot may post in every enabled brand channel."""
        state = self.readiness()
        if not state["ready"]:
            return state
        if not chat_ids:
            return {**state, "ready": False, "reason": "Додайте Telegram-канал бренду (Бренди → Канали)"}
        refused = []
        for chat_id in chat_ids:
            checked = self._post_right(state, chat_id)
            if not checked["ready"]:
                refused.append(f"{chat_id}: {checked['reason']}")
        if refused:
            return {**state, "ready": False, "reason": "; ".join(refused)}
        return state

    def publish(self, video_path: str, metadata: dict) -> dict:
        from publishers.base import publisher_mock_enabled
        if not (metadata.get("telegram_chat_id") or metadata.get("target")) and not publisher_mock_enabled():
            # Owner decision: no installation-wide channel and no migration; a brand
            # without a Telegram channel is simply not configured for Telegram.
            return {"channel": self.channel, "status": "NOT_CONFIGURED",
                    "error": "Telegram-канал бренду не налаштовано"}
        return super().publish(video_path, metadata)

    def _publish_live(self, video_path: str, metadata: dict) -> dict:
        from adapters.telegram import api_root
        from publishers.oauth import _get_secret

        chat_id = metadata.get("telegram_chat_id") or metadata.get("target")
        if not chat_id:
            raise ValueError("Telegram-канал бренду не налаштовано")
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
