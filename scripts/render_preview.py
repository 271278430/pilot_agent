"""Combine four captured Pilot screenshots into the README preview animation.

Capture the home, node, plan and asset views in an isolated demo first, then
pass their image paths with the corresponding options. Requires Pillow and a
font with Chinese glyphs; PILOT_VISUAL_FONT can override the font selection.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("home", "nodes", "plan", "assets"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    fonts = [
        os.environ.get("PILOT_VISUAL_FONT", ""),
        r"C:\Windows\Fonts\msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    ]
    font_path = next((name for name in fonts if name and Path(name).is_file()), None)
    if not font_path:
        raise SystemExit("Set PILOT_VISUAL_FONT to a font with Chinese glyphs")
    title_font = ImageFont.truetype(font_path, 19)
    small_font = ImageFont.truetype(font_path, 14)
    views = [
        ("工作台首页", args.home),
        ("节点与权限", args.nodes),
        ("填写任务方案", args.plan),
        ("模型与镜像", args.assets),
    ]
    frames = []
    for index, (label, path) in enumerate(views):
        screen = Image.open(path).convert("RGB")
        screen.thumbnail((1148, 646), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (1200, 760), "#0e192c")
        draw = ImageDraw.Draw(canvas)
        draw.text((26, 19), "PILOT  /  界面预览", font=title_font, fill="#edf7ff")
        draw.text((1174, 24), "演示数据", font=small_font, fill="#9dc9c2", anchor="ra")
        canvas.paste(screen, ((1200 - screen.width) // 2, 63))
        draw.text((26, 730), f"{index + 1:02d}  /  {label}", font=small_font, fill="#d5e7ed")
        for dot in range(len(views)):
            x = 1120 + dot * 16
            draw.ellipse((x, 735, x + 7, 742), fill="#77d9c7" if dot == index else "#4a6376")
        frames.append(canvas.resize((1000, 633), Image.Resampling.LANCZOS))
    output = ROOT / "docs/assets/preview.webp"
    output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(output, format="WEBP", save_all=True, append_images=frames[1:], duration=2800, loop=0, quality=75, method=6)
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
