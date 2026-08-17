#!/usr/bin/env bash
# Установка приложения на Orange Pi (Debian/ARM64).
set -euo pipefail

APP_DIR=/opt/orders_app
SERVICE=orders

echo "=== 1. Системные пакеты ==="
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip

echo "=== 2. Копирование приложения ==="
sudo mkdir -p "$APP_DIR"
# Скопируйте файлы проекта в APP_DIR (например через git clone или rsync):
#   sudo rsync -a ./ "$APP_DIR/"
# Предполагаем, что файлы уже на месте.

echo "=== 3. Виртуальное окружение ==="
sudo python3 -m venv "$APP_DIR/venv"
sudo "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "=== 4. Файл настроек ==="
if [ ! -f "$APP_DIR/.env" ]; then
  sudo cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "Заполните $APP_DIR/.env своими токенами (TG, VK, MAX, SECRET_KEY)."
fi

echo "=== 5. Права ==="
sudo chown -R pi:pi "$APP_DIR"

echo "=== 6. systemd-сервис ==="
sudo cp "$APP_DIR/deploy/orders.service" /etc/systemd/system/orders.service
sudo systemctl daemon-reload
sudo systemctl enable orders
sudo systemctl restart orders

echo "=== 7. Проверка ==="
sleep 2
sudo systemctl status orders --no-pager
echo "Сервис запущен. Откройте http://<IP-Orange-Pi>:5000"