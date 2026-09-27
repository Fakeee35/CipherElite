# =============================================================================
#  CipherElite Userbot Plugin
#
#  Plugin Name:   style_picker   (commands: .styles / .stylepicker / .aistyle)
#  Author:        CipherElite Dev (@rishabhops)
#  Repository:    https://github.com/rishabhops/CipherElite
#  Support:       @thanosceo
#
#  License:       MIT
# =============================================================================

import asyncio
import html as html_lib
import re
import time
from datetime import datetime

import aiohttp
from telethon import events, Button, version

from config.config import Config
from utils.decorators import rishabh
from utils.utils import CipherElite
from plugins.bot import add_handler, CMD_LIST
from plugins.alive import (
    ALIVE_STYLES,
    PING_STYLES,
    START_TIME,
    get_random_quote,
    get_readable_time,
    save_config,
    user_config,
)

CATEGORY = "developer"

# (chat_id, message_id) -> session dict
SESSIONS = {}

_CACHED_NAME = None

_SPINNER = ["⠋", "", "", "⠸", "⠼", "", "⠦", "⠧", "⠇", ""]

# =============================================================================
#  AI PROVIDERS  (same two APIs as plugins/aiplugingen.py)
# =============================================================================
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)

PROVIDERS = [
    {
        "name": "codexapi",
        "type": "get_query",
        "base_url": getattr(Config, "CODEXAPI_URL", "https://chatbot.codexapi.workers.dev"),
        # strongest / most code-capable first (shortened subset of the
        # aiplugingen list so the UI stays snappy), failover order preserved
        "models": [
            "gpt-5.2",
            "gpt-5.1",
            "gpt-5",
            "o1-preview",
            "o3-mini",
            "chatgpt-4o-latest",
            "anthropic/claude-sonnet-4",
            "deepseek-ai/deepseek-v3.2",
            "qwen/qwen3-coder-480b-a35b-instruct",
            "qwen/qwen3-235b-a22b",
            "x-ai/grok-4",
            "google/gemini-2.5-pro-preview-05-06",
        ],
    },
    {
        "name": "copilotapi",
        "type": "openai_chat",
        "base_url": getattr(Config, "COPILOT_API_URL", "https://copilot-api-delta.vercel.app"),
        "models": ["copilot"],
    },
]

AI_SYSTEM_ALIVE = (
    "You are a Telegram message style designer. Generate ONE beautiful "
    "Telegram HTML style template for a userbot ALIVE status message. "
    "Rules: use ONLY these placeholders, exactly as written: {name} "
    "{version} {telethon} {plugins} {uptime} {branch} {quote}. Use ONLY "
    "Telegram HTML entities (<b> <i> <code> <blockquote>) plus unicode "
    "symbols and emoji for decoration. No markdown, no code fences, no "
    "explanation. Output ONLY the template, under 600 characters."
)

AI_SYSTEM_PING = (
    "You are a Telegram message style designer. Generate ONE beautiful "
    "Telegram HTML style template for a userbot PING message. Rules: use "
    "ONLY these placeholders, exactly as written: {speed} {uptime}. Use "
    "ONLY Telegram HTML entities (<b> <i> <code> <blockquote>) plus unicode "
    "symbols and emoji. No markdown, no code fences, no explanation. Output "
    "ONLY the template, under 300 characters."
)

# placeholders the real .alive/.ping renderers fill in
_ALLOWED_FIELDS = {
    "name", "version", "telethon", "plugins", "uptime", "branch", "quote", "speed",
}


async def _call_get_query(session, base_url, model, full_prompt):
    url = f"{base_url.rstrip('/')}/"
    params = {"prompt": full_prompt, "model": model}
    async with session.get(url, params=params, timeout=REQUEST_TIMEOUT) as resp:
        resp.raise_for_status()
        text = await resp.text()
        try:
            import json
            data = json.loads(text)
        except Exception:
            return text
        for key in ("response", "text", "content", "message", "output", "result", "answer"):
            if isinstance(data, dict) and data.get(key):
                val = data[key]
                return val if isinstance(val, str) else json.dumps(val)
        return json.dumps(data)


async def _call_openai_chat(session, base_url, model, system_prompt, user_prompt):
    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    async with session.post(url, json=payload, timeout=REQUEST_TIMEOUT) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["choices"][0]["message"]["content"]


async def _ai_generate(system_prompt, user_prompt, attempt_cb=None):
    """Try every provider/model in order (codexapi first, copilotapi fallback).
    `attempt_cb` is awaited with a status line per model attempt so the UI can
    show what is being tried. Returns (raw_text, provider_name, model)."""
    combined = f"{system_prompt}\n\nUser request: {user_prompt}"
    async with aiohttp.ClientSession() as session:
        for provider in PROVIDERS:
            for model in provider["models"]:
                if attempt_cb:
                    await attempt_cb(f"trying {provider['name']} → {model} …")
                try:
                    if provider["type"] == "get_query":
                        raw = await _call_get_query(
                            session, provider["base_url"], model, combined
                        )
                    else:
                        raw = await _call_openai_chat(
                            session, provider["base_url"], model,
                            system_prompt, user_prompt,
                        )
                    if raw and raw.strip():
                        return raw, provider["name"], model
                except Exception:
                    continue  # this provider/model failed -> fall through
    return None, None, None


def _extract_template(raw):
    """Pull the template out of an AI reply (strips code fences if present)."""
    m = re.search(r"```(?:html|HTML)?\s*(.*?)```", raw, re.S)
    body = m.group(1) if m else raw
    return body.strip()[:1500]


def _sanitize_template(tpl):
    """Make AI output safe for Telegram + `.format()`:
    <br> -> newline (Telegram HTML has no <br>), and every brace except the
    allowed placeholders is escaped so format() can never crash."""
    tpl = re.sub(r"(?i)<br\s*/?>", "\n", tpl)
    doubled = tpl.replace("{", "{{").replace("}", "}}")
    for key in _ALLOWED_FIELDS:
        doubled = doubled.replace("{{" + key + "}}", "{" + key + "}")
    return doubled


# =============================================================================
#  LIVE GENERATION UI  (spinner while waiting + typewriter reveal)
# =============================================================================
async def _safe_edit(message, text):
    try:
        await message.edit(text, parse_mode="html")
    except Exception:
        pass


async def _live_generate(message, kind, prompt):
    """Run the AI generation against `message` with a live UI.

    Phase 1: spinner + elapsed seconds + the provider/model being tried.
    Phase 2: the AI's raw template revealed typewriter-style.
    Returns (sanitized_template, provider, model) or (None, None, None).
    """
    system = AI_SYSTEM_ALIVE if kind == "alive" else AI_SYSTEM_PING
    attempt = {"text": "contacting providers …"}

    async def attempt_cb(text):
        attempt["text"] = text

    stop = asyncio.Event()
    t0 = time.monotonic()
    state = {"frame": 0}

    async def ticker():
        while not stop.is_set():
            state["frame"] += 1
            secs = int(time.monotonic() - t0)
            await _safe_edit(
                message,
                f"🎨 <b>AI is designing your style…</b> "
                f"{_SPINNER[state['frame'] % len(_SPINNER)]} <code>{secs}s</code>\n"
                f"<i>{html_lib.escape(attempt['text'])}</i>",
            )
            try:
                await asyncio.wait_for(stop.wait(), 1.5)
            except asyncio.TimeoutError:
                pass

    ticker_task = asyncio.get_running_loop().create_task(ticker())
    try:
        raw, provider, model = await _ai_generate(system, prompt, attempt_cb)
    finally:
        stop.set()
        try:
            await asyncio.wait_for(ticker_task, 3)
        except Exception:
            ticker_task.cancel()

    if raw is None:
        await _safe_edit(
            message,
            "❌ <b>The AI APIs are not responding</b> "
            "(codexapi and copilotapi both failed). Please try again in a bit.",
        )
        return None, None, None

    # typewriter reveal of what the AI wrote
    template_text = _extract_template(raw)
    steps = 8
    for i in range(1, steps + 1):
        part = template_text[: len(template_text) * i // steps]
        await _safe_edit(
            message,
            f"🤖 <b>AI is writing…</b> <code>{provider}/{model}</code>\n"
            f"<pre>{html_lib.escape(part)}</pre>",
        )
        await asyncio.sleep(0.9)

    return _sanitize_template(template_text), provider, model


# =============================================================================
#  PREVIEW RENDERING  (mirrors plugins/alive.py exactly)
# =============================================================================
def _uptime_str():
    return get_readable_time((datetime.now() - START_TIME).total_seconds())


def _fill(tpl, name, speed):
    """Fill a template with EVERY known value — str.format ignores unused
    kwargs, so any placeholder the AI (or the user) puts in any style can
    never raise KeyError."""
    return tpl.format(
        name=name,
        speed=speed,
        telethon=version.__version__,
        plugins=len(CMD_LIST),
        uptime=_uptime_str(),
        version=Config.VERSION,
        branch=Config.BRANCH,
        quote=get_random_quote() if user_config.show_quotes else "",
    )


async def _preview_text(event, kind, tpl=None, idx=0):
    """Render a style with live values. `tpl` overrides the indexed style."""
    global _CACHED_NAME
    start = datetime.now()
    me = await event.client.get_me()
    elapsed_ms = max(1, (datetime.now() - start).microseconds // 1000)
    _CACHED_NAME = me.first_name or "Owner"

    if kind == "alive":
        template = tpl if tpl is not None else ALIVE_STYLES[idx % len(ALIVE_STYLES)]
    else:
        template = tpl if tpl is not None else PING_STYLES[idx % len(PING_STYLES)]
    return _fill(template, _CACHED_NAME, elapsed_ms)


def _media_for(kind):
    if kind == "alive":
        return user_config.alive_pic if user_config.use_pic_for_alive else None
    return user_config.ping_pic if user_config.use_pic_for_ping else None


# =============================================================================
#  UI BUILDERS
# =============================================================================
def _main_text():
    return (
        "🎨 <b>Style Studio</b>\n\n"
        f"💫 Alive style: <code>#{user_config.alive_style_index + 1}</code>   "
        f"🏓 Ping style: <code>#{user_config.ping_style_index + 1}</code>\n\n"
        "<i>Which styles do you want to browse live?</i>\n"
        "<i>Or create a brand-new one with AI:</i> <code>.aistyle &lt;prompt&gt;</code>"
    )


def _main_buttons():
    return [
        [
            Button.inline("💫 Alive Styles", b"sp_alive"),
            Button.inline("🏓 Ping Styles", b"sp_ping"),
        ]
    ]


def _browser_text(kind, idx, preview, just_set=False):
    styles = ALIVE_STYLES if kind == "alive" else PING_STYLES
    active = (
        user_config.alive_style_index if kind == "alive"
        else user_config.ping_style_index
    )
    icon = "💫" if kind == "alive" else "🏓"
    title = "Alive" if kind == "alive" else "Ping"
    mark = "  ✅ <b>ACTIVE</b>" if idx == active else ""
    head = f"{icon} <b>{title} Style {idx + 1}/{len(styles)}</b>{mark}"
    if just_set:
        head += f"\n<i>Set! .{kind} will now use this style.</i>"
    return f"{head}\n\n{preview}\n\n<i>◀ ▶ browse · ✅ set · Back</i>"


def _browser_buttons():
    return [
        [
            Button.inline("◀", b"sp_prev"),
            Button.inline("✅", b"sp_set"),
            Button.inline("▶", b"sp_next"),
        ],
        [Button.inline("⬅️ Back", b"sp_back")],
    ]


def _ai_text(kind, prompt, preview, provider, model, just_set=False):
    icon = "💫" if kind == "alive" else "🏓"
    head = f"🎨 <b>AI {icon} Style</b> — <code>{provider}/{model}</code>"
    head += f"\n<i>Prompt: {html_lib.escape(prompt[:80])}</i>"
    if just_set:
        head += f"\n✅ <b>SET!</b> <i>.{kind} will now use this AI style.</i>"
    return f"{head}\n\n{preview}\n\n<i>✅ set · 🔄 regenerate · Back</i>"


def _ai_buttons():
    return [
        [
            Button.inline("✅ Set", b"sp_ai_set"),
            Button.inline("🔄", b"sp_ai_new"),
            Button.inline("⬅️ Back", b"sp_ai_back"),
        ]
    ]


# =============================================================================
#  SESSION HELPERS
# =============================================================================
def _session_key(event):
    return (event.chat_id, event.msg_id)


def _remember(key, sess):
    if len(SESSIONS) > 300:
        SESSIONS.clear()
    SESSIONS[key] = sess


def _forget(key):
    SESSIONS.pop(key, None)


# =============================================================================
#  PLUGIN INTERFACE
# =============================================================================
def init(client_instance):
    add_handler(
        "style_picker",
        [
            ".styles - Live inline browser to preview & set .alive/.ping styles",
            ".stylepicker - Same as .styles",
            ".aistyle <prompt> - AI designs a new alive style, live preview + set",
            ".aistyle ping <prompt> - Same, for ping styles",
        ],
        "Style Studio - browse alive/ping styles live with the arrow and set "
        "buttons, or generate a brand-new style from a prompt with AI "
        "(codexapi + copilotapi failover) and set it with one tap.",
    )


async def register_commands():

    # -------------------------------------------------------------------------
    # .styles / .stylepicker - open the picker
    # -------------------------------------------------------------------------
    @CipherElite.on(events.NewMessage(pattern=r"\.(?:styles|stylepicker)$"))
    @rishabh()
    async def open_picker(event):
        await event.reply(_main_text(), buttons=_main_buttons(), parse_mode="html")

    # -------------------------------------------------------------------------
    # .aistyle [ping] <prompt> - AI style generator with live UI
    # -------------------------------------------------------------------------
    @CipherElite.on(events.NewMessage(pattern=r"\.aistyle(?:\s+(.+))?$"))
    @rishabh()
    async def aistyle_handler(event):
        args = (event.pattern_match.group(1) or "").strip()

        kind = "alive"
        if args.lower().startswith("ping "):
            kind = "ping"
            args = args[5:].strip()
        if not args:
            await event.reply(
                "🎨 <b>Usage:</b>\n"
                "<code>.aistyle cyberpunk neon terminal</code>\n"
                "<code>.aistyle ping minimal clean</code>",
                parse_mode="html",
            )
            return

        status = await event.reply(
            f"🎨 <b>AI is designing your style…</b>\n<i>{html_lib.escape(args[:80])}</i>",
            parse_mode="html",
        )

        template, provider, model = await _live_generate(status, kind, args)
        if template is None:
            return

        preview = await _preview_text(event, kind, tpl=template)
        try:
            await status.delete()
        except Exception:
            pass
        sent = await event.respond(
            _ai_text(kind, args, preview, provider, model),
            buttons=_ai_buttons(),
            parse_mode="html",
        )
        _remember(
            (event.chat_id, sent.id),
            {"mode": "ai", "kind": kind, "template": template, "prompt": args},
        )

    # -------------------------------------------------------------------------
    # Callback router: sp_<action>
    # -------------------------------------------------------------------------
    @CipherElite.on(events.CallbackQuery(pattern=b"sp_(.*)"))
    @rishabh()
    async def picker_callback(event):
        action = event.data_match.group(1).decode()
        key = _session_key(event)

        # -- mode selection from the main menu -------------------------------
        if action in ("alive", "ping"):
            idx = (
                user_config.alive_style_index if action == "alive"
                else user_config.ping_style_index
            )
            try:
                await event.delete()
            except Exception:
                pass
            preview = await _preview_text(event, action, idx=idx)
            sent = await event.respond(
                _browser_text(action, idx, preview),
                file=_media_for(action),
                buttons=_browser_buttons(),
                parse_mode="html",
            )
            _remember((event.chat_id, sent.id),
                      {"mode": "browse", "kind": action, "idx": idx})
            return

        sess = SESSIONS.get(key)
        if sess is None:
            await event.answer("Session expired - send .styles again", alert=True)
            return

        # -- AI preview actions ----------------------------------------------
        if sess.get("mode") == "ai":
            kind = sess["kind"]
            if action == "ai_set":
                if kind == "alive":
                    user_config.custom_alive_text = sess["template"]
                else:
                    user_config.custom_ping_text = sess["template"]
                save_config()
                preview = await _preview_text(event, kind, tpl=sess["template"])
                await event.edit(
                    _ai_text(kind, sess["prompt"], preview, "saved", "saved", just_set=True),
                    buttons=_ai_buttons(),
                    parse_mode="html",
                )
                await event.answer(f"✅ AI {kind} style set!", alert=False)
            elif action == "ai_new":
                # regenerate in place with the same live UI
                template, provider, model = await _live_generate(
                    event, kind, sess["prompt"]
                )
                if template is None:
                    await event.answer("❌ AI failed - try again", alert=True)
                    return
                sess["template"] = template
                preview = await _preview_text(event, kind, tpl=template)
                await event.edit(
                    _ai_text(kind, sess["prompt"], preview, provider, model),
                    buttons=_ai_buttons(),
                    parse_mode="html",
                )
            elif action == "ai_back":
                _forget(key)
                try:
                    await event.delete()
                except Exception:
                    pass
            return

        # -- built-in style browser actions -----------------------------------
        kind, idx = sess["kind"], sess["idx"]
        styles = ALIVE_STYLES if kind == "alive" else PING_STYLES

        if action == "prev":
            idx = (idx - 1) % len(styles)
        elif action == "next":
            idx = (idx + 1) % len(styles)
        elif action == "set":
            if kind == "alive":
                user_config.alive_style_index = idx
                user_config.custom_alive_text = None   # built-in style wins again
            else:
                user_config.ping_style_index = idx
                user_config.custom_ping_text = None
            save_config()
            preview = await _preview_text(event, kind, idx=idx)
            await event.edit(
                _browser_text(kind, idx, preview, just_set=True),
                buttons=_browser_buttons(),
                parse_mode="html",
            )
            await event.answer(f"✅ {kind.title()} style #{idx + 1} set!", alert=False)
            return
        elif action == "back":
            _forget(key)
            try:
                await event.delete()
            except Exception:
                pass
            await event.respond(_main_text(), buttons=_main_buttons(), parse_mode="html")
            return
        else:
            return

        # prev/next -> live re-render in place (media stays attached)
        sess["idx"] = idx
        preview = await _preview_text(event, kind, idx=idx)
        await event.edit(
            _browser_text(kind, idx, preview),
            buttons=_browser_buttons(),
            parse_mode="html",
        )
