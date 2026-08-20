#!/usr/bin/env bash
# Установка приложения на Orange Pi (ARM64) с автозапуском через systemd.
# Запуск: sudo bash deploy/install.sh
set -euo pipefail

APP_DIR="/opt/orders_app"
VENV_DIR="$APP_DIR/venv"
SERVICE_USER="pi"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== 1. Установка системных пакетов ==="
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip sqlite3

echo "=== 2. Создание каталога приложения ==="
sudo mkdir -p "$APP_DIR"

echo "=== 3. Копирование файлов проекта ==="
sudo cp -r "$SCRIPT_DIR/." "$APP_DIR/"
sudo chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

echo "=== 4. Создание виртуального окружения ==="
sudo python3 -m venv "$VENV_DIR"

echo "=== 5. Установка зависимостей ==="
sudo "$VENV_DIR/bin/pip" install --upgrade pip
sudo "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "=== 6. Копирование systemd-юнитов ==="
sudo cp "$APP_DIR/deploy/orders.service" /etc/systemd/system/
sudo cp "$APP_DIR/deploy/orders-max-bot.service" /etc/systemd/system/

echo "=== 7. Включение автозапуска ==="
sudo systemctl daemon-reload
sudo systemctl enable orders
sudo systemctl enable orders-max-bot
sudo systemctl start orders
sudo systemctl start orders-max-bot

echo ""
echo "=== Готово ==="
echo "Проверка: systemctl status orders orders-max-bot"
echo "Веб-интерфейс: http://<IP>:5000"
echo "Не забудьте заполнить $APP_DIR/.env ключами (см. .env.example)."