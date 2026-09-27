# =============================================================================
#  CipherElite Assistant Bot Plugin
#
#  Plugin Name:    assistant
#  Version:        1.0.0
#  Author:         CipherElite Dev (@rishabhops)
#  Repository:     https://github.com/rishabhops/CipherElite
#
#  License:        MIT
# =============================================================================

import json
import re
from pathlib import Path
from telethon import events, Button
import html

VERSION = "1.0.0"

# Database file path
DB_PATH = Path(__file__).parent.parent / "DB" / "assistant_db.json"

# Global variables
bot_instance = None
owner_user_id = None
owner_display_name = None

def load_database():
    """Load assistant database from JSON file"""
    if DB_PATH.exists():
        try:
            with open(DB_PATH, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading assistant database: {e}")
    
    # Default database structure
    return {
        "assistant_enabled": False,
        "users": [],
        "user_message_map": {},
        "stats": {
            "total_messages": 0,
            "total_replies": 0
        }
    }

def save_database(db):
    """Save assistant database to JSON file"""
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DB_PATH, 'w') as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        print(f"Error saving assistant database: {e}")

def add_user(user_id):
    """Add a user to the database safely, auto-repairing if JSON is corrupted"""
    db = load_database()
    
    # Safety Check 1: Ensure 'users' exists
    if "users" not in db:
        db["users"] = []
        
    # Safety Check 2: Auto-repair if the JSON file accidentally saved 'users' as a dict {}
    if isinstance(db["users"], dict):
        print("⚠️ Auto-repairing db['users'] from dict back to list in JSON file...")
        # Convert dictionary keys back into a list
        db["users"] = list(db["users"].keys())
        
    # Safely append now that we are 100% sure it is a list
    if user_id not in db["users"]:
        db["users"].append(user_id)
        save_database(db)
        
    return len(db["users"])

def get_stats():
    """Get bot statistics"""
    db = load_database()
    return {
        "users_count": len(db["users"]),
        "assistant_enabled": db["assistant_enabled"],
        "total_messages": db["stats"]["total_messages"],
        "total_replies": db["stats"]["total_replies"]
    }

def get_bot_plugins_count():
    """Get the number of bot plugins installed"""
    bot_plugins_path = Path(__file__).parent
    return len([
        f for f in bot_plugins_path.glob("*.py")
        if f.stem != "__init__"
    ])


# =============================================================================
#  AUTO-DISCOVERED HELP CATEGORIES
#  Any bot plugin whose handlers are registered on the bot client gets its
#  slash commands listed under its own category in the /help menu — no manual
#  editing needed when new plugins are added.
# =============================================================================

# Modules whose commands are described manually in _CORE_CATEGORIES below,
# plus shared/helper modules that register nothing user-facing.
_SKIP_DISCOVERY = {
    "assistant", "alive_customize", "whisper_bot", "_shared", "__init__",
}

# Nice icons/titles for known plugin modules; anything else gets the fallback.
_DISCOVERED_META = {
    "moderation": ("⚖️", "Moderation"),
    "chatguard": ("🛡", "Chat Guard"),
    "joinmod": ("👋", "Join Moderation"),
    "tools": ("🧮", "Tools"),
    "adult_mode": ("🔞", "Adult Mode"),
}

_CORE_CATEGORIES = {
    "assistant": {
        "icon": "🤖",
        "title": "Assistant Commands",
        "commands": [
            ("/start", "Show main menu"),
            ("/help", "Show help menu"),
            ("/assistant", "Assistant status"),
            ("/assistant on", "Enable assistant mode"),
            ("/assistant off", "Disable assistant mode"),
            ("/assistant status", "Check assistant status"),
        ]
    },
    "alive": {
        "icon": "⚡",
        "title": "Alive / Ping Commands",
        "commands": [
            ("/alive", "Customize alive message"),
            ("/ping", "Customize ping message"),
            ("Buttons", "Change style, pic, quotes"),
        ]
    },
    "whisper": {
        "icon": "🤫",
        "title": "Whisper Commands",
        "commands": [
            (".w @username <text>", "Send secret whisper to user"),
            (".w <text> (reply)", "Send whisper by replying"),
            ("Button", "Target user clicks to view"),
        ]
    },
}


def _extract_cmds(pattern_str):
    """Pull the slash command name(s) out of a NewMessage regex pattern.

    Handles:  ^/ban(?:...)?$          -> ["/ban"]
              ^/(welcome|goodbye)$    -> ["/welcome", "/goodbye"]
              ^/set(welcome|goodbye)  -> ["/setwelcome", "/setgoodbye"]
              ^/warn(?:s|list|limit)? -> ["/warn", "/warns", ...]
    Returns [] for callback-data or non-slash patterns.
    """
    if not pattern_str or not pattern_str.startswith("^/"):
        return []
    body = pattern_str[2:]
    m = re.match(r"^([a-zA-Z_]*)", body)
    prefix = m.group(0)
    rest = body[len(prefix):]

    cmds = []
    alts = re.match(r"^\((?:\?:)?((?:[^()]+))\)\??", rest)
    if alts:
        parts = [p.strip() for p in alts.group(1).split("|")]
        wordish = [p for p in parts if p and re.fullmatch(r"[a-zA-Z_]+", p)]
        if wordish and len(wordish) == len(parts):
            # real alternation like (welcome|goodbye) or (?:s|list|limit)
            if rest.startswith("(?:"):
                cmds.append("/" + prefix)      # optional suffix: base counts
            for p in wordish:
                cmds.append("/" + prefix + p)
        else:
            cmds.append("/" + prefix)          # group holds args, not variants
    else:
        cmds.append("/" + prefix)

    return [c for c in cmds if len(c) > 1]


def _regex_of(builder, kind):
    """Pull the raw regex string out of a Telethon event builder.

    Telethon 1.37 stores NewMessage patterns as `compiled.match` (a bound
    method) — the regex itself lives on `.__self__.pattern`. CallbackQuery
    uses the same trick under `.match`, and compiles to bytes.
    """
    attr = "pattern" if kind == "NewMessage" else "match"
    m = getattr(builder, attr, None)
    regex = getattr(m, "__self__", None) if callable(m) else None
    pat = getattr(regex, "pattern", None) if regex is not None else None
    if isinstance(pat, bytes):
        pat = pat.decode()
    return pat


_LITERAL_BTN = re.compile(r"^[A-Za-z0-9_|()\- ]+$")

_ENTRY_ATTRS = ("init_bot_plugin", "init", "register")


def _call_entry(fn):
    """Call a plugin entry function with as many args as it accepts."""
    import inspect
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return fn(bot_instance)
    positional = [
        p for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(
        p.kind is inspect.Parameter.VAR_POSITIONAL for p in params
    )
    if has_varargs or len(positional) >= 3:
        return fn(bot_instance, owner_user_id, owner_display_name)
    if len(positional) == 2:
        return fn(bot_instance, owner_user_id)
    return fn(bot_instance)


async def _ensure_all_loaded():
    """Hot-load any bot plugin file that is not loaded yet.

    Runs whenever the help menu is opened, so a .py file dropped into
    bot_plugins/ shows up in the bot WITHOUT a restart. Plugins already
    loaded by plugins/bot.py at startup are skipped via the shared dedupe
    set. Helper modules without an entry function (e.g. _shared) are
    ignored.
    """
    import importlib
    import inspect

    if bot_instance is None:
        return
    if not hasattr(bot_instance, "_loaded_bot_plugins"):
        bot_instance._loaded_bot_plugins = set()
    if not hasattr(bot_instance, "_seen_bot_plugins"):
        bot_instance._seen_bot_plugins = set()
    loaded = bot_instance._loaded_bot_plugins
    seen = bot_instance._seen_bot_plugins

    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.stem == "__init__":
            continue
        mod_name = f"bot_plugins.{path.stem}"
        if mod_name in seen or mod_name in loaded:
            continue  # already attempted / already loaded at startup
        seen.add(mod_name)
        try:
            module = importlib.import_module(mod_name)
        except Exception as e:
            print(f"❌ Hot-load failed for {mod_name}: {e}")
            continue

        entry = None
        for attr in _ENTRY_ATTRS:
            candidate = getattr(module, attr, None)
            if callable(candidate):
                entry = candidate
                break
        if entry is None:
            continue  # helper module — nothing to register

        try:
            out = _call_entry(entry)
            if inspect.isawaitable(out):
                await out
            loaded.add(mod_name)
            print(f"✅ Hot-loaded bot plugin: {path.stem}")
        except Exception as e:
            print(f"❌ Hot-load failed for {mod_name}: {e}")


def _discovered_categories():
    """Group the bot's registered handlers by plugin module.

    Slash commands (NewMessage) become the command list; literal callback
    data (CallbackQuery buttons) become the button-action list, so even
    plugins with no slash commands still show up in the menu.
    """
    found = {}
    if bot_instance is None:
        return found
    try:
        handlers = bot_instance.list_event_handlers()
    except Exception:
        return found

    for callback, builder in handlers:
        module = getattr(callback, "__module__", "") or ""
        if not module.startswith("bot_plugins."):
            continue
        stem = module.split(".")[-1]
        if stem in _SKIP_DISCOVERY:
            continue

        entry = found.setdefault(stem, {"commands": [], "buttons": []})

        desc = ""
        doc = getattr(callback, "__doc__", None)
        if doc:
            desc = doc.strip().splitlines()[0]

        pat = _regex_of(builder, type(builder).__name__)
        if not pat:
            continue

        if type(builder).__name__ == "NewMessage":
            for cmd in _extract_cmds(pat):
                if cmd not in [c for c, _ in entry["commands"]] and len(entry["commands"]) < 40:
                    entry["commands"].append((cmd, desc))
        else:
            if (_LITERAL_BTN.match(pat) and len(pat) <= 32
                    and pat not in entry["buttons"]):
                entry["buttons"].append(pat)

    categories = {}
    for stem, entry in found.items():
        icon, title = _DISCOVERED_META.get(stem, ("🧩", stem.replace("_", " ").title()))
        categories[stem] = {
            "icon": icon,
            "title": title,
            "commands": sorted(entry["commands"], key=lambda c: c[0]),
            "buttons": sorted(entry["buttons"]),
        }
    return categories


def get_help_categories():
    """Core manual categories first, then every auto-discovered plugin."""
    cats = dict(_CORE_CATEGORIES)
    cats.update(_discovered_categories())
    return cats


def _help_buttons(extra):
    """One button per category + the extra (Back / Main Menu) row."""
    buttons = [
        [Button.inline(f"{info['icon']} {info['title']}", b"cat_" + key.encode())]
        for key, info in get_help_categories().items()
    ]
    buttons.append(extra)
    return buttons

def init_bot_plugin(bot, owner_id, owner_name):
    """Initialize the assistant bot plugin"""
    global bot_instance, owner_user_id, owner_display_name
    
    bot_instance = bot
    owner_user_id = owner_id
    owner_display_name = owner_name
    
    print(f"🤖 Assistant Plugin: Initialized for {owner_name} (ID: {owner_id})")
    
    # -------------------------------------------------------------------------
    # 1. START COMMAND HANDLER
    # -------------------------------------------------------------------------
    @bot.on(events.NewMessage(pattern=r"^/start"))
    async def start_handler(event):
        user_id = event.sender_id
        
        # Add user to database (Now 100% safe from dict crashes)
        users_count = add_user(user_id)
        
        # Check if sender is the owner
        if user_id == owner_user_id:
            # Owner start menu
            db = load_database()
            bot_me = await bot.get_me()
            bot_plugins_count = get_bot_plugins_count()
            
            text = (
                f"👑 <b>Welcome Master {owner_display_name}!</b>\n\n"
                f"🤖 <b>Bot:</b> @{bot_me.username}\n"
                f"📦 <b>Bot Plugins:</b> <code>{bot_plugins_count}</code>\n"
                f"👥 <b>Total Users:</b> <code>{users_count}</code>\n"
                f"🔧 <b>Assistant:</b> {'🟢 Enabled' if db['assistant_enabled'] else '🔴 Disabled'}\n\n"
                f"<i>Use the buttons below to manage your assistant bot</i>"
            )
            
            from bot_plugins.adult_mode import is_enabled as adult_mode_enabled
            adult_btn = (
                Button.inline("🔞 Disable 18+ Mode", "disable_adult_mode")
                if adult_mode_enabled()
                else Button.inline("🔞 Enable 18+ Mode", "enable_adult_mode")
            )

            buttons = [
                [Button.inline("📚 Help", b"menu_help")],
                [Button.inline("🤖 Assistant", b"menu_assistant")],
                [Button.inline("📊 Stats", b"menu_stats")],
                [Button.inline("⚙️ Settings", b"menu_settings")],
                [adult_btn],
                [Button.url("💬 Support", "https://t.me/thanosprosss")]
            ]
            
            await event.reply(text, buttons=buttons, parse_mode='html')
        else:
            # Regular user
            db = load_database()
            
            if db["assistant_enabled"]:
                text = (
                    f"👋 <b>Hello!</b>\n\n"
                    f"I am the personal assistant of <b>{owner_display_name}</b>.\n\n"
                    f"📩 Send me any message and I will deliver it to my master.\n"
                    f"💬 They can reply to you directly through me!\n\n"
                    f"<i>Please wait for a response...</i>"
                )
            else:
                text = (
                    f"👋 <b>Hello!</b>\n\n"
                    f"I am the personal assistant of <b>{owner_display_name}</b>.\n\n"
                    f"⚠️ <b>Assistant mode is currently disabled.</b>\n"
                    f"Please check back later!"
                )
            
            await event.reply(text, parse_mode='html')
    
    # -------------------------------------------------------------------------
    # 2. CALLBACK QUERY HANDLER (For inline buttons)
    # -------------------------------------------------------------------------
    @bot.on(events.CallbackQuery(pattern=r"menu_(.*)"))
    async def menu_handler(event):
        # Only owner can use these menus
        if event.sender_id != owner_user_id:
            await event.answer("⛔ This is only for the bot owner!", alert=True)
            return
        
        menu = event.data_match.group(1).decode()
        db = load_database()
        
        if menu == "help":
            await _ensure_all_loaded()   # pick up any newly added plugins
            text = (
                "📚 <b>Bot Commands Help</b>\n\n"
                "👇 <i>Select a category below to view commands</i>"
            )
            buttons = _help_buttons([Button.inline("◀️ Back", b"menu_main")])
            await event.edit(text, buttons=buttons, parse_mode='html')
        
        elif menu == "assistant":
            text = (
                "🤖 <b>Assistant Settings</b>\n\n"
                f"<b>Status:</b> {'🟢 Enabled' if db['assistant_enabled'] else '🔴 Disabled'}\n"
                f"<b>Total Messages:</b> <code>{db['stats']['total_messages']}</code>\n"
                f"<b>Total Replies:</b> <code>{db['stats']['total_replies']}</code>\n\n"
                "<b>Features:</b>\n"
                "• Forward user messages to you\n"
                "• Reply to users through the bot\n"
                "• Track multiple conversations\n"
                "• User information with each message\n\n"
                "<i>Use /assistant on|off|status to control</i>"
            )
            
            if db['assistant_enabled']:
                buttons = [
                    [Button.inline("🔴 Disable Assistant", b"assistant_toggle")],
                    [Button.inline("◀️ Back", b"menu_main")]
                ]
            else:
                buttons = [
                    [Button.inline("🟢 Enable Assistant", b"assistant_toggle")],
                    [Button.inline("◀️ Back", b"menu_main")]
                ]
            
            await event.edit(text, buttons=buttons, parse_mode='html')
        
        elif menu == "stats":
            stats = get_stats()
            text = (
                "📊 <b>Bot Statistics</b>\n\n"
                f"👥 <b>Total Users:</b> <code>{stats['users_count']}</code>\n"
                f"💬 <b>Messages Received:</b> <code>{stats['total_messages']}</code>\n"
                f"📤 <b>Replies Sent:</b> <code>{stats['total_replies']}</code>\n"
                f"🔧 <b>Assistant:</b> {'🟢 Enabled' if stats['assistant_enabled'] else '🔴 Disabled'}\n\n"
                f"<b>Uptime:</b> <i>Since bot start</i>\n"
                f"<b>Version:</b> <code>1.0.0</code>\n\n"
                f"<i>Statistics updated in real-time</i>"
            )
            buttons = [[Button.inline("◀️ Back", b"menu_main")]]
            await event.edit(text, buttons=buttons, parse_mode='html')
        
        elif menu == "settings":
            text = (
                "⚙️ <b>General Settings</b>\n\n"
                "<b>Coming Soon:</b>\n"
                "• Auto-response templates\n"
                "• Block/unblock users\n"
                "• Custom welcome messages\n"
                "• Scheduled messages\n"
                "• Analytics reports\n\n"
                "<i>More features in development...</i>"
            )
            buttons = [[Button.inline("◀️ Back", b"menu_main")]]
            await event.edit(text, buttons=buttons, parse_mode='html')
        
        elif menu == "main":
            # Back to main menu
            bot_me = await bot.get_me()
            stats = get_stats()
            bot_plugins_count = get_bot_plugins_count()
            
            text = (
                f"👑 <b>Welcome Master {owner_display_name}!</b>\n\n"
                f"🤖 <b>Bot:</b> @{bot_me.username}\n"
                f"📦 <b>Bot Plugins:</b> <code>{bot_plugins_count}</code>\n"
                f"👥 <b>Total Users:</b> <code>{stats['users_count']}</code>\n"
                f"🔧 <b>Assistant:</b> {'🟢 Enabled' if stats['assistant_enabled'] else '🔴 Disabled'}\n\n"
                f"<i>Use the buttons below to manage your assistant bot</i>"
            )
            
            from bot_plugins.adult_mode import is_enabled as adult_mode_enabled
            adult_btn = (
                Button.inline("🔞 Disable 18+ Mode", "disable_adult_mode")
                if adult_mode_enabled()
                else Button.inline("🔞 Enable 18+ Mode", "enable_adult_mode")
            )

            buttons = [
                [Button.inline("📚 Help", b"menu_help")],
                [Button.inline("🤖 Assistant", b"menu_assistant")],
                [Button.inline("📊 Stats", b"menu_stats")],
                [Button.inline("⚙️ Settings", b"menu_settings")],
                [adult_btn],
                [Button.url("💬 Support", "https://t.me/thanosprosss")]
            ]
            
            await event.edit(text, buttons=buttons, parse_mode='html')
    
    # -------------------------------------------------------------------------
    # 3. CATEGORY HELP HANDLER
    # -------------------------------------------------------------------------
    @bot.on(events.CallbackQuery(pattern=r"cat_(.*)"))
    async def category_help_handler(event):
        # Only owner can use these menus
        if event.sender_id != owner_user_id:
            await event.answer("⛔ This is only for the bot owner!", alert=True)
            return
        
        category = event.data_match.group(1).decode()

        await _ensure_all_loaded()   # pick up any newly added plugins
        categories = get_help_categories()

        if category not in categories:
            await event.answer("❌ Category not found!", alert=True)
            return

        info = categories[category]
        text = f"{info['icon']} <b>{info['title']}</b>\n\n"
        if info.get("commands"):
            for cmd, desc in info["commands"]:
                text += f"❯ <code>{cmd}</code>\n"
                if desc:
                    text += f"   <i>{desc}</i>\n"
                text += "\n"
        if info.get("buttons"):
            text += "🔘 <i>Button actions:</i>\n"
            for btn in info["buttons"]:
                text += f"❯ <code>{btn}</code>\n"
            text += "\n"
        if not info.get("commands") and not info.get("buttons"):
            text += "🤖 <i>Automatic feature — no manual commands.</i>\n\n"
        
        buttons = [
            [Button.inline("◀️ Back to Categories", b"menu_help")],
            [Button.inline("🏠 Main Menu", b"menu_main")]
        ]
        
        await event.edit(text, buttons=buttons, parse_mode='html')
    
    # -------------------------------------------------------------------------
    # 4. ASSISTANT TOGGLE HANDLER
    # -------------------------------------------------------------------------
    @bot.on(events.CallbackQuery(pattern=r"assistant_toggle"))
    async def assistant_toggle_handler(event):
        # Only owner can toggle
        if event.sender_id != owner_user_id:
            await event.answer("⛔ This is only for the bot owner!", alert=True)
            return
        
        db = load_database()
        db["assistant_enabled"] = not db["assistant_enabled"]
        save_database(db)
        
        status = "🟢 Enabled" if db["assistant_enabled"] else "🔴 Disabled"
        await event.answer(f"✅ Assistant is now {status}", alert=True)
        
        # Refresh the assistant menu
        text = (
            "🤖 <b>Assistant Settings</b>\n\n"
            f"<b>Status:</b> {status}\n"
            f"<b>Total Messages:</b> <code>{db['stats']['total_messages']}</code>\n"
            f"<b>Total Replies:</b> <code>{db['stats']['total_replies']}</code>\n\n"
            "<b>Features:</b>\n"
            "• Forward user messages to you\n"
            "• Reply to users through the bot\n"
            "• Track multiple conversations\n"
            "• User information with each message\n\n"
            "<i>Use /assistant on|off|status to control</i>"
        )
        
        if db['assistant_enabled']:
            buttons = [
                [Button.inline("🔴 Disable Assistant", b"assistant_toggle")],
                [Button.inline("◀️ Back", b"menu_main")]
            ]
        else:
            buttons = [
                [Button.inline("🟢 Enable Assistant", b"assistant_toggle")],
                [Button.inline("◀️ Back", b"menu_main")]
            ]
        
        await event.edit(text, buttons=buttons, parse_mode='html')
    
    # -------------------------------------------------------------------------
    # 4. ASSISTANT COMMAND HANDLER
    # -------------------------------------------------------------------------
    @bot.on(events.NewMessage(pattern=r"^/assistant(?:\s+(.+))?"))
    async def assistant_command_handler(event):
        # Only owner can use this command
        if event.sender_id != owner_user_id:
            await event.reply("⛔ This command is only for the bot owner!")
            return
        
        action = event.pattern_match.group(1)
        db = load_database()
        
        if not action:
            # Show status
            status = "🟢 Enabled" if db["assistant_enabled"] else "🔴 Disabled"
            await event.reply(
                f"🤖 <b>Assistant Status:</b> {status}\n\n"
                f"<b>Commands:</b>\n"
                f"• <code>/assistant on</code> - Enable assistant\n"
                f"• <code>/assistant off</code> - Disable assistant\n"
                f"• <code>/assistant status</code> - Check status",
                parse_mode='html'
            )
            return
        
        action = action.strip().lower()
        
        if action == "on":
            db["assistant_enabled"] = True
            save_database(db)
            await event.reply(
                "✅ <b>Assistant Mode Enabled!</b>\n\n"
                "Users can now send you messages through the bot.\n"
                "You will receive notifications for each message.",
                parse_mode='html'
            )
        
        elif action == "off":
            db["assistant_enabled"] = False
            save_database(db)
            await event.reply(
                "🔴 <b>Assistant Mode Disabled!</b>\n\n"
                "Users will see a message that assistant is currently disabled.",
                parse_mode='html'
            )
        
        elif action == "status":
            status = "🟢 Enabled" if db["assistant_enabled"] else "🔴 Disabled"
            await event.reply(
                f"🤖 <b>Assistant Status:</b> {status}\n\n"
                f"📊 <b>Statistics:</b>\n"
                f"• Messages: <code>{db['stats']['total_messages']}</code>\n"
                f"• Replies: <code>{db['stats']['total_replies']}</code>\n"
                f"• Users: <code>{len(db['users'])}</code>",
                parse_mode='html'
            )
        
        else:
            await event.reply(
                "❌ Invalid action!\n\n"
                "Use: <code>/assistant on|off|status</code>",
                parse_mode='html'
            )
    
    # -------------------------------------------------------------------------
    # 5. USER MESSAGE HANDLER (Forward to owner)
    # -------------------------------------------------------------------------
    @bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def user_message_handler(event):
        # Ignore bot commands
        if event.text and event.text.startswith('/'):
            return
        
        # Ignore owner's messages
        if event.sender_id == owner_user_id:
            return
        
        # Check if assistant is enabled
        db = load_database()
        if not db["assistant_enabled"]:
            return
        
        # Get sender info
        try:
            sender = await event.get_sender()
            sender_name = sender.first_name or "Unknown"
            sender_username = f"@{sender.username}" if sender.username else "No username"
            sender_id = sender.id
            
            # Forward message to owner
            forward_text = (
                f"📩 <b>New Message from User</b>\n\n"
                f"👤 <b>Name:</b> {html.escape(sender_name)}\n"
                f"🆔 <b>User ID:</b> <code>{sender_id}</code>\n"
                f"📝 <b>Username:</b> {html.escape(sender_username)}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n\n"
            )
            
            # Send the forward to owner
            message_text = html.escape(event.text) if event.text else '[Media/Sticker/Other]'
            forwarded = await bot.send_message(
                owner_user_id,
                forward_text + f"<b>Message:</b> {message_text}",
                parse_mode='html'
            )
            
            # If there's media, forward it too
            if event.photo or event.video or event.document or event.sticker:
                await event.forward_to(owner_user_id)
            
            # Store mapping for replies
            db["user_message_map"][str(forwarded.id)] = sender_id
            db["stats"]["total_messages"] += 1
            save_database(db)
            
            # Confirm to user
            await event.reply(
                "✅ <b>Message sent!</b>\n\n"
                f"Your message has been delivered to <b>{owner_display_name}</b>.\n"
                f"Please wait for a response...",
                parse_mode='html'
            )
            
        except Exception as e:
            print(f"Error forwarding user message: {e}")
    
    # -------------------------------------------------------------------------
    # 6. OWNER REPLY HANDLER (Reply to users)
    # -------------------------------------------------------------------------
    @bot.on(events.NewMessage(from_users=owner_user_id))
    async def owner_reply_handler(event):
        # Check if this is a reply
        if not event.is_reply:
            return
        
        try:
            # Get the message being replied to
            replied_msg = await event.get_reply_message()
            
            # Check if this message is in our mapping
            db = load_database()
            replied_msg_id = str(replied_msg.id)
            
            if replied_msg_id in db["user_message_map"]:
                # Get the original user ID
                user_id = db["user_message_map"][replied_msg_id]
                
                # Send owner's reply to the user
                reply_message = html.escape(event.text) if event.text else '[Media/Sticker/Other]'
                reply_text = (
                    f"💬 <b>Reply from {html.escape(owner_display_name)}:</b>\n\n"
                    f"{reply_message}"
                )
                
                await bot.send_message(user_id, reply_text, parse_mode='html')
                
                # If there's media in the reply, forward it too
                if event.photo or event.video or event.document or event.sticker:
                    await event.forward_to(user_id)
                
                # Update stats
                db["stats"]["total_replies"] += 1
                save_database(db)
                
                # Confirm to owner
                await event.reply("✅ <b>Reply sent to user!</b>", parse_mode='html')
                
                # Note: We keep the message mapping for potential follow-up conversations
                # A cleanup mechanism can be added later based on message age
        
        except Exception as e:
            print(f"Error handling owner reply: {e}")
    
    # -------------------------------------------------------------------------
    # 7. HELP COMMAND
    # -------------------------------------------------------------------------
    @bot.on(events.NewMessage(pattern=r"^/help"))
    async def help_command_handler(event):
        if event.sender_id == owner_user_id:
            await _ensure_all_loaded()   # pick up any newly added plugins
            text = (
                "📚 <b>Bot Commands Help</b>\n\n"
                "👇 <i>Select a category below to view commands</i>"
            )
            buttons = _help_buttons([Button.inline("🏠 Main Menu", b"menu_main")])
            await event.reply(text, buttons=buttons, parse_mode='html')
        else:
            text = (
                "📚 <b>Help</b>\n\n"
                "This is a personal assistant bot.\n"
                "Simply send your message and it will be forwarded to the owner.\n\n"
                "Use /start to begin."
            )
            await event.reply(text, parse_mode='html')
    
    print("✅ Assistant Plugin: All handlers registered successfully")
