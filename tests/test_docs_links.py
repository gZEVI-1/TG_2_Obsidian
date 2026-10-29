"""Проверка ссылок и якорей в README и документации (без сети)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
PUNCT_RE = re.compile(r"[^\w\s\u0400-\u04ff-]")


def slug(text: str) -> str:
    text = text.strip().lower()
    text = PUNCT_RE.sub("", text)
    return re.sub(r"\s+", "-", text)


def anchors_of(path: Path) -> set[str]:
    source = path.read_text(encoding="utf-8")
    return {slug(match.group(2)) for match in HEADING_RE.finditer(source)}


class DocsLinksTests(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self) -> None:
        files = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
        anchors: dict[Path, set[str]] = {}
        problems: list[str] = []
        for path in files:
            source = path.read_text(encoding="utf-8")
            for match in LINK_RE.finditer(source):
                target = match.group(1).strip()
                if not target or target.startswith(
                    ("http://", "https://", "mailto:")
                ):
                    continue
                file_part, _, anchor = target.partition("#")
                resolved = (path.parent / file_part).resolve() if file_part else path
                if not resolved.exists():
                    problems.append(f"{path.name}: нет файла {target}")
                    continue
                if anchor and resolved.suffix == ".md":
                    if resolved not in anchors:
                        anchors[resolved] = anchors_of(resolved)
                    if anchor not in anchors[resolved]:
                        problems.append(
                            f"{path.name}: нет якоря #{anchor} в {resolved.name}"
                        )
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
