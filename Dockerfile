# Образ для NAS, VPS и локального запуска без Python на хосте.
# Сборка: docker build -t tg-obs .
# С RAG (torch, ~1 ГБ): docker build --build-arg INSTALL_RAG=1 -t tg-obs .

# syntax=docker/dockerfile:1
FROM python:3.12-slim

# 0 — без RAG (лёгкий образ), 1 — добавить sentence-transformers и numpy.
ARG INSTALL_RAG=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Dubai

# tzdata нужен, чтобы даты в заметках совпадали с часовым поясом,
# а не с UTC контейнера.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости: слой кэшируется, пока requirements не менялись.
COPY requirements.txt requirements-rag.txt ./
RUN python -m pip install --upgrade pip \
    && pip install -r requirements.txt \
    && if [ "$INSTALL_RAG" = "1" ]; then pip install -r requirements-rag.txt; fi

COPY app ./app

# Непривилегированный пользователь: бот не должен работать от root.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/state /vault /backups \
    && chown -R bot:bot /app

USER bot

# Проверка живости: токен, хранилище, настройки. Интервал редкий, чтобы
# не дёргать Telegram API и диск каждую минуту.
HEALTHCHECK --interval=300s --timeout=60s --start-period=90s --retries=3 \
    CMD ["python", "-m", "app", "--health"]

CMD ["python", "-m", "app"]
