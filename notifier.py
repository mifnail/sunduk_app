import logging
import os
from typing import Any, Callable, Protocol, runtime_checkable

import requests

log = logging.getLogger("notifier")


@runtime_checkable
class Notifier(Protocol):
    name: str

    def send(self, recipient_id: str, message: str) -> bool: ...


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


class MaxNotifier:
    """Уведомления через мессенджер MAX.

    Использует официальный API MAX: POST /messages с заголовком
    Authorization: <token>. Токен присваивается при создании бота.
    """

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


def build_notifiers() -> dict[str, Notifier]:
    return {
        "telegram": TelegramNotifier(),
        "vk": VKNotifier(),
        "max": MaxNotifier(),
    }


def send_notifications(recipient_channels: dict[str, str],
                       message: str,
                       notifiers: dict[str, Notifier] | None = None,
                       on_error: Callable[[str, str, Exception], None] | None = None) -> dict[str, bool]:
    """Отправляет сообщение во все доступные каналы.

    recipient_channels: {канал: идентификатор получателя}
    notifiers: {канал: экземпляр Notifier}
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