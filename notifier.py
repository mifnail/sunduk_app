"""Уведомления клиентов: протокол Notifier + 3 реализации + отправка.

Каналы: Telegram, VK, MAX. Каждый нотификатор независим —
ошибка одного канала не влияет на остальные (см. send_notifications).

ВАЖНО: это уведомления КЛИЕНТАМ. Бот оператора в MAX — это max_bot.py
(класс MaxBot), его не следует путать с MaxNotifier.
"""

import os
import random
from typing import Protocol, runtime_checkable

import requests


@runtime_checkable
class Notifier(Protocol):
    """Протокол канала уведомлений."""

    name: str

    def send(self, recipient_id: str, message: str) -> bool: ...

    def send_photo(self, recipient_id: str, photo_data: bytes,
                   caption: str = "", mime_type: str = "image/jpeg") -> bool: ...


class TelegramNotifier:
    """Уведомления через Telegram Bot API (опционально через SOCKS5-прокси)."""

    name = "telegram"
    API_BASE = "https://api.telegram.org"

    def __init__(self, token: str | None = None, proxy: str | None = None) -> None:
        self.token = token or os.environ.get("TG_TOKEN")
        if not self.token:
            raise ValueError("TelegramNotifier: TG_TOKEN не задан")
        self.proxy = proxy or os.environ.get("TG_PROXY")
        self._proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None

    def _url(self, method: str) -> str:
        return f"{self.API_BASE}/bot{self.token}/{method}"

    def send(self, recipient_id: str, message: str) -> bool:
        resp = requests.post(
            self._url("sendMessage"),
            data={"chat_id": recipient_id, "text": message},
            proxies=self._proxies,
            timeout=15,
        )
        resp.raise_for_status()
        return bool(resp.json().get("ok", False))

    def send_photo(self, recipient_id: str, photo_data: bytes,
                   caption: str = "", mime_type: str = "image/jpeg") -> bool:
        resp = requests.post(
            self._url("sendPhoto"),
            data={"chat_id": recipient_id, "caption": caption},
            files={"photo": ("photo.jpg", photo_data, mime_type)},
            proxies=self._proxies,
            timeout=30,
        )
        resp.raise_for_status()
        return bool(resp.json().get("ok", False))


class VKNotifier:
    """Уведомления через VK Bot API (сообщения сообщества).

    Фото отправляется в 4 шага: получить URL загрузки →
    загрузить файл → сохранить фото → отправить сообщение с вложением.
    """

    name = "vk"
    API_URL = "https://api.vk.com/method"

    def __init__(self, token: str | None = None, group_id: str | None = None,
                 api_version: str = "5.199") -> None:
        self.token = token or os.environ.get("VK_TOKEN")
        if not self.token:
            raise ValueError("VKNotifier: VK_TOKEN не задан")
        self.group_id = group_id or os.environ.get("VK_GROUP_ID")
        self.api_version = api_version

    def _call(self, method: str, params: dict) -> dict:
        params = dict(params)
        params.update({"access_token": self.token, "v": self.api_version})
        resp = requests.post(f"{self.API_URL}/{method}", data=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise RuntimeError(
                f"VK API error {err.get('error_code')}: {err.get('error_msg')}"
            )
        return data.get("response")

    def _send_params(self, recipient_id: str) -> dict:
        params = {"user_id": recipient_id, "random_id": random.randint(1, 2**31)}
        if self.group_id:
            params["group_id"] = self.group_id
        return params

    def send(self, recipient_id: str, message: str) -> bool:
        params = self._send_params(recipient_id)
        params["message"] = message
        self._call("messages.send", params)
        return True

    def send_photo(self, recipient_id: str, photo_data: bytes,
                   caption: str = "", mime_type: str = "image/jpeg") -> bool:
        # 1. Получить URL для загрузки фото
        upload = self._call("photos.getMessagesUploadServer", {"peer_id": recipient_id})
        upload_url = upload["upload_url"]
        # 2. Загрузить фото
        resp = requests.post(
            upload_url,
            files={"photo": ("photo.jpg", photo_data, mime_type)},
            timeout=30,
        )
        resp.raise_for_status()
        uploaded = resp.json()
        # 3. Сохранить фото
        saved = self._call("photos.saveMessagesPhoto", {
            "photo": uploaded["photo"],
            "server": uploaded["server"],
            "hash": uploaded["hash"],
        })
        photo = saved[0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        # 4. Отправить сообщение с вложением
        params = self._send_params(recipient_id)
        params["attachment"] = attachment
        if caption:
            params["message"] = caption
        self._call("messages.send", params)
        return True


class MaxNotifier:
    """Уведомления через MAX Platform API (upload file → send message)."""

    name = "max"
    DEFAULT_ENDPOINT = "https://platform-api2.max.ru"

    def __init__(self, token: str | None = None, endpoint: str | None = None) -> None:
        self.token = token or os.environ.get("MAX_TOKEN")
        if not self.token:
            raise ValueError("MaxNotifier: MAX_TOKEN не задан")
        self.endpoint = (
            endpoint or os.environ.get("MAX_ENDPOINT") or self.DEFAULT_ENDPOINT
        ).rstrip("/")

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def send(self, recipient_id: str, message: str) -> bool:
        resp = requests.post(
            f"{self.endpoint}/messages",
            json={"recipient_id": recipient_id, "text": message},
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return True

    def send_photo(self, recipient_id: str, photo_data: bytes,
                   caption: str = "", mime_type: str = "image/jpeg") -> bool:
        # 1. Загрузить файл
        resp = requests.post(
            f"{self.endpoint}/files",
            files={"file": ("photo.jpg", photo_data, mime_type)},
            headers=self._headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        file_id = data.get("file_id") or data.get("id")
        if not file_id:
            raise RuntimeError("MAX API: не получен file_id")
        # 2. Отправить сообщение с файлом
        resp = requests.post(
            f"{self.endpoint}/messages",
            json={"recipient_id": recipient_id, "file_id": file_id, "caption": caption},
            headers=self._headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return True


def build_notifiers() -> dict[str, Notifier]:
    """Собирает нотификаторы из переменных окружения.

    Канал включается только при наличии токена:
    TG_TOKEN → telegram, VK_TOKEN → vk, MAX_TOKEN → max.
    """
    notifiers: dict[str, Notifier] = {}
    if os.environ.get("TG_TOKEN"):
        notifiers["telegram"] = TelegramNotifier()
    if os.environ.get("VK_TOKEN"):
        notifiers["vk"] = VKNotifier()
    if os.environ.get("MAX_TOKEN"):
        notifiers["max"] = MaxNotifier()
    return notifiers


def send_notifications(recipient_channels: dict[str, str], message: str,
                       notifiers: dict[str, Notifier],
                       photo_data: bytes | None = None,
                       photo_caption: str = "",
                       photo_mime: str = "image/jpeg",
                       on_error=None) -> dict[str, bool]:
    """Отправляет уведомление во все каналы клиента.

    Каждый канал обрабатывается независимо: ошибка одного не влияет
    на остальные. Каналы без ID получателя или без нотификатора
    пропускаются (результат False). Если передан photo_data —
    дополнительно отправляется фото.

    Возвращает {channel: успех}.
    """
    results: dict[str, bool] = {}
    for channel, recipient_id in recipient_channels.items():
        notifier = notifiers.get(channel)
        if notifier is None or not recipient_id:
            results[channel] = False
            continue
        try:
            ok = bool(notifier.send(recipient_id, message))
            if photo_data:
                ok = bool(notifier.send_photo(
                    recipient_id, photo_data, photo_caption, photo_mime
                )) and ok
            results[channel] = ok
        except Exception as exc:  # noqa: BLE001 — один канал не ломает другие
            results[channel] = False
            if on_error is not None:
                on_error(channel, exc)
    return results


def order_status_message(order, status_name: str) -> str:
    """Формирует текст уведомления о смене статуса заказа."""
    return (
        f"Ваш заказ #{order['id']} ({order['service_name']}): "
        f"статус изменился на «{status_name}».\n"
        f"Описание: {order['description'] or '—'}"
    )