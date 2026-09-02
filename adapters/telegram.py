import os
import json
import time
import threading
import logging
import pathlib
from typing import Callable

import httpx

from core.first_run import config_root

logger = logging.getLogger(__name__)


def _integration_secret(name: str) -> str | None:
    try:
        from core.first_run import ensure_secret_store
        return ensure_secret_store().get(name)
    except Exception:
        return None


class TelegramAdapter:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN") or _integration_secret("telegram_bot_token") or ""
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def configured(self) -> bool:
        return bool(self.token)

    def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        if not self.configured():
            return {"status": "STUB", "reason": "TELEGRAM_BOT_TOKEN is not configured"}
        last_error = None
        for attempt in range(int(os.getenv("TELEGRAM_RETRIES", "3"))):
            try:
                payload = {"chat_id": chat_id, "text": text}
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                response = httpx.post(f"{self.base_url}/sendMessage", json=payload, timeout=20)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as error:
                last_error = error
                time.sleep(2 ** attempt)
        raise last_error or RuntimeError("Telegram send failed")

    def download_file(self, file_id: str, max_bytes: int = 25 * 1024 * 1024) -> tuple[bytes, str]:
        if not self.configured():
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        metadata = httpx.get(f"{self.base_url}/getFile", params={"file_id": file_id}, timeout=20)
        metadata.raise_for_status()
        file_path = metadata.json().get("result", {}).get("file_path")
        if not file_path:
            raise RuntimeError("Telegram returned no file_path")
        response = httpx.get(f"https://api.telegram.org/file/bot{self.token}/{file_path}", timeout=60)
        response.raise_for_status()
        if len(response.content) > max_bytes:
            raise RuntimeError("Telegram attachment exceeds configured size limit")
        return response.content, os.path.basename(file_path)

    def set_webhook(self, public_url: str, secret_token: str = "") -> dict:
        if not self.configured():
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        payload = {"url": public_url.rstrip("/") + "/api/telegram/webhook"}
        if secret_token:
            payload["secret_token"] = secret_token
        response = httpx.post(f"{self.base_url}/setWebhook", json=payload, timeout=20)
        response.raise_for_status()
        return response.json()

    def delete_webhook(self) -> dict:
        if not self.configured():
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        response = httpx.post(f"{self.base_url}/deleteWebhook", timeout=20)
        response.raise_for_status()
        return response.json()

    def get_me(self) -> dict:
        if not self.configured():
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        response = httpx.get(f"{self.base_url}/getMe", timeout=20)
        response.raise_for_status()
        return response.json()

    def get_updates(self, offset: int, timeout: int = 30, allowed_updates: str | None = None) -> dict:
        if not self.configured():
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        params = {"timeout": timeout, "offset": offset}
        if allowed_updates:
            params["allowed_updates"] = allowed_updates
        response = httpx.get(f"{self.base_url}/getUpdates", params=params, timeout=timeout + 10)
        response.raise_for_status()
        return response.json()

    def answer_callback(self, callback_query_id: str, text: str) -> dict:
        if not self.configured():
            return {"status": "STUB"}
        response = httpx.post(f"{self.base_url}/answerCallbackQuery",
                              json={"callback_query_id": callback_query_id, "text": text}, timeout=20)
        response.raise_for_status()
        return response.json()

    def send_video(self, chat_id: str, video_path: str, caption: str = "") -> dict:
        if not self.configured():
            return {"status": "STUB", "reason": "TELEGRAM_BOT_TOKEN is not configured"}
        with open(video_path, "rb") as video_file:
            response = httpx.post(f"{self.base_url}/sendVideo",
                                  data={"chat_id": chat_id, "caption": caption[:1024]},
                                  files={"video": video_file}, timeout=120)
        response.raise_for_status()
        return response.json()

    def send_photo(self, chat_id: str, photo_path: str, caption: str = "") -> dict:
        if not self.configured():
            return {"status": "STUB", "reason": "TELEGRAM_BOT_TOKEN is not configured"}
        with open(photo_path, "rb") as photo_file:
            response = httpx.post(f"{self.base_url}/sendPhoto",
                                  data={"chat_id": chat_id, "caption": caption[:1024]},
                                  files={"photo": photo_file}, timeout=60)
        response.raise_for_status()
        return response.json()


class TelegramPollingService:
    def __init__(
        self,
        token: str,
        on_update: Callable[[dict], None],
        offset_file: pathlib.Path | None = None,
    ) -> None:
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.on_update = on_update
        self.offset_file = offset_file or (config_root() / "telegram_polling_state.json")
        self.last_error: str | None = None
        self.last_update_id: int | None = None
        self.last_message_at: str | None = None
        self.offset = self._load_offset()
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _load_offset(self) -> int:
        try:
            if self.offset_file.exists():
                data = json.loads(self.offset_file.read_text(encoding="utf-8"))
                self.last_update_id = data.get("last_update_id")
                self.last_message_at = data.get("last_message_at")
                return int(data.get("offset", 0))
        except (OSError, ValueError, TypeError):
            pass
        return 0

    def _save_offset(self) -> None:
        try:
            self.offset_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "offset": self.offset,
                "last_update_id": self.last_update_id,
                "last_message_at": self.last_message_at,
            }
            temporary = self.offset_file.parent / (self.offset_file.name + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.offset_file)
        except OSEError:
            pass

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self.last_error = None
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=15)
            self._thread = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _run(self) -> None:
        try:
            self._delete_webhook()
        except Exception as error:
            logger.warning("Telegram deleteWebhook failed: %s", error)

        while True:
            with self._lock:
                if not self._running:
                    break
            try:
                updates = self._get_updates()
                for update in updates:
                    with self._lock:
                        if not self._running:
                            break
                    try:
                        self.on_update(update)
                    except Exception as error:
                        logger.error("Telegram update processing failed: %s", error)
                    update_id = update.get("update_id")
                    if update_id is not None:
                        self.last_update_id = update_id
                        self.offset = update_id + 1
                        self._save_offset()
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 429:
                    retry_after = 5
                    try:
                        retry_after = int(error.response.json().get("parameters", {}).get("retry_after", 5))
                    except Exception:
                        pass
                    logger.warning("Telegram rate limited, sleeping %ds", retry_after)
                    time.sleep(retry_after)
                    continue
                self.last_error = str(error)
                logger.error("Telegram polling HTTP error: %s", error)
            except (httpx.HTTPError, OSError, RuntimeError) as error:
                self.last_error = str(error)
                logger.error("Telegram polling error: %s", error)
            except Exception as error:
                self.last_error = str(error)
                logger.error("Telegram polling unexpected error: %s", error)
            with self._lock:
                if not self._running:
                    break
            delay = int(os.getenv("TELEGRAM_POLLING_RETRY_DELAY", "5"))
            time.sleep(delay)

        logger.info("Telegram polling stopped")

    def _delete_webhook(self) -> None:
        response = httpx.post(f"{self.base_url}/deleteWebhook", timeout=20)
        response.raise_for_status()

    def _get_updates(self) -> list[dict]:
        timeout = int(os.getenv("TELEGRAM_POLLING_TIMEOUT", "30"))
        params: dict[str, object] = {"timeout": timeout, "offset": self.offset}
        allowed_updates = os.getenv("TELEGRAM_POLLING_ALLOWED_UPDATES", "")
        if allowed_updates:
            params["allowed_updates"] = [item.strip() for item in allowed_updates.split(",") if item.strip()]
        response = httpx.get(f"{self.base_url}/getUpdates", params=params, timeout=timeout + 10)
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Telegram API error: {result.get('description')}")
        updates = result.get("result", [])
        if updates:
            self.last_error = None
        return updates
