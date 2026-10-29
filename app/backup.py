"""Резервная копия: хранилище и журнал аудита в zip, с ротацией копий."""

from __future__ import annotations

import logging
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from app.config import Settings

log = logging.getLogger(__name__)

ARCHIVE_PREFIX = "tg-obs-backup-"
MANIFEST_NAME = "backup-info.txt"
# Настройки Obsidian (в них ключ плагина) и история git в копию не попадают.
SKIP_DIRS = frozenset({".obsidian", ".git"})


def create_backup(settings: Settings, *, now: datetime | None = None) -> Path:
    """Создаёт zip с хранилищем и журналом, старые копии сверх BACKUP_KEEP удаляет.

    Секреты и производные файлы в копию не попадают: `.env`, `.obsidian`
    (в настройках плагина лежит ключ Local REST API), индекс RAG и логи.
    Журнал аудита копируется отдельно от vault, потому что лежит в `state/`.
    """
    target = settings.backup_path
    if target is None:
        raise ValueError("BACKUP_DIR не задан: укажите папку для копий в .env")
    vault = _vault_path(settings)
    if vault is None and not settings.audit_root.is_dir():
        raise ValueError("Нечего копировать: хранилище не задано, журнала аудита нет")
    if vault is not None and _is_inside(target, vault):
        raise ValueError(
            "BACKUP_DIR не должен быть внутри хранилища: копия попадёт в следующую копию"
        )

    moment = now or datetime.now()
    target.mkdir(parents=True, exist_ok=True)
    archive = _archive_path(target, moment)

    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        if vault is not None:
            _add_folder(bundle, vault, "vault")
        if settings.audit_root.is_dir():
            _add_folder(bundle, settings.audit_root, "state/audit")
        bundle.writestr(MANIFEST_NAME, _manifest(settings, moment))

    log.info(
        "Резервная копия создана: %s (%.1f МБ)",
        archive,
        archive.stat().st_size / 1_048_576,
    )
    _prune(target, settings.backup_keep)
    return archive


def last_backup(settings: Settings) -> datetime | None:
    """Время самой свежей копии; None — копий ещё нет."""
    target = settings.backup_path
    if target is None or not target.is_dir():
        return None
    times = [
        path.stat().st_mtime
        for path in target.glob(f"{ARCHIVE_PREFIX}*.zip")
    ]
    return datetime.fromtimestamp(max(times)) if times else None


def backup_due(settings: Settings, *, now: datetime | None = None) -> bool:
    """Пора ли делать копию: по интервалу BACKUP_EVERY_HOURS."""
    if settings.backup_path is None or settings.backup_every_hours <= 0:
        return False
    last = last_backup(settings)
    if last is None:
        return True
    moment = now or datetime.now()
    return moment - last >= timedelta(hours=settings.backup_every_hours)


def backup_if_due(settings: Settings, *, now: datetime | None = None) -> Path | None:
    """Делает копию, только если она просрочена; иначе ничего не трогает."""
    if not backup_due(settings, now=now):
        return None
    return create_backup(settings, now=now)


def _vault_path(settings: Settings) -> Path | None:
    if not settings.obsidian_vault_path:
        return None
    vault = Path(settings.obsidian_vault_path).expanduser()
    if not vault.is_dir():
        raise ValueError(f"Хранилище не найдено: {vault}")
    return vault.resolve()


def _is_inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def _archive_path(target: Path, moment: datetime) -> Path:
    """Имя с временем; суффикс нужен, если две копии попали в одну секунду."""
    archive = target / f"{ARCHIVE_PREFIX}{moment:%Y%m%d-%H%M%S}.zip"
    counter = 2
    while archive.exists():
        archive = target / f"{ARCHIVE_PREFIX}{moment:%Y%m%d-%H%M%S}-{counter}.zip"
        counter += 1
    return archive


def _add_folder(bundle: zipfile.ZipFile, folder: Path, prefix: str) -> None:
    """Кладёт файлы папки в архив; служебные папки и ссылки пропускаются."""
    for item in sorted(folder.rglob("*")):
        if item.is_dir() or item.is_symlink():
            continue
        relative = item.relative_to(folder)
        if SKIP_DIRS.intersection(relative.parts[:-1]):
            continue
        bundle.write(item, f"{prefix}/{relative.as_posix()}")


def _manifest(settings: Settings, moment: datetime) -> str:
    return (
        "TG-OBS backup\n"
        f"создано: {moment:%Y-%m-%d %H:%M:%S}\n"
        f"vault: {settings.obsidian_vault_path or '<не задан>'}\n"
        "состав: vault/ (заметки, без .obsidian и .git), state/audit/ (журнал)\n"
        "не входят: .env, state/rag, state/logs, настройки Obsidian\n"
    )


def _prune(target: Path, keep: int) -> None:
    """Оставляет `keep` самых свежих копий, остальные удаляет."""
    archives = sorted(
        target.glob(f"{ARCHIVE_PREFIX}*.zip"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    for old in archives[keep:]:
        try:
            old.unlink()
            log.info("Удалена старая копия: %s", old.name)
        except OSError as error:
            log.warning("Не удалось удалить старую копию %s: %s", old.name, error)
