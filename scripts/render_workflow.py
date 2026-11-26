"""Render the animated workflow illustration used by the project README.

Requires Pillow and a font with Chinese glyphs. Set PILOT_VISUAL_FONT to
override the font used when regenerating the image.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs/assets/workflow.gif"
FONTS = [
    os.environ.get("PILOT_VISUAL_FONT", ""),
    r"C:\Windows\Fonts\msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
font_path = next((name for name in FONTS if name and Path(name).is_file()), None)
if not font_path:
    raise SystemExit("Set PILOT_VISUAL_FONT to a font with Chinese glyphs")

title_font = ImageFont.truetype(font_path, 27)
label_font = ImageFont.truetype(font_path, 21)
small_font = ImageFont.truetype(font_path, 16)
steps = [
    ("资源检查", "节点、资产和可用配额"),
    ("方案与授权", "冻结范围，由用户审阅"),
    ("受限执行", "按配方运行并记录状态"),
    ("独立验收", "核对身份、测试与证据"),
]
frames = []
for active in range(4):
    for pulse in (0, 1, 2):
        canvas = Image.new("RGB", (1200, 330), "#0c1424")
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((1, 1, 1198, 328), 24, outline="#253650", width=2)
        draw.text((48, 30), "从需求到交付", font=title_font, fill="#f3f7ff")
        draw.text((48, 70), "PILOT  /  INFERENCE WORKFLOW", font=small_font, fill="#8fa7c5")
        for index, (label, detail) in enumerate(steps):
            x = 48 + index * 283
            selected = index == active
            fill = "#182f49" if selected else "#101f32"
            border = ("#62e6d1" if pulse != 1 else "#b0fff0") if selected else "#2b425c"
            draw.rounded_rectangle((x, 118, x + 245, 254), 16, fill=fill, outline=border, width=2)
            draw.text((x + 18, 132), f"0{index + 1}", font=small_font, fill="#69d6c9" if selected else "#6c87a4")
            draw.text((x + 18, 159), label, font=label_font, fill="#f1f7ff")
            draw.text((x + 18, 202), detail, font=small_font, fill="#a3b7cf")
            if index < 3:
                draw.line((x + 252, 186, x + 273, 186), fill="#617d98", width=2)
                draw.polygon([(x + 273, 186), (x + 267, 182), (x + 267, 190)], fill="#617d98")
        draw.text((48, 282), "可审阅的方案  ·  有边界的执行  ·  可核对的结果", font=small_font, fill="#9db3ca")
        frames.append(canvas.quantize(colors=96))
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
frames[0].save(OUTPUT, save_all=True, append_images=frames[1:], duration=350, loop=0, optimize=True)
print(f"Wrote {OUTPUT}")
