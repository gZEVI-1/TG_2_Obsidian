# Этап 15: CI и Docker для NAS/VPS (и опциональный этап 12)

## Этап

Этап 15 — инфраструктура развёртывания: CI на GitHub Actions и Docker-образ
с `docker compose`, чтобы бота можно было поднять на NAS или VPS и чтобы
человек, скачавший проект с GitHub, не ставил Python вручную. Здесь же —
опциональный этап 12: systemd-юнит, HTTPS для самоподписанного сертификата и
пошаговый перенос на NAS.

## Цель

Сделать так, чтобы:

- каждая отправка в GitHub автоматически проверялась тестами и сборкой образа;
- `git clone` + `.env` + `docker compose up -d` давали работающего бота;
- после перезагрузки NAS/VPS бот поднимался сам (`restart: unless-stopped`);
- путь к хранилищу, копиям и состоянию на хосте задавался в одном `.env`;
- образ не тащил секреты и не работал от root.

## Что сделано

- `.github/workflows/tests.yml`:
  - job `tests` — матрица Python 3.11/3.12/3.13, `pip install -r
    requirements.txt`, `python -m unittest discover -s tests -v`;
  - job `docker` — проверка `docker compose config` (на копии
    `.env.example`) и сборка образа через BuildKit с кэшем GitHub Actions;
  - секреты не нужны: тесты не ходят в сеть и подменяют `.env`.
- `Dockerfile`:
  - база `python:3.12-slim`, `tzdata`, `TZ` (по умолчанию `Asia/Dubai`) —
    иначе даты в заметках были бы по UTC;
  - зависимости ставятся отдельным слоем, кэшируются;
  - `ARG INSTALL_RAG=0` — при `1` добавляются `sentence-transformers` и
    `numpy` (образ тяжелее примерно на гигабайт);
  - непривилегированный пользователь `bot` (uid 1000), `HEALTHCHECK` через
    `python -m app --health`;
  - `.dockerignore` исключает `.env`, `.venv`, `state`, `tests`, `docs`,
    `templates`.
- `docker-compose.yml`:
  - один сервис `bot`, `restart: unless-stopped`, `init: true`;
  - `env_file: .env` плюс переопределения `OBSIDIAN_VAULT_PATH=/vault`,
    `BACKUP_DIR=/backups` — на смонтированные каталоги;
  - тома: `VAULT_PATH`, `BACKUP_PATH`, `STATE_PATH` (по умолчанию `./vault`,
    `./backups`, `./state`), `user: PUID:PGID`, ограничение логов Docker.
- `.env.example` — разделы выстроены в порядке рабочего `.env` (хранилище,
  Telegram, LLM, копии), дальше — необязательные блоки; в конце добавлен
  раздел Docker (`VAULT_PATH`, `PUID`, `INSTALL_RAG`, `TZ` и т. д.).
- `README.md` — бейджи Docker и CI, раздел «Запуск в Docker», обновлённое
  дерево проекта.
- **Этап 12 (опционально):** `OBSIDIAN_VERIFY_SSL` — поддержка HTTPS
  Local REST API с самоподписанным сертификатом (по умолчанию проверка
  включена; `false` — только для своего сертификата); `deploy/tg-obs.service`
  — юнит systemd для NAS/VPS без Docker; `restart: unless-stopped` в compose
  уже даёт автозапуск после перезагрузки.

## Внешние действия

1. Включить CI: `git push` в `main` — workflow запустится сам. Ветку
   `main` в настройках репозитория GitHub Actions по умолчанию использует
   как основную; дополнительных секретов не требуется.
2. Проверить Docker-запуск у себя (Docker Desktop должен быть запущен):

   ```powershell
   cd E:\FOLDER\TG-OBS\TG_2_Obsidian
   docker compose up -d --build
   docker compose logs -f bot
   ```

   Перед этим в `.env` стоит задать `OBSIDIAN_MODE=vault` и пути
   `VAULT_PATH`/`BACKUP_PATH` в прямых слэшах.
3. На NAS/VPS: скопировать репозиторий, создать `.env`, выполнить
   `docker compose up -d --build`. Для Synology Container Manager нужен
   проект с тем же `docker-compose.yml`.

## Принятые решения

- **Docker, а не инструкция «поставьте Python».** Для NAS и VPS это
  единственный переносимый способ: один образ, версии зависимостей
  фиксируются слоем, обновление — `git pull && docker compose up -d --build`.
- **`vault`-режим как основной в контейнере.** Local REST API на хосте
  слушает `127.0.0.1`; из контейнера это его собственный loopback. Пока
  плагин не слушает сеть, надёжнее смонтировать папку хранилища. Если нужно
  именно REST — в `.env` задаётся `OBSIDIAN_BASE_URL=http://host.docker.internal:27123`
  (заготовка есть в compose) и плагин настраивается на прослушивание сети.
- **Отдельные `VAULT_PATH`/`BACKUP_PATH`, а не `OBSIDIAN_VAULT_PATH`.** Пути
  в `.env` для Windows (`E:\...`) не годятся как источник для монтирования,
  а переменные compose-хоста удобнее держать отдельно и в прямых слэшах.
- **Не root.** Бот пишет в смонтированные каталоги; `PUID/PGID` подгоняются
  под владельца папки на NAS, иначе будут права `root:root` и отказ записи.
- **`TZ` в образе.** Даты в заметках берутся из локального времени; без
  `tzdata` контейнер писал бы UTC и даты съезжали на 4 часа.
- **CI без RAG.** `torch` в CI — это гигабайты и минуты; RAG-тесты помечены
  `skipUnless(available())` и пропускаются без `numpy`, а полный набор
  проверяется локально.
- Отклонено: публикация образа в GHCR (пока не нужно — собирается на месте),
  отдельный `docker-compose.prod.yml` (один файл проще), запуск от root.

## Как проверить

```powershell
cd E:\FOLDER\TG-OBS\TG_2_Obsidian

# 1. Локальные тесты (то же, что в CI)
.\.venv\Scripts\python.exe -m unittest discover -s tests    # 246 тестов, OK

# 2. Конфигурация compose без вывода секретов
docker compose config --quiet                               # код 0, пусто

# 3. Сборка и запуск
docker build -t tg-obs:ci .
docker run --rm tg-obs:ci python -m app --check-config      # config OK
docker run --rm tg-obs:ci id -u                             # 1000
docker images tg-obs:ci                                     # ~241 МБ
```

Проверено 30.10.2026: образ собран (241 МБ), `--check-config` внутри
контейнера отвечает `config OK`, процесс работает от uid 1000,
`docker compose config --quiet` проходит. Запуск `docker compose up` в
рамках проверки не выполнялся: он поднял бы второго бота с вашим токеном.

После `git push` в GitHub: вкладка **Actions** → workflow `tests`, три
зелёные сборки по Python 3.11/3.12/3.13 и job `docker build`.

## Перенос на NAS (этап 12, опционально)

NAS пока нет, поэтому этап 12 сделан как опциональная инфраструктура: всё
готово, включается по мере переезда. Три способа запуска — выбирайте один.

### 1. Docker Compose (рекомендуется)

```bash
git clone https://github.com/gZEVI-1/TG_2_Obsidian.git /volume1/docker/tg-obs
cd /volume1/docker/tg-obs
cp .env.example .env
# в .env: токен, ключи, OBSIDIAN_MODE=vault,
# VAULT_PATH=/volume1/obsidian/MyVault, BACKUP_PATH=/volume1/docker/tg-obs/backups
docker compose up -d --build
```

`restart: unless-stopped` поднимает контейнер после перезагрузки NAS.
Обновление: `git pull && docker compose up -d --build`. RAG: соберите с
`INSTALL_RAG=1` в `.env` (образ станет тяжелее на ~1 ГБ).

### 2. systemd без Docker

```bash
sudo useradd -r -m -d /opt/tg-obs tgobs
sudo git clone https://github.com/gZEVI-1/TG_2_Obsidian.git /opt/tg-obs
cd /opt/tg-obs && sudo -u tgobs python3 -m venv .venv
sudo -u tgobs .venv/bin/pip install -r requirements.txt
sudo cp deploy/tg-obs.service /etc/systemd/system/
sudo nano /etc/systemd/system/tg-obs.service   # пути, user, ReadWritePaths
sudo systemctl daemon-reload && sudo systemctl enable --now tg-obs
journalctl -u tg-obs -f
```

`EnvironmentFile` читает `.env`; `ReadWritePaths` должен содержать `state/`,
хранилище и каталог копий — иначе `ProtectSystem=strict` не даст писать.

### 3. HTTPS и туннель для Local REST API

Если Obsidian остаётся на ПК, а бот на NAS, есть два варианта:

- **SSH-туннель** (ничего не настраивать в плагине):

  ```bash
  ssh -N -L 27124:127.0.0.1:27124 user@desktop
  # в .env бота: OBSIDIAN_BASE_URL=https://127.0.0.1:27124
  #               OBSIDIAN_VERIFY_SSL=false   (сертификат самоподписанный)
  ```

- **HTTPS-порт плагина** (27124): включите его в настройках Local REST API и
  укажите `OBSIDIAN_BASE_URL=https://<адрес>:27124`. Для самоподписанного
  сертификата нужен `OBSIDIAN_VERIFY_SSL=false`; с валидным сертификатом
  проверку оставьте включённой. Порт в интернет не открывайте — только LAN
  или туннель.

После переезда проверьте: `--health` (хранилище, доступ, копии), одну тестовую
заметку, копию `--backup` в сетевую папку и перезагрузку NAS.

## Риски и откат

- **Права на тома.** Если `PUID/PGID` не совпадает с владельцем папки
  хранилища, бот получит отказ записи; лечится `ls -ln` на NAS и правкой
  `PUID/PGID`.
- **Пустой `VAULT_PATH`.** Compose создаст пустую папку `./vault`, и бот
  будет работать с пустым хранилищем — проверяйте `docker compose config`
  и `--health` после первого запуска.
- **REST из контейнера.** По умолчанию недоступен: `127.0.0.1` внутри
  контейнера — это он сам. Нужен `host.docker.internal` и плагин,
  слушающий сеть (или `network_mode: host` на Linux).
- **Обновление образа не перезаписывает `.env` и тома.** Данные в
  `VAULT_PATH`/`STATE_PATH`/`BACKUP_PATH` живут на хосте.
- **Два экземпляра бота.** Нельзя одновременно держать запущенным бота на
  хосте и в контейнере с одним токеном: Telegram отдаст `409 Conflict`.
- **`OBSIDIAN_VERIFY_SSL=false`** отключает проверку сертификата: это только
  для своего самоподписанного сертификата в локальной сети. Если адрес станет
  публичным, проверку нужно вернуть.
- **systemd и `ReadWritePaths`.** Если путь из списка не существует,
  `ProtectSystem=strict` не даст юниту стартовать; список правьте под свои
  каталоги.
- **Откат.** Остановить и удалить контейнер (`docker compose down`), при
  необходимости удалить образ (`docker image rm tg-obs:local`); CI-файл
  удаляется вместе с `.github/workflows/tests.yml`. Код бота не менялся.

## Следующий шаг

Обязательных работ не осталось: этапы 0–15 закрыты, этап 12 доступен как
опциональная инфраструктура. По желанию: оповещения о сбоях в Telegram,
метрики, публикация образа в GHCR и автообновление на NAS.
