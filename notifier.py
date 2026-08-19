import logging
import os
from typing import Any, Callable, Protocol, runtime_checkable

import requests

log = logging.getLogger("notifier")


@runtime_checkable
class Notifier(Protocol):
    name: str

    def send(self, recipient_id: str, message: str) -> bool: ...
    def send_photo(self, recipient_id: str, photo_data: bytes, caption: str = "",
                   mime_type: str = "image/jpeg") -> bool: ...


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str = "", base_url: str = "https://api.telegram.org"):
        self.token = token or os.environ.get("TG_TOKEN", "")
        self.base_url = base_url
        self.proxy = os.environ.get("TG_PROXY", "")

    def _session(self) -> requests.Session:
        s = requests.Session()
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
        return s

    def send(self, recipient_id: str, message: str) -> bool:
        if not self.token or not recipient_id:
            return False
        url = f"{self.base_url}/bot{self.token}/sendMessage"
        session = self._session()
        resp = session.post(url, json={
            "chat_id": recipient_id,
            "text": message,
            "disable_web_page_preview": True,
        }, timeout=10)
        resp.raise_for_status()
        return resp.json().get("ok", False)

    def send_photo(self, recipient_id: str, photo_data: bytes, caption: str = "",
                   mime_type: str = "image/jpeg") -> bool:
        if not self.token or not recipient_id:
            return False
        url = f"{self.base_url}/bot{self.token}/sendPhoto"
        session = self._session()
        files = {"photo": ("photo.jpg", photo_data, mime_type)}
        data = {"chat_id": recipient_id}
        if caption:
            data["caption"] = caption
        resp = session.post(url, data=data, files=files, timeout=20)
        resp.raise_for_status()
        return resp.json().get("ok", False)


class VKNotifier:
    name = "vk"

    def __init__(self, token: str = "", group_id: str = "",
                 api_url: str = "https://api.vk.com/method"):
        self.token = token or os.environ.get("VK_TOKEN", "")
        self.group_id = group_id or os.environ.get("VK_GROUP_ID", "")
        self.api_url = api_url
        self.version = os.environ.get("VK_API_VERSION", "5.199")

    def send(self, recipient_id: str, message: str) -> bool:
        if not self.token or not recipient_id:
            return False
        url = f"{self.api_url}/messages.send"
        resp = requests.post(url, data={
            "access_token": self.token,
            "user_id": recipient_id,
            "random_id": abs(hash((recipient_id, message))) % (2 ** 31),
            "message": message,
            "v": self.version,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            log.warning("VK error: %s", data["error"])
            return False
        return True

    def send_photo(self, recipient_id: str, photo_data: bytes, caption: str = "",
                   mime_type: str = "image/jpeg") -> bool:
        """VK: загрузка фото на сервер -> сохранение -> отправка."""
        if not self.token or not recipient_id:
            return False
        try:
            # 1. Получаем сервер для загрузки
            upload_url_resp = requests.post(
                f"{self.api_url}/photos.getMessagesUploadServer",
                data={"access_token": self.token, "v": self.version},
                timeout=10
            )
            upload_url_resp.raise_for_status()
            upload_data = upload_url_resp.json()
            if "error" in upload_data:
                log.warning("VK upload server error: %s", upload_data["error"])
                return False
            upload_url = upload_data["response"]["upload_url"]

            # 2. Загружаем фото
            files = {"photo": ("photo.jpg", photo_data, mime_type)}
            upload_resp = requests.post(upload_url, files=files, timeout=20)
            upload_resp.raise_for_status()
            save_data = upload_resp.json()
            if "error" in save_data:
                log.warning("VK photo save error: %s", save_data["error"])
                return False

            # 3. Сохраняем фото
            save_resp = requests.post(
                f"{self.api_url}/photos.saveMessagesPhoto",
                data={
                    "access_token": self.token,
                    "photo": save_data["photo"],
                    "server": save_data["server"],
                    "hash": save_data["hash"],
                    "v": self.version,
                },
                timeout=10
            )
            save_resp.raise_for_status()
            photo_info = save_resp.json()
            if "error" in photo_info:
                log.warning("VK save photo error: %s", photo_info["error"])
                return False

            photo = photo_info["response"][0]
            attachment = f"photo{photo['owner_id']}_{photo['id']}"

            # 4. Отправляем сообщение с вложением
            message_text = caption or "Фото заказа"
            send_resp = requests.post(
                f"{self.api_url}/messages.send",
                data={
                    "access_token": self.token,
                    "user_id": recipient_id,
                    "random_id": abs(hash((recipient_id, message_text))) % (2 ** 31),
                    "message": message_text,
                    "attachment": attachment,
                    "v": self.version,
                },
                timeout=10
            )
            send_resp.raise_for_status()
            result = send_resp.json()
            if "error" in result:
                log.warning("VK send error: %s", result["error"])
                return False
            return True
        except Exception as exc:
            log.warning("VK send_photo error: %s", exc)
            return False


class MaxNotifier:
    """Уведомления через мессенджер MAX."""

    name = "max"

    def __init__(self, token: str = "", base_url: str = "https://platform-api2.max.ru"):
        self.token = token or os.environ.get("MAX_TOKEN", "")
        self.base_url = base_url

    def send(self, recipient_id: str, message: str) -> bool:
        if not self.token or not recipient_id:
            log.info("MAX: не настроен, пропускаем")
            return False
        resp = requests.post(
            f"{self.base_url}/messages",
            params={"user_id": recipient_id},
            headers={"Authorization": self.token},
            json={"text": message},
            timeout=10,
        )
        resp.raise_for_status()
        return True

    def send_photo(self, recipient_id: str, photo_data: bytes, caption: str = "",
                   mime_type: str = "image/jpeg") -> bool:
        if not self.token or not recipient_id:
            log.info("MAX: не настроен, пропускаем")
            return False
        try:
            # 1. Загружаем файл
            files = {"file": ("photo.jpg", photo_data, mime_type)}
            resp = requests.post(
                f"{self.base_url}/files",
                headers={"Authorization": self.token},
                files=files,
                timeout=20,
            )
            resp.raise_for_status()
            file_data = resp.json()
            file_id = file_data.get("file_id") or file_data.get("id")
            if not file_id:
                log.warning("MAX: не получен file_id: %s", file_data)
                return False

            # 2. Отправляем сообщение с фото
            body = {
                "text": caption or "Фото заказа",
                "attachments": [{"type": "photo", "payload": {"file_id": file_id}}]
            }
            resp = requests.post(
                f"{self.base_url}/messages",
                params={"user_id": recipient_id},
                headers={"Authorization": self.token},
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            log.warning("MAX send_photo error: %s", exc)
            return False


def build_notifiers() -> dict[str, Notifier]:
    return {
        "telegram": TelegramNotifier(),
        "vk": VKNotifier(),
        "max": MaxNotifier(),
    }


def send_notifications(recipient_channels: dict[str, str],
                       message: str,
                       notifiers: dict[str, Notifier] | None = None,
                       photo_data: bytes | None = None,
                       photo_caption: str = "",
                       photo_mime: str = "image/jpeg",
                       on_error: Callable[[str, str, Exception], None] | None = None) -> dict[str, bool]:
    """Отправляет сообщение (и опционально фото) во все доступные каналы.

    recipient_channels: {канал: идентификатор получателя}
    notifiers: {канал: экземпляр Notifier}
    photo_data: байты фото (если есть)
    photo_caption: подпись к фото
    Возвращает {канал: успех}. Ошибка одного канала не влияет на остальные.
    """
    notifiers = notifiers or build_notifiers()
    results: dict[str, bool] = {}
    for channel, recipient_id in recipient_channels.items():
        if not recipient_id:
            continue
        notifier = notifiers.get(channel)
        if notifier is None:
            results[channel] = False
            continue
        try:
            if photo_data and hasattr(notifier, 'send_photo'):
                results[channel] = bool(notifier.send_photo(recipient_id, photo_data, photo_caption, photo_mime))
            else:
                results[channel] = bool(notifier.send(recipient_id, message))
        except Exception as exc:  # noqa: BLE001 - изоляция каналов
            log.warning("Канал %s не сработал: %s", channel, exc)
            results[channel] = False
            if on_error:
                on_error(channel, message, exc)
    return results


def order_status_message(order: Any, status_name: str) -> str:
    return (
        f"Ваш заказ #{order['id']} ({order['service_name']}): "
        f"статус изменился на «{status_name}».\n"
        f"Описание: {order['description'] or '—'}"
    )