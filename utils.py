"""Pure helper functions: formatting, file-name rules, caption rendering."""
import html
import os
import re


# ----------------------------------------------------------------- formatting
def hsize(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f}{unit}"
        n /= 1024
    return f"{n:.2f}PB"


def htime(s):
    s = int(max(float(s or 0), 0))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    if d:
        return f"{d}d{h}h{m}m"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{sec}s"
    return f"{sec}s"


def bar(pct, width=12):
    filled = int(min(max(pct, 0), 100) / 100 * width)
    return "⬢" * filled + "⬡" * (width - filled)


def shorten(text, limit=60):
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ----------------------------------------------------------------- caption fonts
# key -> (button label, html tag or None)
FONTS = {
    "default": ("Default", None),
    "bold": ("Bold", "b"),
    "italic": ("Italic", "i"),
    "underline": ("Underline", "u"),
    "strike": ("Strikethrough", "s"),
    "mono": ("Mono/Code", "code"),
    "spoiler": ("Spoiler", "spoiler"),
    "pre": ("Preformatted", "pre"),
}


def font_wrap(escaped_text, font):
    tag = FONTS.get(font, FONTS["default"])[1]
    return f"<{tag}>{escaped_text}</{tag}>" if tag else escaped_text


# ----------------------------------------------------------------- name rules
def parse_words(text):
    return [w.strip() for w in re.split(r"[,\n]", text or "") if w.strip()]


def parse_pairs(text):
    pairs = []
    for item in parse_words(text):
        if ":" in item:
            old, new = item.split(":", 1)
            if old.strip():
                pairs.append((old.strip(), new.strip()))
    return pairs


def safe_filename(name):
    name = re.sub(r"[\\/\x00]", "_", name or "").strip()
    stem, ext = os.path.splitext(name)
    while len((stem + ext).encode("utf-8")) > 240 and stem:
        stem = stem[:-1]
    return (stem + ext) or "file"


def apply_rules(filename, settings):
    """remove words -> replace pairs -> prefix / suffix. Extension is never touched."""
    stem, ext = os.path.splitext(filename)
    new = stem
    for word in parse_words(settings.get("remove", "")):
        new = re.sub(re.escape(word), "", new, flags=re.I)
    for old, rep in parse_pairs(settings.get("replace", "")):
        new = re.sub(re.escape(old), lambda _m, r=rep: r, new, flags=re.I)
    new = re.sub(r"\s{2,}", " ", new).strip()
    if not new:
        new = stem
    new = f"{settings.get('prefix', '')}{new}{settings.get('suffix', '')}"
    return safe_filename(new + ext)


def parse_size(text):
    m = re.fullmatch(r"\s*([\d.]+)\s*(b|kb|mb|gb)?\s*", (text or "").lower())
    if not m:
        return None
    mult = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, None: 1024**2}[m.group(2)]
    try:
        return int(float(m.group(1)) * mult)
    except ValueError:
        return None


# ----------------------------------------------------------------- caption
CAPTION_VARS = ("filename", "size", "duration", "resolution", "ext")


def render_caption(settings, filename, size, duration=0, width=0, height=0):
    """Build the HTML caption: template -> escape -> apply the user's chosen font."""
    template = settings.get("caption") or "{filename}"
    values = {
        "filename": filename,
        "size": hsize(size),
        "duration": htime(duration) if duration else "N/A",
        "resolution": f"{height}p" if height else "N/A",
        "ext": os.path.splitext(filename)[1].lstrip(".").upper() or "N/A",
    }
    raw_text = re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)
    raw_text = raw_text[:1000]
    escaped = html.escape(raw_text)
    while len(escaped) > 1000 and raw_text:  # escaping can grow the text
        raw_text = raw_text[:-20]
        escaped = html.escape(raw_text)
    return font_wrap(escaped, settings.get("font", "default"))
