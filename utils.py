"""Pure helper functions: formatting, file-name rules, caption rendering."""
import html
import os
import re
from html.parser import HTMLParser
from urllib.parse import unquote


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
    prefix = (settings.get("prefix") or "").replace(r"\s", " ")
    suffix = (settings.get("suffix") or "").replace(r"\s", " ")
    new = f"{prefix}{new}{suffix}"
    return safe_filename(new + ext)


def tidy_name(name):
    """Underscores -> spaces (and %20 style escapes decoded) so names look like the original title."""
    stem, ext = os.path.splitext(name)
    stem = unquote(stem).replace("_", " ")
    stem = re.sub(r"\s{2,}", " ", stem).strip()
    return safe_filename((stem or name) + ext) if stem else name


def parse_size(text):
    m = re.fullmatch(r"\s*([\d.]+)\s*(b|kb|mb|gb)?\s*", (text or "").lower())
    if not m:
        return None
    mult = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3, None: 1024**2}[m.group(2)]
    try:
        return int(float(m.group(1)) * mult)
    except ValueError:
        return None


# ----------------------------------------------------------------- media type
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".3gp", ".mpg", ".mpeg",
             ".mts", ".m2ts", ".ogv"}
AUDIO_EXT = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".oga", ".opus", ".aac", ".wma", ".mka", ".ac3"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def media_kind(path):
    """video | audio | image | other. Extension based: Python's mimetypes does not know .mkv."""
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    return "other"


# ----------------------------------------------------------------- caption
CAPTION_VARS = ("filename", "size", "duration", "resolution", "ext")
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote",
                "tg-spoiler"}
_TAG = re.compile(r"<(/?)([a-zA-Z][\w-]*)([^<>]*)>")


def _clean_html(text):
    """Keep Telegram-supported tags, escape everything else."""
    out, pos = [], 0
    for m in _TAG.finditer(text):
        out.append(html.escape(text[pos : m.start()]))
        closing, name, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if name in ALLOWED_TAGS:
            if name == "a" and not closing:
                href = re.search(r"href\s*=\s*[\"']([^\"']+)[\"']", attrs)
                if href and href.group(1).startswith(("http://", "https://", "tg://")):
                    out.append(f'<a href="{html.escape(href.group(1), quote=True)}">')
                else:
                    out.append(html.escape(m.group(0)))
            else:
                out.append(f"</{name}>" if closing else f"<{name}>")
        else:
            out.append(html.escape(m.group(0)))
        pos = m.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)


class _Balance(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack, self.ok = [], True

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.ok = False
        else:
            self.stack.pop()


def _balanced(text):
    p = _Balance()
    try:
        p.feed(text)
        p.close()
    except Exception:  # noqa: BLE001
        return False
    return p.ok and not p.stack


def _fit(escaped_builder, limit=1000):
    """Build escaped text; shorten the raw text until it fits Telegram's caption limit."""
    raw = escaped_builder
    escaped = html.escape(raw)
    while len(escaped) > limit and raw:
        raw = raw[:-20]
        escaped = html.escape(raw)
    return escaped


def render_caption(settings, filename, size, duration=0, width=0, height=0):
    """Build the HTML caption: template -> (safe HTML tags kept) -> chosen font."""
    template = settings.get("caption") or "{filename}"
    values = {
        "filename": filename,
        "size": hsize(size),
        "duration": htime(duration) if duration else "N/A",
        "resolution": f"{height}p" if height else "N/A",
        "ext": os.path.splitext(filename)[1].lstrip(".").upper() or "N/A",
    }
    inner, plain = [], []
    for part in re.split(r"(\{\w+\})", template):
        key = part[1:-1] if part.startswith("{") and part.endswith("}") else None
        if key in values:
            inner.append(html.escape(str(values[key])))
            plain.append(str(values[key]))
        else:
            inner.append(_clean_html(part))
            plain.append(part)
    rich = "".join(inner)
    has_tags = bool(re.search(r"<[a-z/]", rich))
    if not _balanced(rich) or len(rich) > 1000:
        rich = _fit("".join(plain))  # broken / too long tags -> show as plain text
        has_tags = False
    font = settings.get("font", "default")
    if has_tags and font in ("mono", "pre"):  # Telegram cannot nest tags inside code / pre
        font = "default"
    return font_wrap(rich, font)
