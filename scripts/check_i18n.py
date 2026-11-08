#!/usr/bin/env python3
"""Localization contract checker for CC Desktop Switch.

Usage:
    python scripts/check_i18n.py
    python scripts/check_i18n.py --verbose

The script enforces three rules:

1. ``frontend/js/i18n.js`` and ``backend/i18n.py`` expose exactly the same key
   set for English (``en``) and Simplified Chinese (``zh``).
2. Every translation key referenced from the UI (``t("...")``,
   ``formatI18n("...")``, ``data-i18n*`` attributes) exists in both languages.
3. Application source files contain no CJK characters outside the two
   dictionary files. Localized documents (``README.zh-CN.md``,
   ``README.ja.md`` and the historical ``docs/`` archive) are exempt.

Exit code is 0 when everything passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FRONTEND_DICT = ROOT / "frontend" / "js" / "i18n.js"
BACKEND_DICT = ROOT / "backend" / "i18n.py"

# Source trees and loose files that must stay free of untranslated CJK text.
CHECKED_PATHS = [
    ROOT / "backend",
    ROOT / "frontend",
    ROOT / "macos",
    ROOT / "scripts",
    ROOT / "tests",
    ROOT / "main.py",
    ROOT / "build.spec",
    ROOT / "build.bat",
    ROOT / "start.bat",
    ROOT / "installer.nsi",
    ROOT / "README.md",
]

# Dictionary files are allowed to hold CJK because that is where the
# translations live. Localized documentation is a separate deliverable.
CJK_EXEMPT_FILES = {
    FRONTEND_DICT,
    BACKEND_DICT,
    ROOT / "README.zh-CN.md",
    ROOT / "README.ja.md",
}

EXEMPT_DIRS = {
    ROOT / "docs",
    ROOT / "frontend" / "vendor",
    ROOT / "frontend" / "assets",
    ROOT / ".git",
    ROOT / "backend" / "__pycache__",
    ROOT / "tests" / "__pycache__",
}

SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".zip",
    ".exe",
    ".dmg",
    ".pkg",
    ".pdf",
    ".pyc",
    ".pyo",
}

CJK_PATTERN = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")

KEY_LINE = re.compile(r'^ {6}"([A-Za-z0-9_.\-]+)"\s*:\s*"', re.MULTILINE)
FRONTEND_KEY_USE = re.compile(r'(?:^|[^A-Za-z0-9_.$])t\(\s*"([A-Za-z0-9_.\-]+)"')
FRONTEND_FORMAT_USE = re.compile(r'formatI18n\(\s*"([A-Za-z0-9_.\-]+)"')
HTML_ATTR_USE = re.compile(r'data-i18n(?:-placeholder|-title|-aria-label)?="([A-Za-z0-9_.\-]+)"')


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def parse_frontend_dictionaries() -> dict[str, set[str]]:
    """Split i18n.js into its ``en`` and ``zh`` dictionaries and collect keys."""
    source = read_text(FRONTEND_DICT)
    match = re.search(r"zh:\s*\{", source)
    if not match:
        raise SystemExit("check_i18n: cannot locate the zh dictionary in frontend/js/i18n.js")
    english_part = source[: match.start()]
    chinese_part = source[match.start():]
    return {
        "en": set(KEY_LINE.findall(english_part)),
        "zh": set(KEY_LINE.findall(chinese_part)),
    }


def parse_backend_dictionaries() -> dict[str, set[str]]:
    """Import backend.i18n and collect its key sets."""
    sys.path.insert(0, str(ROOT))
    try:
        from backend import i18n  # type: ignore
    finally:
        sys.path.pop(0)
    dictionaries = getattr(i18n, "TRANSLATIONS", {})
    return {lang: set(values.keys()) for lang, values in dictionaries.items()}


def collect_used_keys() -> dict[str, set[str]]:
    """Collect every translation key referenced by the UI."""
    used: dict[str, set[str]] = {}
    frontend_key_files = [
        ROOT / "frontend" / "js" / "app.js",
        ROOT / "frontend" / "js" / "api.js",
    ]
    for path in frontend_key_files:
        if not path.exists():
            continue
        text = read_text(path)
        keys = set(FRONTEND_KEY_USE.findall(text)) | set(FRONTEND_FORMAT_USE.findall(text))
        if keys:
            used[path.name] = keys
    html = ROOT / "frontend" / "index.html"
    if html.exists():
        used[html.name] = set(HTML_ATTR_USE.findall(read_text(html)))
    return used


def iter_checked_files() -> list[Path]:
    """Yield every source file that must stay free of CJK characters."""
    files: list[Path] = []
    for entry in CHECKED_PATHS:
        if not entry.exists():
            continue
        if entry.is_file():
            files.append(entry)
            continue
        for path in sorted(entry.rglob("*")):
            if not path.is_file():
                continue
            if any(exempt in path.parents for exempt in EXEMPT_DIRS):
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            if path in CJK_EXEMPT_FILES:
                continue
            files.append(path)
    return files


def find_cjk(path: Path) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    for number, line in enumerate(read_text(path).splitlines(), start=1):
        if CJK_PATTERN.search(line):
            findings.append((number, line.strip()))
    return findings


def check_key_parity(dictionaries: dict[str, set[str]], label: str, problems: list[str]) -> None:
    if "en" not in dictionaries or "zh" not in dictionaries:
        problems.append(f"{label}: missing an en or zh dictionary")
        return
    english = dictionaries["en"]
    chinese = dictionaries["zh"]
    only_en = sorted(english - chinese)
    only_zh = sorted(chinese - english)
    if only_en:
        problems.append(f"{label}: {len(only_en)} key(s) missing from zh: {only_en[:10]}")
    if only_zh:
        problems.append(f"{label}: {len(only_zh)} key(s) missing from en: {only_zh[:10]}")


def check_used_keys(
    dictionaries: dict[str, set[str]],
    used: dict[str, set[str]],
    problems: list[str],
) -> None:
    for filename, keys in sorted(used.items()):
        for lang in ("en", "zh"):
            missing = sorted(key for key in keys if key not in dictionaries[lang])
            if missing:
                problems.append(f"{filename}: {len(missing)} key(s) missing from {lang}: {missing[:10]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the CC Desktop Switch localization contract.")
    parser.add_argument("--verbose", action="store_true", help="print a summary of every check")
    args = parser.parse_args(argv)

    problems: list[str] = []

    frontend = parse_frontend_dictionaries()
    backend = parse_backend_dictionaries()
    check_key_parity(frontend, "frontend/js/i18n.js", problems)
    check_key_parity(backend, "backend/i18n.py", problems)
    check_used_keys(frontend, collect_used_keys(), problems)

    cjk_files = 0
    for path in iter_checked_files():
        findings = find_cjk(path)
        if not findings:
            continue
        cjk_files += 1
        relative = path.relative_to(ROOT)
        for number, line in findings[:5]:
            problems.append(f"{relative}:{number}: untranslated CJK text: {line[:120]}")

    if args.verbose:
        print(f"frontend keys: en={len(frontend['en'])} zh={len(frontend['zh'])}")
        print(f"backend keys:  en={len(backend.get('en', set()))} zh={len(backend.get('zh', set()))}")
        print(f"source files scanned: {len(iter_checked_files())}")
        print(f"files containing CJK: {cjk_files}")

    if problems:
        print("Localization check failed:\n")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("Localization check passed: dictionaries are in sync and no untranslated CJK text remains.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
