# =============================================================================
#  CipherElite Userbot Plugin
#
#  Plugin Name:   style_picker
#  Commands:      .styles / .stylepicker / .new_alive_style / .new_ping_style
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
import uuid
from datetime import datetime

import aiohttp
from telethon import events, Button, version

from config.config import Config
from utils.decorators import rishabh
from utils.utils import CipherElite
from plugins.bot import add_handler, CMD_LIST, bot
from plugins.alive import (
    ALIVE_STYLES,
    PING_STYLES,
    START_TIME,
    get_random_quote,
    get_readable_time,
    save_config,
    user_config,
)

CATEGORY = "utilities"

# sessions for bot-side callbacks, keyed by (chat_id, message_id)
SESSIONS = {}
# AI results waiting to be posted, keyed by a short token
AI_STORE = {}
# who may use the menus (filled from SUDO_USERS + whoever runs the commands)
OWNER_IDS = set(getattr(Config, "SUDO_USERS", []) or [])

_CACHED_OWNER_NAME = None

_SPINNER = ["⠋", "⠙", "", "", "⠼", "⠴", "", "", "⠇", "⠏"]

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
#  Runs on plain userbot text messages - no buttons needed here.
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
    """Fill a template with EVERY known value - str.format ignores unused
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


async def _preview_text(client, kind, name, tpl=None, idx=0):
    """Render a style with live values. `tpl` overrides the indexed style."""
    start = datetime.now()
    try:
        await client.get_me()
    except Exception:
        pass
    elapsed_ms = max(1, (datetime.now() - start).microseconds // 1000)

    if kind == "alive":
        template = tpl if tpl is not None else ALIVE_STYLES[idx % len(ALIVE_STYLES)]
    else:
        template = tpl if tpl is not None else PING_STYLES[idx % len(PING_STYLES)]
    return _fill(template, name or "Owner", elapsed_ms)


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
        "<i>Or create a brand-new one with AI:</i> "
        "<code>.new_alive_style &lt;prompt&gt;</code>"
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


def _ai_buttons(token):
    # the AI result token travels inside the callback data, so the bot can
    # find the generated style no matter where/when the button is pressed
    t = token.encode()
    return [
        [
            Button.inline("✅ Set", b"sp_aiset_" + t),
            Button.inline("🔄", b"sp_ainew_" + t),
            Button.inline("⬅️ Back", b"sp_aiback_" + t),
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


async def _open_inline(event, payload, fb_text=None, fb_buttons=None):
    """Post an interactive menu through the helper bot (user accounts cannot
    send inline buttons).

    Layer 1: inline query + click - the same trick plugins/alive.py uses.
    Layer 2: if the inline round-trip fails for any reason (bot not started,
             inline mode off, network hiccup), the bot sends the message
             itself - a message sent by a BOT always keeps its buttons.
    """
    try:
        results = await event.client.inline_query(Config.TG_BOT_USERNAME, payload)
        return await results[0].click(
            event.chat_id,
            reply_to=event.reply_to_msg_id,
            hide_via=True,
        )
    except Exception:
        if bot is not None and fb_text is not None:
            return await bot.send_message(
                event.chat_id,
                fb_text,
                buttons=fb_buttons,
                parse_mode="html",
                reply_to=event.reply_to_msg_id,
            )
        raise


# =============================================================================
#  PLUGIN INTERFACE
# =============================================================================
def init(client_instance):
    add_handler(
        "style_picker",
        [
            ".styles - Live inline browser to preview & set .alive/.ping styles",
            ".stylepicker - Same as .styles",
            ".new_alive_style <prompt> - AI designs a new alive style, live preview + set",
            ".new_ping_style <prompt> - Same, for ping styles",
        ],
        "Style Studio - browse alive/ping styles live with the arrow and set "
        "buttons, or generate a brand-new style from a prompt with AI "
        "(codexapi + copilotapi failover) and set it with one tap.",
    )


async def register_commands():

    def _iq_safe(fn):
        """Never let a bot-side inline handler die silently: on any error the
        user sees the error text inside the inline result instead of getting
        an empty/no-result query (which would look like 'buttons missing')."""
        async def wrap(event):
            try:
                return await fn(event)
            except Exception as e:
                try:
                    await event.answer(
                        [event.builder.article(
                            "⚠️ Error",
                            text=f"⚠️ <code>{type(e).__name__}: {e}</code>",
                            parse_mode="html",
                        )],
                        cache_time=1,
                    )
                except Exception:
                    pass
        return wrap

    # -------------------------------------------------------------------------
    # .styles / .stylepicker - open the picker through the bot
    # -------------------------------------------------------------------------
    @CipherElite.on(events.NewMessage(pattern=r"\.(?:styles|stylepicker)$"))
    @rishabh()
    async def open_picker(event):
        OWNER_IDS.add(event.sender_id)
        if event.sender and event.sender.first_name:
            global _CACHED_OWNER_NAME
            _CACHED_OWNER_NAME = event.sender.first_name
        try:
            await _open_inline(event, "sp_menu", _main_text(), _main_buttons())
            await event.delete()
        except Exception:
            await event.reply(
                _main_text() +
                "\n\n⚠️ <i>The interactive menu needs the helper bot's inline "
                "mode. Enable it via @BotFather if it is disabled.</i>",
                parse_mode="html",
            )

    # -------------------------------------------------------------------------
    # .new_alive_style / .new_ping_style - AI generator with live typewriter
    # -------------------------------------------------------------------------
    async def _aistyle_flow(event, kind, prompt):
        OWNER_IDS.add(event.sender_id)
        if event.sender and event.sender.first_name:
            global _CACHED_OWNER_NAME
            _CACHED_OWNER_NAME = event.sender.first_name

        status = await event.reply(
            f"🎨 <b>AI is designing your style…</b>\n<i>{html_lib.escape(prompt[:80])}</i>",
            parse_mode="html",
        )

        template, provider, model = await _live_generate(status, kind, prompt)
        if template is None:
            return

        token = uuid.uuid4().hex[:10]
        AI_STORE[token] = {
            "kind": kind,
            "template": template,
            "prompt": prompt,
            "provider": provider,
            "model": model,
        }
        if len(AI_STORE) > 100:
            AI_STORE.clear()

        preview = await _preview_text(
            event.client, kind, _CACHED_OWNER_NAME, tpl=template
        )
        ai_text = _ai_text(kind, prompt, preview, provider, model)
        try:
            await _open_inline(event, f"sp_ai_{token}", ai_text, _ai_buttons(token))
            await status.delete()
        except Exception:
            # both delivery layers failed -> plain text preview + hint
            await _safe_edit(
                status,
                ai_text
                + "\n\n⚠️ <i>Buttons need the helper bot - check TG_BOT_USERNAME "
                "in vars.py and make sure you have started your bot once.</i>",
            )

    @CipherElite.on(events.NewMessage(pattern=r"\.new_alive_style(?:\s+(.+))?$"))
    @rishabh()
    async def new_alive_style(event):
        prompt = (event.pattern_match.group(1) or "").strip()
        if not prompt:
            await event.reply(
                "🎨 <b>Usage:</b> <code>.new_alive_style cyberpunk neon terminal</code>",
                parse_mode="html",
            )
            return
        await _aistyle_flow(event, "alive", prompt)

    @CipherElite.on(events.NewMessage(pattern=r"\.new_ping_style(?:\s+(.+))?$"))
    @rishabh()
    async def new_ping_style(event):
        prompt = (event.pattern_match.group(1) or "").strip()
        if not prompt:
            await event.reply(
                "🎨 <b>Usage:</b> <code>.new_ping_style minimal clean gold</code>",
                parse_mode="html",
            )
            return
        await _aistyle_flow(event, "ping", prompt)

    # =========================================================================
    #  BOT SIDE - menus, previews and callbacks (bots CAN send buttons)
    # =========================================================================
    if bot:

        @bot.on(events.InlineQuery(pattern=r"^sp_menu$"))
        @_iq_safe
        async def iq_menu(event):
            if event.sender_id not in OWNER_IDS:
                return await event.answer(
                    [event.builder.article(" Owner only", text=" Owner only.")], cache_time=1,
                )
            await event.answer([
                event.builder.article(
                    "🎨 Style Studio",
                    text=_main_text(),
                    buttons=_main_buttons(),
                    parse_mode="html",
                )
            ], cache_time=1)

        @bot.on(events.InlineQuery(pattern=r"^sp_browser_(alive|ping)$"))
        @_iq_safe
        async def iq_browser(event):
            if event.sender_id not in OWNER_IDS:
                return await event.answer(
                    [event.builder.article("⛔ Owner only", text="⛔ Owner only.")], cache_time=1,
                )
            kind = event.pattern_match.group(1).decode()
            idx = (
                user_config.alive_style_index if kind == "alive"
                else user_config.ping_style_index
            )
            preview = await _preview_text(bot, kind, _CACHED_OWNER_NAME, idx=idx)
            text = _browser_text(kind, idx, preview)
            media = _media_for(kind)
            if media:
                result = event.builder.photo(
                    media, text=text, buttons=_browser_buttons(), parse_mode="html"
                )
            else:
                result = event.builder.article(
                    "Style browser", text=text,
                    buttons=_browser_buttons(), parse_mode="html",
                )
            await event.answer([result], cache_time=1)

        @bot.on(events.InlineQuery(pattern=r"^sp_ai_([a-f0-9]+)$"))
        @_iq_safe
        async def iq_ai(event):
            if event.sender_id not in OWNER_IDS:
                return await event.answer(
                    [event.builder.article("⛔ Owner only", text="⛔ Owner only.")], cache_time=1,
                )
            data = AI_STORE.get(event.pattern_match.group(1).decode())
            if not data:
                return await event.answer(
                    [event.builder.article(
                        "⏳ Expired",
                        text="⏳ <i>That AI style expired - run the command again.</i>",
                        parse_mode="html",
                    )], cache_time=1,
                )
            token = event.pattern_match.group(1).decode()
            preview = await _preview_text(
                bot, data["kind"], _CACHED_OWNER_NAME, tpl=data["template"]
            )
            await event.answer([
                event.builder.article(
                    "🎨 AI Style",
                    text=_ai_text(
                        data["kind"], data["prompt"], preview,
                        data["provider"], data["model"],
                    ),
                    buttons=_ai_buttons(token),
                    parse_mode="html",
                )
            ], cache_time=1)

        @bot.on(events.CallbackQuery(pattern=b"sp_(.*)"))
        async def picker_callback(event):
            if event.sender_id not in OWNER_IDS:
                return await event.answer("⛔ This is only for the bot owner!", alert=True)

            action = event.data_match.group(1).decode()
            key = _session_key(event)

            # -- mode selection from the main menu -------------------------
            if action in ("alive", "ping"):
                idx = (
                    user_config.alive_style_index if action == "alive"
                    else user_config.ping_style_index
                )
                preview = await _preview_text(bot, action, _CACHED_OWNER_NAME, idx=idx)
                _remember(key, {"mode": "browse", "kind": action, "idx": idx})
                return await event.edit(
                    _browser_text(action, idx, preview),
                    buttons=_browser_buttons(),
                    parse_mode="html",
                )

            # -- AI preview actions (token travels inside the callback data) -
            ai_match = re.match(r"ai(set|new|back)_([a-f0-9]+)", action)
            if ai_match:
                verb, token = ai_match.group(1), ai_match.group(2)
                data = AI_STORE.get(token)
                if data is None:
                    return await event.answer(
                        "That AI style expired - run the command again", alert=True
                    )
                kind = data["kind"]

                if verb == "set":
                    if kind == "alive":
                        user_config.custom_alive_text = data["template"]
                    else:
                        user_config.custom_ping_text = data["template"]
                    save_config()
                    preview = await _preview_text(
                        bot, kind, _CACHED_OWNER_NAME, tpl=data["template"]
                    )
                    await event.edit(
                        _ai_text(kind, data["prompt"], preview, "saved", "saved", just_set=True),
                        buttons=_ai_buttons(token),
                        parse_mode="html",
                    )
                    return await event.answer(f"✅ AI {kind} style set!", alert=False)

                if verb == "new":
                    await event.answer("🔄 Regenerating…")
                    system = AI_SYSTEM_ALIVE if kind == "alive" else AI_SYSTEM_PING
                    raw, provider, model = await _ai_generate(system, data["prompt"])
                    if raw is None:
                        return await event.answer("❌ AI failed - try again", alert=True)
                    data["template"] = _sanitize_template(_extract_template(raw))
                    data["provider"], data["model"] = provider, model
                    preview = await _preview_text(
                        bot, kind, _CACHED_OWNER_NAME, tpl=data["template"]
                    )
                    return await event.edit(
                        _ai_text(kind, data["prompt"], preview, provider, model),
                        buttons=_ai_buttons(token),
                        parse_mode="html",
                    )

                # back
                AI_STORE.pop(token, None)
                try:
                    await event.delete()
                except Exception:
                    pass
                return

            # -- built-in style browser actions -----------------------------
            sess = SESSIONS.get(key)
            if sess is None:
                return await event.answer("Session expired - send .styles again", alert=True)

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
                preview = await _preview_text(bot, kind, _CACHED_OWNER_NAME, idx=idx)
                await event.edit(
                    _browser_text(kind, idx, preview, just_set=True),
                    buttons=_browser_buttons(),
                    parse_mode="html",
                )
                return await event.answer(f"✅ {kind.title()} style #{idx + 1} set!", alert=False)
            elif action == "back":
                _forget(key)
                return await event.edit(
                    _main_text(), buttons=_main_buttons(), parse_mode="html"
                )
            else:
                return

            # prev/next -> live re-render in place
            sess["idx"] = idx
            preview = await _preview_text(bot, kind, _CACHED_OWNER_NAME, idx=idx)
            await event.edit(
                _browser_text(kind, idx, preview),
                buttons=_browser_buttons(),
                parse_mode="html",
            )
