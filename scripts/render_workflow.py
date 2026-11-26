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

title_font = ImageFont.truetype(font_path, 28)
label_font = ImageFont.truetype(font_path, 21)
small_font = ImageFont.truetype(font_path, 15)
steps = [
    ("观测资源", "查看节点、设备和资产"),
    ("制定方案", "选择目标，保存草稿"),
    ("执行部署", "审阅后启动，记录任务"),
    ("诊断修复", "按需分析并冻结新候选"),
    ("独立验收", "核对服务并生成报告"),
]
frames = []
for active in range(len(steps)):
    for pulse in (0, 1, 2):
        canvas = Image.new("RGB", (1200, 350), "#0c1424")
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((1, 1, 1198, 348), 24, outline="#253650", width=2)
        draw.text((40, 30), "一次推理服务交付", font=title_font, fill="#f3f7ff")
        draw.text((40, 74), "PILOT  /  INFERENCE WORKFLOW", font=small_font, fill="#8fa7c5")
        for index, (label, detail) in enumerate(steps):
            x = 40 + index * 227
            selected = index == active
            fill = "#182f49" if selected else "#101f32"
            border = ("#62e6d1" if pulse != 1 else "#b0fff0") if selected else "#2b425c"
            draw.rounded_rectangle((x, 124, x + 210, 260), 16, fill=fill, outline=border, width=2)
            draw.text((x + 18, 132), f"0{index + 1}", font=small_font, fill="#69d6c9" if selected else "#6c87a4")
            draw.text((x + 18, 159), label, font=label_font, fill="#f1f7ff")
            draw.text((x + 18, 202), detail, font=small_font, fill="#a3b7cf")
            if index < len(steps) - 1:
                draw.line((x + 213, 192, x + 223, 192), fill="#617d98", width=2)
                draw.polygon([(x + 223, 192), (x + 218, 188), (x + 218, 196)], fill="#617d98")
        draw.text((40, 290), "方案保存后由用户审阅并启动  ·  修复仅在需要时进行", font=small_font, fill="#9db3ca")
        frames.append(canvas)
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
palette = frames[0].quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
frames[0].save(OUTPUT, save_all=True, append_images=frames[1:], duration=350, loop=0, optimize=True)
print(f"Wrote {OUTPUT}")
