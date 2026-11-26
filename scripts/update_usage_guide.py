"""Generate or check the current guide from the shared business catalogue."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pilot_infra.usage_guide import markdown  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "docs/CURRENT_GUIDE.md"
    expected = markdown()
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            raise SystemExit("Usage guide is stale. Run python scripts/update_usage_guide.py")
        print("Usage guide matches the shared catalogue")
    else:
        target.write_text(expected, encoding="utf-8")
        print("Updated docs/CURRENT_GUIDE.md")


if __name__ == "__main__":
    main()
