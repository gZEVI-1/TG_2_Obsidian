# Документация проекта TG-OBS

Telegram-бот с AI-агентом для работы с Obsidian через Obsidian Local REST API.
Создание заметок по шаблону или в моём стиле, небольшая генерация текста,
автоматическая простановка связей с существующими заметками.

## Этапы

| № | Этап | Документ | Статус |
|---|------|----------|--------|
| 0 | Уточнение требований и проверка окружения | [00_requirements.md](00_requirements.md) | готово |
| 1 | Каркас проекта и конфигурация | [01_env_setup.md](01_env_setup.md) | готово |
| 2 | Скелет Telegram-бота (`aiogram 3.x`) | [02_telegram_bot_skeleton.md](02_telegram_bot_skeleton.md) | готово |
| 3 | Клиент Obsidian Local REST API + dry-run | [03_obsidian_client.md](03_obsidian_client.md) | готово |
| 4 | LLM-клиент (OpenAI-совместимый API) | [04_llm_client.md](04_llm_client.md) | готово |
| 5 | Агент с инструментами | — | запланировано |
| 6 | Шаблоны заметок и «мой стиль» | — | запланировано |
| 7 | Автолинковка связей | — | запланировано |
| 8 | RAG / семантический поиск (опционально) | — | запланировано |
| 9 | Надёжность, подтверждения, лимиты | — | запланировано |
| 10 | Эксплуатация и перенос на NAS | — | запланировано |

## Архитектурные решения

Журнал решений в формате ADR-lite: [DECISIONS.md](DECISIONS.md)

## Текущее состояние

Готов Этап 4. Бот на `aiogram 3.x` запускается командой `python -m app`,
отвечает на `/start` и `/help`, любой текст отправляет модели и возвращает её
ответ. Бот помнит контекст диалога (последние 20 сообщений), команды `/new`
и `/start` начинают новый диалог. Модель — OpenRouter,
`nvidia/nemotron-3-ultra-550b-a55b:free` (`:free`-тариф, tool-calls проверены).
Клиент Obsidian умеет проверять соединение, читать и записывать заметки;
запись по умолчанию заблокирована dry-run.

Служебные команды:

```powershell
.\.venv\Scripts\python.exe -m app --check-config     # настройки, без сети
.\.venv\Scripts\python.exe -m app --check-obsidian   # Local REST API + тестовая запись
.\.venv\Scripts\python.exe -m app --check-llm        # модель + поддержка tool-calls
```

Агент с инструментами и создание заметок — Этап 5.

## Смежные папки

- `.env` — реальные секреты, в git не попадает.
- `E:\FOLDER\TG-OBS\TG-OBS-vault-test` — тестовое хранилище Obsidian
  с плагином Local REST API (HTTP, порт 27123). Рабочий vault не используется.

## Быстрый старт

Все команды — из корня репозитория `TG_2_Obsidian`:

```powershell
cd E:\FOLDER\TG-OBS\TG_2_Obsidian
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env    # если .env ещё нет; заполнить значения
.\.venv\Scripts\python.exe -m app
```
