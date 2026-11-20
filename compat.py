"""Library compatibility layer.

Uses ``wzgram`` (the "WzPyro" fork used by WZML-X) when it is installed and falls back to any
Pyrogram-compatible fork otherwise. It also hides every API difference between those forks:

* coloured inline buttons (green / red / blue) - auto-detected, plain buttons if unsupported
* link-preview / reply-parameter keyword differences
* safe send / reply / edit helpers
"""
import inspect
import logging
from importlib import import_module, metadata

log = logging.getLogger("leechbot.compat")

try:
    import wzgram as tg
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    import pyrogram as tg

Client = tg.Client
filters = tg.filters
enums = tg.enums
raw = tg.raw
types = import_module(f"{tg.__name__}.types")
errors = import_module(f"{tg.__name__}.errors")

StopTransmission = getattr(tg, "StopTransmission", None) or getattr(errors, "StopTransmission", None)
if StopTransmission is None:  # extremely defensive: never matches anything

    class StopTransmission(Exception):  # type: ignore
        pass


def _lib_label():
    try:
        return f"WzPyro v{metadata.version('wzgram')}"
    except metadata.PackageNotFoundError:
        pass
    for dist in ("kurigram", "pyrofork", "pyrogram"):
        try:
            return f"Pyro v{metadata.version(dist)}"
        except metadata.PackageNotFoundError:
            continue
    return f"Pyro v{getattr(tg, '__version__', '?')}"


LIB_LABEL = _lib_label()


def _params(fn):
    try:
        return set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return set()


_SEND_PARAMS = _params(Client.send_message)

# ----------------------------------------------------------------- keyword differences
if "link_preview_options" in _SEND_PARAMS and hasattr(types, "LinkPreviewOptions"):
    NOPREVIEW = {"link_preview_options": types.LinkPreviewOptions(is_disabled=True)}
elif "disable_web_page_preview" in _SEND_PARAMS:
    NOPREVIEW = {"disable_web_page_preview": True}
else:
    NOPREVIEW = {}


def reply_kwargs(message_id):
    """Keyword arguments that make a send_* call reply to ``message_id`` on any fork."""
    if "reply_parameters" in _SEND_PARAMS and hasattr(types, "ReplyParameters"):
        return {"reply_parameters": types.ReplyParameters(message_id=message_id)}
    if "reply_to_message_id" in _SEND_PARAMS:
        return {"reply_to_message_id": message_id}
    return {}


# ----------------------------------------------------------------- coloured buttons
GREEN = "success"
RED = "danger"
BLUE = "primary"

_MAKER = None  # callable(name) -> style value, chosen by init_styles()
STYLES_ON = False


def _m_enum(name):
    style_enum = getattr(enums, "ButtonStyle", None)
    return getattr(style_enum, name.upper()) if style_enum else None


def _m_types(name):
    cls = getattr(types, "KeyboardButtonStyle", None)
    return cls(**{f"bg_{name}": True}) if cls else None


def _m_raw(name):
    cls = getattr(raw.types, "KeyboardButtonStyle", None)
    return cls(**{f"bg_{name}": True}) if cls else None


def _m_str(name):
    return name


async def init_styles(client):
    """Detect how this library exposes button colours. Safe to call once at startup."""
    global _MAKER, STYLES_ON
    for maker in (_m_enum, _m_types, _m_raw, _m_str):
        try:
            value = maker(GREEN)
            if value is None:
                continue
            btn = types.InlineKeyboardButton("t", callback_data="t", style=value)
            res = btn.write(client)
            if inspect.isawaitable(res):
                await res
            _MAKER, STYLES_ON = maker, True
            log.info("Coloured buttons enabled (%s)", maker.__name__)
            return True
        except Exception as e:  # noqa: BLE001 - probing
            log.debug("button style probe %s failed: %r", maker.__name__, e)
    log.info("This library has no coloured button support - using plain buttons")
    return False


def B(text, data=None, style=None, url=None):
    """Button spec: B('Label', 'callback_data', GREEN) or B('Label', url='https://...')."""
    return (text, data, url, style)


def _btn(spec):
    text, data, url, style = spec
    kw = {"url": url} if url else {"callback_data": data}
    if style and STYLES_ON and _MAKER is not None:
        value = _MAKER(style)
        if value is not None:
            kw["style"] = value
    return types.InlineKeyboardButton(text, **kw)


def KB(rows):
    return types.InlineKeyboardMarkup([[_btn(b) for b in row] for row in rows])


def _markup_error(e):
    if isinstance(e, (TypeError, AttributeError, ValueError)):
        return True
    s = str(e).upper()
    return any(x in s for x in ("MARKUP", "BUTTON", "STYLE"))


async def _call(make, rows):
    """Run make(markup). If the buttons' colour style is rejected, retry once with plain buttons."""
    global STYLES_ON
    try:
        return await make(KB(rows) if rows else None)
    except Exception as e:  # noqa: BLE001
        if rows and STYLES_ON and _MAKER is not None and _markup_error(e):
            STYLES_ON = False
            log.warning("Button styles were rejected (%r) - falling back to plain buttons", e)
            return await make(KB(rows))
        raise


async def reply(m, text, rows=None, **kw):
    return await _call(lambda mk: m.reply_text(text, reply_markup=mk, **NOPREVIEW, **kw), rows)


async def send(client, chat_id, text, rows=None, **kw):
    return await _call(lambda mk: client.send_message(chat_id, text, reply_markup=mk, **NOPREVIEW, **kw), rows)


async def edit(msg, text, rows=None):
    """Edit a message; silently ignores 'message is not modified'."""
    try:
        return await _call(lambda mk: msg.edit_text(text, reply_markup=mk, **NOPREVIEW), rows)
    except errors.MessageNotModified:
        return msg
