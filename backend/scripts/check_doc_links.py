"""Check repository Markdown file links without importing the application.

Run from any directory: python backend/scripts/check_doc_links.py
Only local file targets are checked; URL availability and heading fragments are
not checked. Project-owned hidden directories are included.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

EXCLUDED = {'.git', 'node_modules', '.venv', 'venv', '__pycache__',
            '.pytest_cache', '.mypy_cache', '.ruff_cache', 'dist', 'build', 'vendor'}


def documents(root: Path):
    for base, directories, names in os.walk(root):
        directories[:] = sorted(d for d in directories if d not in EXCLUDED)
        for name in sorted(names):
            path = Path(base) / name
            if path.suffix.lower() in {'.md', '.mdx'}:
                yield path


def links(text: str):
    text = re.sub(r'(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$', '', text)
    text = re.sub(r'`[^`\n]+`', '', text)
    inline = re.findall(r'''!?\[[^\]]*\]\(\s*<?([^\s)>]+)>?(?:\s+["'][^\n]*?["'])?\s*\)''', text)
    references = re.findall(r'(?m)^\s*\[[^\]]+\]:\s*<?([^\s>]+)>?', text)
    return inline + references


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    root = parser.parse_args().root.resolve()
    checked = missing = 0
    for path in documents(root):
        checked += 1
        for target in links(path.read_text(encoding='utf-8')):
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            if not (path.parent / unquote(parsed.path)).exists():
                print(f'{path.relative_to(root)}: missing {target}')
                missing += 1
    print(f'{checked} Markdown documents; {missing} missing local file links')
    return 1 if missing else 0


if __name__ == '__main__':
    raise SystemExit(main())
