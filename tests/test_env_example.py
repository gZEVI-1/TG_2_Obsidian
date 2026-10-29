"""Шаблон .env.example должен описывать все настройки из config.py."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_CALL_RE = re.compile(
    r'_env_(?:str|bool|int|float|folder|note_path|user_ids)\(\s*"([A-Z0-9_]+)"'
)
EXAMPLE_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]{2,})=", re.MULTILINE)


class EnvExampleTests(unittest.TestCase):
    def test_every_setting_is_documented(self) -> None:
        source = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        names = set(ENV_CALL_RE.findall(source))
        missing = sorted(name for name in names if name not in example)
        self.assertEqual(missing, [])

    def test_no_unknown_names_in_example(self) -> None:
        source = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
        known = set(ENV_CALL_RE.findall(source))
        # Docker-переменные читает docker-compose.yml, а не бот.
        known |= {
            "VAULT_PATH",
            "BACKUP_PATH",
            "STATE_PATH",
            "PUID",
            "PGID",
            "INSTALL_RAG",
            "TZ",
        }
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        listed = set(EXAMPLE_RE.findall(example))
        unknown = sorted(
            name
            for name in listed
            if name not in known
            and not re.fullmatch(r"LLM_API_\d+_(NAME|BASE_URL|KEY|API_KEY)", name)
        )
        self.assertEqual(unknown, [])


if __name__ == "__main__":
    unittest.main()
