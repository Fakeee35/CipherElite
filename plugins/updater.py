# =============================================================================
#  CipherElite Userbot Plugin
#
#  Plugin Name:    updater
#  Version:        2.0.0
#  Author:         CipherElite Dev (@rishabhops)
#  Repository:     https://github.com/rishabhops/CipherElite
#
#  License:        MIT
#
#  IMPORTANT:
#    • If you copy, fork, or include this plugin in your own bot,
#      you MUST keep this header intact.
#    • You MUST give proper credit to the CipherElite Userbot author:
#      – GitHub:    https://github.com/rishabhops/CipherElite
#      – Telegram:  @thanosceo
# =============================================================================
import os, sys, json, time, shutil, asyncio, aiohttp
from pathlib import Path
from telethon import events
from utils.utils import CipherElite
from utils.decorators import rishabh
from config.config import Config

VERSION = "2.0.0"
CATEGORY = "developer"

# ──────────────────────────────────────────────────────────────
GITHUB_OWNER  = "rishabhops"
GITHUB_REPO   = "CipherElite"
GITHUB_BRANCH = Config.BRANCH
API_BASE      = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
RAW_BASE      = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}"
# ──────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent
DB_DIR       = PROJECT_ROOT / "DB"
DB_DIR.mkdir(exist_ok=True)
DB_FILE      = DB_DIR / "updater_db.json"
BACKUP_DIR   = DB_DIR / "update_backups"

# ANY files here will be *skipped* by the download-mode updater
SKIP_FILES = {
  "vars.py",            # your credentials + IDs
  "config/config.py",   # if you also customize your Config
}

def load_db() -> dict:
    if DB_FILE.exists():
        return json.loads(DB_FILE.read_text())
    return {}

def save_db(d: dict):
    DB_DIR.mkdir(parents=True, exist_ok=True)   # survive hard resets that wipe untracked dirs
    DB_FILE.write_text(json.dumps(d, indent=2))

async def get_json(url: str):
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as resp:
            resp.raise_for_status()
            return await resp.json()

async def download_file(relpath: str) -> bytes:
    url = f"{RAW_BASE}/{relpath}"
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()

async def fetch_full_tree():
    tree_url = f"{API_BASE}/git/trees/{GITHUB_BRANCH}?recursive=1"
    js = await get_json(tree_url)
    return [e for e in js.get("tree", []) if e["type"] == "blob"]

async def check_and_install_reqs(msg):
    """Checks requirements. Returns True if new packages were installed, False otherwise."""
    req_path = PROJECT_ROOT / "requirements.txt"
    if not req_path.exists():
        return False

    await msg.edit("📦 **Checking dependencies in requirements.txt...**")

    pip_process = await asyncio.create_subprocess_shell(
        f"{sys.executable} -m pip install -r {req_path}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await pip_process.communicate()
    output = stdout.decode().strip()

    # If pip actually had to download and install something
    if "Successfully installed" in output or "Downloading" in output:
        await msg.edit("⚙️ **New dependencies installed successfully!**")
        await asyncio.sleep(1.5)
        return True
    else:
        await msg.edit("✅ **All dependencies are already satisfied!**")
        await asyncio.sleep(1.0)
        return False

# ══════════════════════════════════════════════════════════════
#  Git helpers (self-healing core)
# ══════════════════════════════════════════════════════════════

def is_git_repo() -> bool:
    """True when the bot is installed inside a git clone."""
    return (PROJECT_ROOT / ".git").exists() and shutil.which("git") is not None

async def run_git(*args):
    """Run a git command inside the project root -> (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )

def _parse_blocked_files(output: str, header: str):
    """Extract the tab-indented file list that git prints under `header`."""
    names, collecting = [], False
    for line in output.splitlines():
        stripped = line.strip()
        if header in stripped:
            collecting = True
            continue
        if collecting:
            if line.startswith("\t") and stripped:
                names.append(stripped)
            else:
                collecting = False
    return names

def _backup_and_remove(rel: str, stamp: str) -> bool:
    """Move a conflicting path out of the working tree into DB/update_backups/."""
    src = PROJECT_ROOT / rel
    try:
        if src.is_dir():
            shutil.rmtree(src)
        elif src.exists():
            dest = BACKUP_DIR / stamp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            shutil.move(str(src), str(dest))
        return True
    except Exception:
        return False

def _restart():
    """Restart the userbot process (separate function so it can be intercepted)."""
    os.execv(sys.executable, [sys.executable] + sys.argv)

async def self_heal_pull(msg):
    """
    `git pull --ff-only` with automatic repair. Returns (ok, detail).

    Repairs, in order of the error git reports:
      * untracked files blocking the merge  -> back them up + remove, retry
      * tracked files with local edits      -> git stash, retry
      * diverged branch / not fast-forward  -> reset --hard to official, retry
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for _ in range(6):
        rc, out, err = await run_git("pull", "--ff-only", "origin", GITHUB_BRANCH)
        if rc == 0:
            return True, (out + err).strip()

        combined = out + "\n" + err

        # 1) untracked files would be overwritten (the classic manual-copy bug)
        untracked = _parse_blocked_files(
            combined, "The following untracked working tree files would be overwritten by"
        )
        if untracked:
            moved = [rel for rel in untracked if _backup_and_remove(rel, stamp)]
            await msg.edit(
                "⚠️ Update was blocked by conflicting local file(s).\n"
                f"Moved them to DB/update_backups/{stamp}/ and retrying:\n"
                + "\n".join("• " + m for m in moved)
            )
            continue

        # 2) tracked files with uncommitted local edits
        local = _parse_blocked_files(
            combined, "Your local changes to the following files would be overwritten by"
        )
        if local:
            await run_git("stash", "push", "-m", "updater-autostash-" + stamp)
            await msg.edit(
                "⚠️ Local edits blocked the update — stashed them automatically, retrying…"
            )
            continue

        # 3) branch diverged / cannot fast-forward
        low = combined.lower()
        if "not possible to fast-forward" in low or "have diverged" in low or "divergent" in low:
            await run_git("reset", "--hard", f"origin/{GITHUB_BRANCH}")
            await msg.edit(
                "⚠️ Local branch diverged from the official repo — "
                "reset to the official version, retrying…"
            )
            continue

        # unknown failure -> report it
        return False, combined.strip()

    return False, "Update kept failing after several self-heal attempts."

async def _record_sha():
    """Store the current HEAD sha so the download-mode stays in sync too."""
    rc, sha, _ = await run_git("rev-parse", "HEAD")
    if rc == 0 and sha.strip():
        db = load_db()
        db["last_sha"] = sha.strip()
        save_db(db)

# ══════════════════════════════════════════════════════════════
#  Commands
# ══════════════════════════════════════════════════════════════

@CipherElite.on(events.NewMessage(pattern=r"\.checkupdate$", outgoing=True))
@rishabh()
async def check_update(event):
    await event.reply("🔍 Checking for updates…")

    # ─── git mode ───────────────────────────────────────────────
    if is_git_repo():
        rc, out, err = await run_git("fetch", "origin", GITHUB_BRANCH)
        if rc != 0:
            return await event.reply(f"❌ git fetch failed:\n{(out + err)[-800:]}")
        rc, count_out, _ = await run_git(
            "rev-list", "--count", f"HEAD..origin/{GITHUB_BRANCH}"
        )
        behind = int((count_out.strip() or "0"))
        if behind == 0:
            return await event.reply("✅ Already up-to-date.")
        rc, namestatus, _ = await run_git(
            "diff", "--name-status", f"HEAD..origin/{GITHUB_BRANCH}"
        )
        lines = [ln.strip() for ln in namestatus.splitlines() if ln.strip()][:40]
        return await event.reply(
            f"**{behind} commit(s) behind — {len(lines)} file(s) will change:**\n\n"
            + "\n".join("• " + l for l in lines)
            + "\n\nSend .update to install."
        )

    # ─── download mode (no git clone on this device) ───────────
    db       = load_db()
    last_sha = db.get("last_sha", "")
    try:
        branch = await get_json(f"{API_BASE}/branches/{GITHUB_BRANCH}")
        remote_sha = branch["commit"]["sha"]
    except Exception as e:
        return await event.reply(f"❌ Failed to fetch updates: {e}")

    if not last_sha:
        # initial import listing
        tree = await fetch_full_tree()
        text = "**Initial import – files you will get:**\n\n"
        for e in tree:
            if e["path"] in SKIP_FILES:
                continue
            text += f"• ADDED   {e['path']}\n"
        return await event.reply(text)

    comp = await get_json(f"{API_BASE}/compare/{last_sha}...{remote_sha}")
    files = comp.get("files", [])
    if not files:
        return await event.reply("✅ Already up-to-date.")

    text = "**Updates available:**\n\n"
    for f in files:
        name = f["filename"]
        if name in SKIP_FILES:
            continue
        text += f"• {f['status'].upper():8} {name} (+{f['additions']}/–{f['deletions']})\n"
    await event.reply(text)

@CipherElite.on(events.NewMessage(pattern=r"\.update$", outgoing=True))
@rishabh()
async def do_update(event):
    db       = load_db()
    last_sha = db.get("last_sha", "")
    msg      = await event.reply("🔄 Fetching updates…")

    # ─── git mode with self-healing ─────────────────────────────
    if is_git_repo():
        rc, old_sha, _ = await run_git("rev-parse", "HEAD")
        old_sha = old_sha.strip()

        rc, out, err = await run_git("fetch", "origin", GITHUB_BRANCH)
        if rc != 0:
            return await msg.edit(f"❌ git fetch failed:\n{(out + err)[-800:]}")

        rc, count_out, _ = await run_git(
            "rev-list", "--count", f"HEAD..origin/{GITHUB_BRANCH}"
        )
        behind = int((count_out.strip() or "0"))

        if behind == 0:
            installed_new = await check_and_install_reqs(msg)
            if installed_new:
                await msg.edit(
                    "✅ Missing dependencies were found and installed! Restarting to load them…"
                )
                await asyncio.sleep(1.0)
                _restart()
            else:
                return await msg.edit("✅ Code and dependencies are already up-to-date!")

        await msg.edit(f"📥 {behind} new commit(s) — updating…")
        ok, detail = await self_heal_pull(msg)
        if not ok:
            return await msg.edit(
                f"❌ Update failed:\n{detail[-800:]}\n\n"
                "Run .update again — the updater will repeat its self-healing steps."
            )

        rc, namestatus, _ = await run_git("diff", "--name-status", f"{old_sha}..HEAD")
        changed = [ln.strip() for ln in namestatus.splitlines() if ln.strip()][:30]
        await _record_sha()

        await check_and_install_reqs(msg)

        text = f"✅ Update successful — {len(changed)} file(s) changed:\n"
        text += "\n".join("• " + c for c in changed) or "• (no tracked changes)"
        text += "\n\nVariables untouched. Restarting…"
        await msg.edit(text)
        await asyncio.sleep(1.0)
        _restart()
        return

    # ─── download mode (original behaviour, no git on device) ──
    try:
        branch = await get_json(f"{API_BASE}/branches/{GITHUB_BRANCH}")
        remote_sha = branch["commit"]["sha"]
    except Exception as e:
        return await msg.edit(f"❌ Failed to fetch updates: {e}")

    # ─── initial import ─────────────────────────────────────────────
    if not last_sha:
        tree = await fetch_full_tree()
        await msg.edit(f"📦 Downloading {len(tree)} files (initial import)…")
        for e in tree:
            rel = e["path"]
            if rel in SKIP_FILES:
                continue
            local = PROJECT_ROOT / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            content = await download_file(rel)
            local.write_bytes(content)
        db["last_sha"] = remote_sha
        save_db(db)

        await check_and_install_reqs(msg)

        await msg.edit(
          "✅ Initial import & dependencies complete! Variables preserved.\n"
          "Now try .ping and .alive\n"
          "Restarting…"
        )
        await asyncio.sleep(1.0)
        _restart()
        return

    # ─── delta update ───────────────────────────────────────────────
    if remote_sha == last_sha:
        # Code is up to date, but we MUST check requirements anyway!
        installed_new = await check_and_install_reqs(msg)

        if installed_new:
            # If pip installed something, we have to restart to load it
            await msg.edit("✅ Missing dependencies were found and installed! Restarting to load them…")
            await asyncio.sleep(1.0)
            _restart()
        else:
            # If nothing was installed, no restart needed
            return await msg.edit("✅ Code and dependencies are already up-to-date!")

    comp = await get_json(f"{API_BASE}/compare/{last_sha}...{remote_sha}")
    files = comp.get("files", [])
    if not files:
        db["last_sha"] = remote_sha
        save_db(db)
        installed_new = await check_and_install_reqs(msg)
        if installed_new:
            await msg.edit("✅ Dependencies updated! Restarting…")
            await asyncio.sleep(1.0)
            _restart()
        else:
            return await msg.edit("ℹ️ No code changes detected – marker updated.")

    await msg.edit(f"📄 {len(files)} files changed – downloading…")
    for f in files:
        rel    = f["filename"]
        status = f["status"]
        if rel in SKIP_FILES:
            continue
        local = PROJECT_ROOT / rel
        if status in ("added", "modified", "renamed"):
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(await download_file(rel))
        elif status == "removed" and local.exists():
            local.unlink()

    db["last_sha"] = remote_sha
    save_db(db)

    # Check requirements after downloading new files
    await check_and_install_reqs(msg)

    # final success edit + preserve vars + tip
    await msg.edit(
      "✅ Update successful. Variables untouched.\n"
      "Check .ping and .alive\n"
      "Restarting now…"
    )
    await asyncio.sleep(1.0)
    _restart()


def init(client):
    # no-op so your loader doesn't complain
    pass
