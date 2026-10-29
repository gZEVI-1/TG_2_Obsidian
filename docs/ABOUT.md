# About проекта: что писать на GitHub

Готовые тексты для поля **About** в правой колонке репозитория и для каталогов
вроде awesome-lists. Ничего секретного здесь нет — только описание проекта.

## Поле About → Description

Ограничение GitHub — 350 символов. Два варианта, выбирайте по языку аудитории.

**Русский (162 символа):**

```text
Telegram-бот с AI-агентом для Obsidian: создаёт и правит заметки в вашем стиле, ставит связи, ищет по смыслу. REST API или файлы, Docker, резервные копии.
```

**English (157 символов):**

```text
Telegram bot with an AI agent for Obsidian: writes and edits notes in your style, adds links, searches by meaning. REST API or plain files, Docker-ready.
```

## Поле About → Website

Отдельного сайта у проекта нет. Варианты:

- оставить пустым — GitHub сам покажет README;
- указать [INSTALL.md](INSTALL.md) как точку входа для новичков:
  `https://github.com/gZEVI-1/TG_2_Obsidian/blob/main/docs/INSTALL.md`.

## Topics (теги)

GitHub: **Settings → About → Topics**, до 20 тегов, строчными буквами через
дефис. Рекомендуемый набор:

```text
telegram-bot
obsidian
obsidian-vault
aiogram
ai-agent
llm
openai-compatible
rag
note-taking
personal-knowledge-management
markdown
python
docker
self-hosted
automation
```

## Социальный превью-кадр

GitHub показывает его при ссылке на репозиторий: **Settings → Social preview**,
картинка 1280×640. Что на ней стоит разместить:

- название `TG-OBS` крупно;
- одну строку «Telegram → AI-агент → Obsidian»;
- три тега: `aiogram`, `LLM`, `Obsidian`;
- тёмный фон, крупный шрифт — превью часто показывают маленьким.

## Короткое описание для каталогов

Один абзац, если нужен текст без Markdown:

> TG-OBS — личный Telegram-бот с AI-агентом для Obsidian. Он создаёт заметки в
> вашем стиле и по шаблонам, дописывает и правит разделы, расставляет `[[связи]]`,
> ищет по смыслу и работает как через Local REST API, так и напрямую с
> `.md`-файлами. Настраивается через `.env` и заметки в хранилище, разворачивается
> на ПК, NAS или VPS в Docker.

## Чего в About не должно быть

- токенов, ключей, id пользователей и путей вида `E:\FOLDER\...`;
- обещаний, которых нет в коде (например, «работает с Evernote»);
- эмодзи в Description — GitHub показывает их непредсказуемо в разных темах.

## Связанные документы

- [README.md](../README.md) — возможности и быстрый старт.
- [STAGES.md](STAGES.md) — история этапов и статус.
- [16_ci_docker.md](16_ci_docker.md) — Docker, NAS и публикация образа.
- [LICENSE](../LICENSE) — MIT.
