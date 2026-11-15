"""
Prompt niches: /ghibli, /cooking, etc. send a branded, watermarked prompt card.

- PROMPT_NICHES defines which slash commands exist (edit here; a new niche needs a
  redeploy so Discord registers the command). The prompt TEXT lives in the Neon
  database and is edited on the dashboard, no redeploy needed.
- render_card() draws the prompt onto an image with a tiled watermark that is hard
  to remove without destroying the picture, plus the brand name in the metadata.
"""
import io
import os
import textwrap

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin

# key -> (label, emoji, accent color).  key becomes the slash command name.
PROMPT_NICHES = {
    "ghibli":    ("Ghibli", "\U0001f33f", (76, 125, 92)),
    "ghibli2d":  ("Ghibli 2D", "\U0001f3a8", (90, 110, 160)),
    "cooking":   ("Cooking", "\U0001f373", (196, 120, 40)),
    "hometips":  ("Home Tips", "\U0001f3e0", (70, 130, 150)),
    "geography": ("Geography", "\U0001f30d", (60, 120, 90)),
    "aidrama":   ("AI Drama Tagalog", "\U0001f3ad", (150, 70, 90)),
    "stickman":  ("Stickman Long-Form", "\U0001f58a", (90, 90, 100)),
}

BRAND = os.getenv("PROMPT_BRAND", os.getenv("SITE_NAME", "Syndicate"))
OWNER = os.getenv("PROMPT_OWNER", "$SYNDICATE")
CREATOR = os.getenv("PROMPT_CREATOR", "david_5522")
WIDTH = 1200
MARGIN = 70


def _font(size: int, bold: bool = False):
    paths = [
        f"/usr/share/fonts/truetype/liberation/LiberationSans-{'Bold' if bold else 'Regular'}.ttf",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    lines = []
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        words, cur = para.split(), ""
        for w in words:
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=font) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
    return lines


def render_card(niche_key: str, prompt: str, index: int = 0, total: int = 0) -> bytes:
    label, emoji, accent = PROMPT_NICHES.get(niche_key, (niche_key.title(), "\u2728", (90, 90, 100)))
    bg, card, ink, muted = (15, 18, 24), (24, 28, 36), (236, 239, 245), (150, 158, 170)

    title_f, meta_f, body_f, foot_f = _font(46, True), _font(26), _font(34), _font(24)
    body_max = WIDTH - MARGIN * 2
    tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    body_lines = _wrap(tmp, prompt, body_f, body_max)
    line_h = body_f.getbbox("Ay")[3] + 14
    body_h = len(body_lines) * line_h
    height = max(630, MARGIN + 150 + body_h + 90)

    img = Image.new("RGB", (WIDTH, height), bg)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([MARGIN - 24, MARGIN - 24, WIDTH - MARGIN + 24, height - MARGIN + 24], radius=28, fill=card)
    d.rounded_rectangle([MARGIN - 24, MARGIN - 24, MARGIN - 14, height - MARGIN + 24], radius=6, fill=accent)

    d.text((MARGIN, MARGIN - 4), f"{label} prompt", font=title_f, fill=ink)
    sub = f"{BRAND}" + (f"   \u2022   #{index} of {total}" if total else "")
    d.text((MARGIN, MARGIN + 58), sub, font=meta_f, fill=muted)
    d.line([MARGIN, MARGIN + 104, WIDTH - MARGIN, MARGIN + 104], fill=(55, 62, 76), width=2)

    y = MARGIN + 140
    for ln in body_lines:
        d.text((MARGIN, y), ln, font=body_f, fill=ink)
        y += line_h

    d.text((MARGIN, height - MARGIN - 6), f"{BRAND} \u2022 Do not remove watermark", font=foot_f, fill=muted)

    _tile_watermark(img, BRAND)

    meta = PngImagePlugin.PngInfo()
    meta.add_text("Watermark", BRAND)
    meta.add_text("Niche", label)
    meta.add_text("Software", f"{BRAND} Prompt Bot")
    out = io.BytesIO()
    img.save(out, format="PNG", pnginfo=meta)
    return out.getvalue()


def _tile_watermark(img: Image.Image, text: str):
    """Diagonal repeating watermark across the whole image, so a crop can't remove it."""
    w, h = img.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    wf = _font(34, True)
    stamp = f"  {text} \u00b7 {text} \u00b7 {text}  "
    step_x, step_y = 460, 150
    for yy in range(-step_y, h + step_y, step_y):
        offset = (yy // step_y % 2) * (step_x // 2)
        for xx in range(-step_x + offset, w + step_x, step_x):
            ld.text((xx, yy), stamp, font=wf, fill=(255, 255, 255, 28))
    layer = layer.rotate(30, expand=False, resample=Image.BICUBIC)
    img.paste(Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB"), (0, 0))

    # one bolder centered mark
    big = _font(72, True)
    bl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bl)
    tw = bd.textlength(text.upper(), font=big)
    bd.text(((w - tw) / 2, h / 2 - 40), text.upper(), font=big, fill=(255, 255, 255, 22))
    bl = bl.rotate(30, expand=False, resample=Image.BICUBIC)
    img.paste(Image.alpha_composite(img.convert("RGBA"), bl).convert("RGB"), (0, 0))


def render_txt(niche_key: str, prompt: str, index: int = 0, total: int = 0, label: str = "") -> bytes:
    """Prompt as a .txt file: owner header, the prompt with a watermark tag on every line, creator footer."""
    label = label or PROMPT_NICHES.get(niche_key, (niche_key.title(),))[0]
    mark = f"[{BRAND}]"
    bar = "=" * 60
    lines = [bar, f"  COMMUNITY OWNER  {OWNER}", f"  {label} PROMPT" + (f"   (#{index} of {total})" if total else ""), bar, ""]
    for raw in prompt.split(chr(10)):
        wrapped = textwrap.wrap(raw, width=76) or [""]
        for seg in wrapped:
            lines.append(f"{seg}  {mark}")
    lines += ["", bar, f"  Creator by {CREATOR}", f"  (c) {OWNER} - do not remove credit", bar, ""]
    return (chr(10).join(lines)).encode("utf-8")
