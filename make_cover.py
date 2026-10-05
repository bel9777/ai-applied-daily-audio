"""Generate docs/cover.png (1400x1400) with Pillow. One-off; re-run to restyle."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

S = 1400
img = Image.new("RGB", (S, S), "#1e1b4b")
d = ImageDraw.Draw(img)
d.rectangle([0, 0, S, 520], fill="#22d3ee")
fonts = Path(r"C:\Windows\Fonts")
bold = str(fonts / "segoeuib.ttf")


def fit(text, start, max_w):
    size = start
    while size > 40:
        f = ImageFont.truetype(bold, size)
        if d.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return f


f1 = fit("AI APPLIED", 230, S - 200)
w = d.textlength("AI APPLIED", font=f1)
d.text(((S - w) / 2, 260), "AI APPLIED", font=f1, fill="#1e1b4b", anchor="lm")
f2 = fit("DAILY", 300, S - 400)
w = d.textlength("DAILY", font=f2)
d.text(((S - w) / 2, 900), "DAILY", font=f2, fill="#ffffff", anchor="lm")
img.save(Path(__file__).resolve().parent / "docs" / "cover.png")
