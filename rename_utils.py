"""Auto Rename: build a file name from a template.

Ported from the L4SAVE bot (same placeholders, same behaviour) with these differences:
  * {name} never contains site / group tags such as [Toonworld4all]
  * {resolution} is plain: 720p, 1080p ... (no bit depth)
  * {shortlang} is the language itself for one audio (Hindi), Dual for two, Multi for three or more
"""
import asyncio
import json
import os
import re

TEMPLATE_GUIDE_URL = "https://t.me/c/4291361651/11"

SITE_TAGS = (
    "HDHub4u|MoviesVerse|FilmyZilla|KatMovieHD|Worldfree4u|9xmovies|ExtraFlix|PrimeFix|mkvCinemas|"
    "Toonworld4all|ToonWorld4All|Toonworld|VegaMovies|MoviesMod|SkymoviesHD|Bolly4u|MoviesFlix|"
    "KMHD|1TamilMV|TamilBlasters|TamilRockers|HDMoviesHub|Filmywap|Jalshamoviez|DesiCinemas"
)
LANG_NAMES = {
    "hin": "Hindi", "hi": "Hindi", "eng": "English", "en": "English", "tam": "Tamil", "ta": "Tamil",
    "tel": "Telugu", "te": "Telugu", "mal": "Malayalam", "ml": "Malayalam", "kan": "Kannada",
    "kn": "Kannada", "mar": "Marathi", "mr": "Marathi", "ben": "Bengali", "bn": "Bengali",
    "guj": "Gujarati", "gu": "Gujarati", "pan": "Punjabi", "pa": "Punjabi", "urd": "Urdu", "ur": "Urdu",
    "ori": "Odia", "ory": "Odia", "or": "Odia", "bho": "Bhojpuri", "spa": "Spanish", "es": "Spanish",
    "fra": "French", "fre": "French", "fr": "French", "deu": "German", "ger": "German", "de": "German",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese", "jpn": "Japanese", "ja": "Japanese",
    "kor": "Korean", "ko": "Korean", "ita": "Italian", "it": "Italian", "por": "Portuguese",
    "pt": "Portuguese", "rus": "Russian", "ru": "Russian", "ara": "Arabic", "ar": "Arabic",
    "tur": "Turkish", "tr": "Turkish", "tha": "Thai", "th": "Thai", "ind": "Indonesian",
    "id": "Indonesian", "msa": "Malay", "may": "Malay", "ms": "Malay", "vie": "Vietnamese",
    "vi": "Vietnamese", "nep": "Nepali", "ne": "Nepali", "sin": "Sinhala", "si": "Sinhala",
    "fil": "Filipino", "pol": "Polish", "pl": "Polish", "nld": "Dutch", "dut": "Dutch", "nl": "Dutch",
    "swe": "Swedish", "sv": "Swedish", "heb": "Hebrew", "he": "Hebrew", "fas": "Persian",
    "per": "Persian", "fa": "Persian", "ukr": "Ukrainian", "uk": "Ukrainian", "ell": "Greek",
    "gre": "Greek", "el": "Greek", "ces": "Czech", "cze": "Czech", "hun": "Hungarian",
}
SKIP_LANGS = {"und", "mis", "zxx", "mul", ""}
FILENAME_LANGS = [
    "Hindi", "English", "Tamil", "Telugu", "Malayalam", "Kannada", "Marathi", "Bengali", "Gujarati",
    "Bhojpuri", "Odia", "Punjabi", "Spanish", "French", "German", "Chinese", "Japanese", "Korean",
    "Italian", "Portuguese", "Russian",
]
QUALITIES = [
    "SDTV-Rip", "HDTV-Rip", "SDTV", "HDTV", "SDTVRip", "HDTVRip", "DVDScr", "PRE-HD", "WEBDL",
    "WEB-DL", "WEB DL", "WEBRip", "BluRay", "HDRip", "PreDVD", "DVDRip", "YTRip", "HDTS", "HDTC",
    "CAMRip", "HQSprint", "HDCAM", "HDCAMRip", "TELESYNC", "TSQRip", "BRRip", "HQ SPrint",
]
OTT = [
    "NF", "AMZN", "HBO", "JioHotstar", "JioHS", "JHS", "DSNP", "HS", "CR", "JC", "Jio", "PF", "VOD",
    "BMS", "CRAV", "MX", "ATVP", "MAX", "PCOK", "SONYLiv", "ZEE5", "SS", "APLVTV", "HULU", "APL",
    "YT", "STZ", "STARZ", "PS", "AHA",
]


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


# ----------------------------------------------------------------- name parsing
def clean_name_for_parse(filename):
    """Turn a release-style file name into plain words (site tags and brackets removed)."""
    if not isinstance(filename, str) or not filename:
        return ""
    original = filename.strip()
    if "." in original:
        name_part, ext = original.rsplit(".", 1)
        ext = "." + ext
    else:
        name_part, ext = original, ""

    # leading [Site] / (Group) tags, also several in a row
    name_part = re.sub(r"^(?:[\[\(][^\]\)]{1,40}[\]\)][\s._-]*)+", "", name_part)
    # leading www.site.com
    name_part = re.sub(
        r"^(?:www\.)?[\w-]+\.(?:com|net|org|in|co|me|xyz|lol|top|cc|tv|club|site|cyou|pro|ink|vip|win)\b[\s._-]*",
        "", name_part, flags=re.I,
    )
    # known site / group tags anywhere
    name_part = re.sub(rf"\b(?:{SITE_TAGS})\b", "", name_part, flags=re.I)

    name_part = name_part.replace("_", " ").replace("+", " ")
    name_part = re.sub(r"\.+", " ", name_part)
    name_part = re.sub(r"[\[\]\(\)\{\}]", "", name_part)
    name_part = re.sub(r"\s+", " ", name_part).strip(" -")
    if not name_part:
        return original
    return f"{name_part}{ext}"


def extract_season_episode(file_name):
    season = start = end = ""
    m = re.search(r"\bS(\d{1,2})E(\d{1,3})(?:[-._]E?(\d{1,3}))?\b", file_name, flags=re.I)
    if m:
        season = f"S{int(m.group(1)):02d}"
        start = f"E{int(m.group(2)):02d}"
        if m.group(3):
            end = f"E{int(m.group(3)):02d}"
        return season, start, end
    m = re.search(r"\b(?:S|Season)\s*(\d{1,2})\b", file_name, flags=re.I)
    if m:
        season = f"S{int(m.group(1)):02d}"
    ep = re.compile(
        r"\b(?:E|EP|Episode)\s*(\d{1,3})(?:\s*(?:to|-|T|\.|_)\s*(?:E)?(\d{1,3}))?|E(\d{1,3})E(\d{1,3})\b",
        flags=re.I,
    )
    m = ep.search(file_name)
    if m:
        if m.group(1):
            start = f"E{int(m.group(1)):02d}"
            if m.group(2):
                end = f"E{int(m.group(2)):02d}"
        elif m.group(3):
            start = f"E{int(m.group(3)):02d}"
            end = f"E{int(m.group(4)):02d}"
    return season, start, end


def parse_name(original_name):
    """Read everything that can be read from the file name alone."""
    raw = re.sub(r"[\_\s+\(\)]", " ", original_name)
    cleaned = clean_name_for_parse(raw)
    stem = os.path.splitext(cleaned)[0] if "." in cleaned else cleaned

    audio = re.findall(
        r"\b(?:AAC|DD|DDP|Opus|AC3)\s?(?:\d{1,2}(?:\.\d+)?)?(?:\s?Atmos)?\b|\b(?:\d+CH)\b", raw, flags=re.I
    )
    m = re.search(r"\b(\d{3,4})p\b", stem, flags=re.I)
    if m:
        resolution = f"{int(m.group(1))}p"
    else:
        m = re.search(r"\b([248]K)\b", stem, flags=re.I)
        resolution = m.group(1).upper() if m else ""

    langs = [x for x in FILENAME_LANGS if re.search(r"\b" + x + r"\b", stem, re.I)]
    m = re.search(r"\b(19[0-9]{2}|20[0-9]{2})\b", stem)
    year = m.group(1) if m else ""
    season, start, end = extract_season_episode(stem)

    quality = ""
    for q in QUALITIES:
        if re.search(r"\b" + re.escape(q) + r"\b", stem, re.I):
            quality = q
            break
    ott = ""
    for p in OTT:
        if re.search(r"\b" + re.escape(p) + r"\b(?=\s|$)", stem, re.I) and quality.lower() != "bluray":
            ott = p
            break
    m = re.search(r"(x264|x265|H\.?264|H\.?265|HEVC)", stem, re.I)
    lib = ""
    if m:
        lib = "x265" if ("265" in m.group(0) or "hevc" in m.group(0).lower()) else "x264"

    m = re.search(r"^(.*?)(?=\s*(?:S\d+E\d+|S\d+|E\d+|\d{4}(?!\d)|\d+p|$))", stem)
    name = (m.group(1) if m else stem).strip(" .-")
    part = ""
    m = re.search(r"\.part0*(\d{1,3})\b", original_name, flags=re.I)
    if m:
        part = m.group(1)

    return {
        "name": name, "year": year, "season": season, "start": start, "end": end, "audio": " ".join(audio) or "AAC",
        "quality": quality, "resolution": resolution, "ott": ott, "languages": langs, "lib": lib, "part": part,
    }


# ----------------------------------------------------------------- media probing
async def _ffprobe(path):
    try:
        p = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(p.communicate(), 60)
        return json.loads(out.decode(errors="ignore") or "{}")
    except Exception:  # noqa: BLE001
        return {}


def _lang_name(code):
    code = (code or "").strip().lower()
    if code in SKIP_LANGS:
        return ""
    return LANG_NAMES.get(code) or LANG_NAMES.get(code.split("-")[0]) or code.upper()


def _uniq(items):
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _res_class(width, height):
    eff = max(height, int(width * 9 / 16))
    for limit, label in (
        (260, "240p"), (400, "360p"), (500, "480p"), (620, "576p"), (800, "720p"),
        (1150, "1080p"), (1500, "1440p"), (2300, "2160p"), (4500, "4320p"),
    ):
        if eff <= limit:
            return label
    return "4320p"


def _codec_name(name):
    return {"h264": "x264", "hevc": "x265", "av1": "AV1", "vp9": "VP9", "mpeg4": "MPEG4"}.get(name, (name or "").upper())


def _media_facts(info):
    streams = info.get("streams") or []
    try:
        duration = int(float((info.get("format") or {}).get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    probed_res = codec = ""
    if video:
        probed_res = _res_class(int(video.get("width") or 0), int(video.get("height") or 0))
        codec = _codec_name(video.get("codec_name"))
    audio_langs = _uniq(
        _lang_name((s.get("tags") or {}).get("language")) for s in streams if s.get("codec_type") == "audio"
    )
    sub_langs = _uniq(
        _lang_name((s.get("tags") or {}).get("language")) for s in streams if s.get("codec_type") == "subtitle"
    )
    return duration, probed_res, codec, audio_langs, sub_langs


def _hms(sec):
    h, r = divmod(int(sec), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def short_lang(languages):
    if len(languages) == 1:
        return languages[0]
    if len(languages) == 2:
        return "Dual"
    return "Multi" if languages else ""


# ----------------------------------------------------------------- name building
def sanitize(name, ext=""):
    if not name:
        return "renamed_file" + ext
    name = re.sub(r"<[^>]+>", "", name)
    if ext:
        idx = name.lower().find(ext.lower())
        if idx != -1:
            name = name[:idx]
        base = name
    else:
        base, ext = os.path.splitext(name)
    base = re.sub(r'[<>:"|?*\0/\\]', "", base).strip()
    base = re.sub(r"\s+", " ", base).rstrip(". ").lstrip(".")
    if not base:
        base = "renamed_file"
    if len(base.encode("utf-8")) > 235:
        while len(base.encode("utf-8")) > 235:
            base = base[:-1]
        base = base.rstrip(". ")
    return base + ext


async def auto_renamed(template, original_name, path):
    """Render ``template`` for ``original_name``. Falls back to the original name on any failure."""
    if not template:
        return original_name
    try:
        info = parse_name(original_name)
        raw_name, extension = os.path.splitext(original_name)
        duration, probed_res, codec, audio_langs, sub_langs = _media_facts(await _ffprobe(path))

        resolution = info["resolution"] or probed_res
        languages = audio_langs or info["languages"]
        episode = f"{info['start']}-{info['end'][1:]}" if info["end"] else info["start"]
        short_sub = "" if not sub_langs else (f"{sub_langs[0][:1].upper()}Sub" if len(sub_langs) == 1 else "MSub")

        formatted = template.replace(" ", "\u03c0").format_map(
            _SafeDict(
                name=info["name"], year=info["year"], season=info["season"], episode=episode,
                audio=info["audio"], quality=info["quality"], resolution=resolution, ott=info["ott"],
                languages=" ".join(languages), subtitles=" ".join(sub_langs),
                shortlang=short_lang(languages), shortsub=short_sub, lib=codec or info["lib"],
                duration=_hms(duration) if duration else "", extension=extension,
                raw_name=raw_name, file_name=original_name, file_caption=original_name,
            )
        )
        if not formatted.strip():
            return original_name
        new_name = re.sub(r"[^\S\n]+", " ", formatted).strip().replace("\u03c0", " ")
        if info["part"]:
            base, ext = os.path.splitext(new_name)
            new_name = f"{base} Part {info['part']}{ext}"
        return sanitize(new_name, extension)
    except Exception:  # noqa: BLE001
        return original_name
