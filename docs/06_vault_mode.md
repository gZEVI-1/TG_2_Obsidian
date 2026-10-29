# Работа без запущенного Obsidian: файловый режим

## Зачем

Local REST API — плагин внутри Obsidian, поэтому в режиме `rest` бот работает,
только пока Obsidian открыт. Если приложение закрыто (или его нет на машине,
например на NAS), бот получал ошибку соединения и не мог ни найти, ни записать
заметку.

## Как устроено

Добавлен второй клиент хранилища — `VaultClient` в `app/obsidian.py`. Он читает
и пишет `.md`-файлы напрямую в папке vault, без HTTP и без плагина.

- Режим выбирается переменной `OBSIDIAN_MODE`:
  - `auto` (по умолчанию) — сначала REST API, при его недоступности файлы,
    если задан `OBSIDIAN_VAULT_PATH` (подробно — [12_auto_mode.md](12_auto_mode.md));
  - `rest` — `ObsidianClient` (Local REST API), Obsidian должен быть запущен;
  - `vault` — `VaultClient`, Obsidian может быть закрыт, `OBSIDIAN_API_KEY`
    не нужен; путь в `OBSIDIAN_VAULT_PATH` обязателен.
- Явный `OBSIDIAN_MODE` позволяет переключаться, не комментируя путь:
  `OBSIDIAN_MODE=rest` при заполненном `OBSIDIAN_VAULT_PATH` работает через API.
- Оба клиента реализуют общий интерфейс `ObsidianBackend` (`health`,
  `search`, `list_notes`, `folder_exists`, `note_exists`, `read_note`,
  `write_note`). Поиск по именам `search_notes` тоже общий, поэтому поведение
  поиска и подтверждений в боте не меняется.
- Полнотекстовый поиск в файловом режиме — чтение заметок и поиск по словам:
  запрос очищается от кавычек, пунктуации и служебных слов, совпадение фразы
  целиком весит больше, чем совпадение слов в другом порядке. Индекс Obsidian
  не используется, отдельная БД не нужна.
- Служебные папки (`.obsidian`, `.trash`, `.git`, `.stfolder`, `.sync`)
  пропускаются; символьные ссылки не обходятся.
- Глубина обхода и число заметок ограничены так же, как в REST-режиме
  (3 уровня, 500 файлов).
- Все файловые операции выполняются в отдельном потоке (`asyncio.to_thread`),
  чтобы не блокировать обработку сообщений Telegram.
- Путь каждой заметки проверяется: выйти за пределы папки vault нельзя.
- Запись идёт через временный файл и `os.replace`, чтобы при сбое не оставить
  половину заметки. Защита от перезаписи и dry-run работают как раньше.
- Нечитаемые и слишком большие файлы (> 2 МБ) при поиске пропускаются.

## Настройка

В `.env`:

```dotenv
# rest — только Local REST API, vault — только файлы, auto — REST с резервом
OBSIDIAN_MODE=vault
OBSIDIAN_VAULT_PATH=E:\FOLDER\TG-OBS\TG-OBS-vault-test
# или для рабочего хранилища:
# OBSIDIAN_VAULT_PATH=C:\Users\user\Documents\MyVault
```

Переключение на API — поменять одну строку и перезапустить бота:

```dotenv
OBSIDIAN_MODE=rest
```

`OBSIDIAN_BASE_URL` и `OBSIDIAN_API_KEY` можно оставить — в режиме `vault`
они не используются. При `OBSIDIAN_MODE=rest` путь из `OBSIDIAN_VAULT_PATH`
игнорируется (в лог пишется предупреждение). В `auto` нужны оба значения:
ключ включает REST, путь — резерв.

## Проверка

```powershell
cd E:\FOLDER\TG-OBS\TG_2_Obsidian
$env:OBSIDIAN_MODE='vault'
$env:OBSIDIAN_VAULT_PATH='E:\FOLDER\TG-OBS\TG-OBS-vault-test'
.\.venv\Scripts\python.exe -m app --check-config      # obsidian_mode = vault
.\.venv\Scripts\python.exe -m app --check-obsidian    # хранилище доступно, тестовая запись
```

Obsidian при этом можно закрыть. Ожидаемые строки:

```
app:   obsidian_mode = vault (файлы напрямую), OBSIDIAN_MODE=vault
app.obsidian: Obsidian: режим vault, прямой доступ к файлам ...
app: Хранилище доступно напрямую: ... (writable=True)
```

Тесты: `python -m unittest discover -s tests`.

## Ограничения

- Нет поиска Obsidian по алиасам, тегам и ссылкам: ищутся имена файлов и текст.
- Удаление папок и заметок обратимое: объект переносится в `.trash`, а не
  стирается. Восстановление — вручную из файловой системы или Obsidian.
- Нет мгновенного обновления открытого Obsidian: файл меняется на диске,
  Obsidian подхватит изменение при переключении в окно (или сразу, если открыт).
- Плагины Obsidian (шаблоны, Dataview) при записи не выполняются — бот пишет
  обычный Markdown.
- Если vault синхронизируется (Syncthing, облако), возможны конфликтные копии
  при одновременной правке с двух устройств. Один пользователь — не проблема.

## Откат

Убрать `OBSIDIAN_VAULT_PATH` (и/или поставить `OBSIDIAN_MODE=rest`) в `.env`
и перезапустить бота — снова используется Local REST API. Код откатывается
возвратом `app/obsidian.py` к версии Этапа 3.
