import os
import httpx
import time

class TelegramAdapter:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
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

    def answer_callback(self, callback_query_id: str, text: str) -> dict:
        if not self.configured():
            return {"status": "STUB"}
        response = httpx.post(f"{self.base_url}/answerCallbackQuery",
                              json={"callback_query_id": callback_query_id, "text": text}, timeout=20)
        response.raise_for_status()
        return response.json()
