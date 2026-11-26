"""Check repository-local Markdown links and section anchors."""
from __future__ import annotations

import collections
import html
import os
import posixpath
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
EXTERNAL = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//)")


def headings(text: str) -> set[str]:
    anchors = set(re.findall(r"<a\s+(?:name|id)=[\"']([^\"']+)", text))
    counts: collections.Counter[str] = collections.Counter()
    for line in text.splitlines():
        match = HEADING.match(line)
        if not match:
            continue
        title = re.sub(r"<[^>]+>", "", match.group(1))
        title = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)
        title = html.unescape(title).lower()
        slug = "".join(c for c in title if c.isalnum() or c in " -_").replace(" ", "-")
        counts[slug] += 1
        anchors.add(slug if counts[slug] == 1 else f"{slug}-{counts[slug] - 1}")
    return anchors


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    )
    files = {os.fsdecode(x).replace("\\", "/") for x in result.stdout.split(b"\0") if x}
    files = {x for x in files if (ROOT / x).is_file()}
    directories = {posixpath.dirname(name) for name in files}
    markdown = sorted(name for name in files if name.lower().endswith(".md"))
    anchors: dict[str, set[str]] = {}
    failures = []
    checked = 0
    for name in markdown:
        text = (ROOT / name).read_text(encoding="utf-8")
        for match in LINK.finditer(text):
            target = match.group(1).strip().split(" ", 1)[0].strip("<>")
            if not target or EXTERNAL.match(target):
                continue
            path, _, fragment = target.partition("#")
            path = unquote(path)
            relative = name if not path else posixpath.normpath(posixpath.join(posixpath.dirname(name), path))
            checked += 1
            if relative not in files and relative not in directories:
                failures.append((name, target))
                continue
            if fragment and relative in files and relative.lower().endswith(".md"):
                if relative not in anchors:
                    anchors[relative] = headings((ROOT / relative).read_text(encoding="utf-8"))
                if unquote(fragment) not in anchors[relative]:
                    failures.append((name, target))
    for name, target in failures:
        print(f"{name}: missing link {target}")
    print(f"Documentation links: {checked} checked, {len(failures)} failures")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
