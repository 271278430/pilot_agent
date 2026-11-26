"""Generate or check documentation from the current architecture catalogue."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pilot_infra.architecture import catalog, markdown  # noqa: E402
from pilot_infra.diagram import svg  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    target = ROOT / "docs/PILOT_ARCHITECTURE_MAP.md"
    expected = markdown()
    value = catalog()
    images = {ROOT / "docs/architecture" / (view["id"] + ".svg"): svg(view, value["components"]) for view in value["views"]}
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != expected:
            raise SystemExit("Architecture is stale. Run python scripts/update_architecture.py")
        if any(not path.exists() or path.read_text(encoding="utf-8") != image for path, image in images.items()):
            raise SystemExit("Architecture images are stale. Run python scripts/update_architecture.py")
        print("Architecture and SVG images match current source references and geometry")
    else:
        target.write_text(expected, encoding="utf-8")
        for path, image in images.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(image, encoding="utf-8")
        print(f"Updated architecture document and {len(images)} SVG diagrams")


if __name__ == "__main__":
    main()
