#!/bin/bash
set -e

echo "=== Развёртывание контент-платформы ==="

if [ ! -f .env ]; then
    echo "ОШИБКА: Файл .env не найден. Скопируйте .env.example в .env и заполните ключи."
    exit 1
fi

echo "1. Сборка Docker контейнера..."
docker compose build

echo "2. Запуск сервиса в фоне..."
docker compose up -d

echo "3. Статус контейнера:"
docker compose ps

echo "=== Готово! Логи: docker compose logs -f bot media-bot web ==="
