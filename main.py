#!/usr/bin/env python3
"""
=============================================================================
 GitHub .env File Discovery Bot – Ethical Use Only
=============================================================================

 A fully asynchronous Telegram bot that searches GitHub for publicly committed
 `.env` files using the GitHub REST API and delivers results directly to your
 chat.

 Features:
  - Advanced filtering (org, user, language)
  - Configurable max results, content inclusion, secret redaction
  - Multiple output formats (text, JSON, CSV)
  - Full GitHub pagination & rate-limit awareness
  - Per‑chat settings stored in memory (never persisted)
  - Non‑blocking scans with progress updates and cancellation

 Prerequisites:
  - Python 3.9+
  - A Telegram Bot Token from @BotFather
  - A GitHub Personal Access Token (classic) with no special scopes
    (public repos only)

 Setup:
  1. Create a virtual environment and activate it
  2. pip install -r requirements.txt
  3. export BOT_TOKEN="your_telegram_bot_token"
  4. python bot.py

 The bot token MUST be set as the environment variable BOT_TOKEN.
 Never hard-code credentials.

 Ethics & Warnings:
  - This tool searches PUBLIC GitHub repositories.
  - It MUST ONLY be used on repositories you own or have explicit
    permission to scan.
  - Any discovered secrets must be reported responsibly and never
    misused.
  - Unauthorised scanning of third‑party repositories may violate
    GitHub's Terms of Service.

 Author: (your name / project)
 License: MIT
=============================================================================
"""

import asyncio
import base64
import csv
import io
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# --------------------------------------------------------------------------- #
#  Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  Global state (per‑chat) – thread‑safe in asyncio
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: Dict[str, Any] = {
    "token": None,
    "org": None,
    "user": None,
    "lang": None,
    "max_results": 1000,
    "include_content": False,
    "redact": False,
    "output_format": "text",
}

user_configs: Dict[int, Dict[str, Any]] = {}          # chat_id → settings
scan_tasks: Dict[int, asyncio.Task] = {}              # chat_id → running scan task
cancel_events: Dict[int, asyncio.Event] = {}          # chat_id → cancellation event

# --------------------------------------------------------------------------- #
#  Helpers – Token masking, redaction, message splitting
# --------------------------------------------------------------------------- #
def mask_token(token: str) -> str:
    """Return a safely masked version of a GitHub token."""
    if not token or len(token) < 8:
        return "****"
    return f"{token[:4]}{'*' * (len(token) - 8)}{token[-4:]}"


def redact_content(text: str) -> str:
    """
    Simple redaction of potential secrets.
    Replaces high‑entropy strings (≥20 chars, base64‑like) with [REDACTED].
    """
    # This is a naïve heuristic – tune for your own needs.
    pattern = re.compile(r"[A-Za-z0-9+/=_-]{20,}")
    return pattern.sub("[REDACTED]", text)


async def send_long_message(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    parse_mode: str = ParseMode.HTML,
) -> None:
    """Split text into chunks of 4096 bytes and send them sequentially."""
    max_len = 4096
    for start in range(0, len(text), max_len):
        await context.bot.send_message(
            chat_id=chat_id,
            text=text[start:start + max_len],
            parse_mode=parse_mode,
        )


def build_query(config: Dict[str, Any]) -> str:
    """Construct the GitHub search query string."""
    qualifiers = ["filename:.env"]
    if config.get("org"):
        qualifiers.append(f"org:{config['org']}")
    if config.get("user"):
        qualifiers.append(f"user:{config['user']}")
    if config.get("lang"):
        qualifiers.append(f"language:{config['lang']}")
    return "+".join(qualifiers)


# --------------------------------------------------------------------------- #
#  GitHub API interaction
# --------------------------------------------------------------------------- #
class GitHubSearcher:
    """Async wrapper around GitHub REST API for code search and file contents."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "Telegram-GitHubEnvBot/1.0",
            },
            timeout=30.0,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(
        self, method: str, path: str, params: Optional[Dict] = None
    ) -> httpx.Response:
        """Make a request with rate‑limit handling."""
        while True:
            resp = await self.client.request(method, path, params=params)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning("Rate limited, waiting %s seconds...", retry_after)
                await asyncio.sleep(retry_after)
                continue
            # Handle other transient errors (5xx) with a small delay
            if resp.status_code >= 500:
                await asyncio.sleep(2)
                continue
            return resp

    async def search_code(
        self,
        query: str,
        max_results: int = 1000,
        progress_callback=None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> List[Dict[str, Any]]:
        """
        Paginate through /search/code.
        progress_callback is an async function(page, total_fetched).
        """
        per_page = 100
        page = 1
        all_items: List[Dict[str, Any]] = []
        while len(all_items) < max_results:
            if cancel_event and cancel_event.is_set():
                logger.info("Scan cancelled by user.")
                break

            resp = await self._request(
                "GET",
                "/search/code",
                params={"q": query, "per_page": per_page, "page": page},
            )
            if resp.status_code != 200:
                logger.error("Search failed: %s %s", resp.status_code, resp.text)
                break

            data = resp.json()
            items = data.get("items", [])
            if not items:
                break

            # Append only as many as needed to reach max_results
            needed = max_results - len(all_items)
            all_items.extend(items[:needed])

            if progress_callback:
                await progress_callback(page, len(all_items))

            # If the page is not full, we're done
            if len(items) < per_page:
                break
            page += 1

        return all_items

    async def fetch_file_content(
        self, full_name: str, path: str
    ) -> Optional[str]:
        """Fetch and decode the content of a single file."""
        try:
            resp = await self._request(
                "GET", f"/repos/{full_name}/contents/{path}"
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("encoding") == "base64" and data.get("content"):
                    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            # 404 → file deleted / not accessible; ignore
        except Exception as e:
            logger.warning("Could not fetch %s/%s: %s", full_name, path, e)
        return None


# --------------------------------------------------------------------------- #
#  Scan orchestrator (runs in background)
# --------------------------------------------------------------------------- #
async def run_scan(
    chat_id: int,
    config: Dict[str, Any],
    context: ContextTypes.DEFAULT_TYPE,
    cancel_event: asyncio.Event,
) -> None:
    """The main scan job. Sends progress, handles content fetching, output."""
    token = config["token"]
    if not token:
        await context.bot.send_message(chat_id, "❌ GitHub token not set. Use /settoken.")
        return

    searcher = GitHubSearcher(token)
    try:
        await context.bot.send_chat_action(chat_id, action="typing")

        # Build query
        query = build_query(config)
        max_res = config["max_results"]

        # Progress callback for pagination
        async def progress(page: int, total: int) -> None:
            await context.bot.send_message(
                chat_id,
                f"📄 Fetched page {page}, total results so far: {total}",
            )

        await context.bot.send_message(
            chat_id,
            f"🔎 Searching for `{query}` (max {max_res} results)…",
            parse_mode=ParseMode.MARKDOWN,
        )

        # Run search
        items = await searcher.search_code(
            query=query,
            max_results=max_res,
            progress_callback=progress,
            cancel_event=cancel_event,
        )

        if cancel_event.is_set():
            await context.bot.send_message(chat_id, "🛑 Scan cancelled.")
            return

        if not items:
            await context.bot.send_message(chat_id, "✅ No .env files found.")
            return

        # Content fetching if enabled
        if config["include_content"]:
            await context.bot.send_message(chat_id, "⬇ Fetching file contents…")
            semaphore = asyncio.Semaphore(3)  # limit concurrent fetches

            async def fetch_one(idx: int, itm: dict) -> None:
                async with semaphore:
                    full_name = itm["repository"]["full_name"]
                    path = itm["path"]
                    content = await searcher.fetch_file_content(full_name, path)
                    if content:
                        itm["content"] = content
                    # Progress update every 10 files
                    if idx % 10 == 0:
                        await context.bot.send_message(
                            chat_id,
                            f"⬇ Fetched contents {idx}/{len(items)}",
                        )

            tasks = [asyncio.create_task(fetch_one(i, item)) for i, item in enumerate(items, start=1)]
            await asyncio.gather(*tasks)

        # Apply redaction if both content and redact are enabled
        if config["include_content"] and config["redact"]:
            for itm in items:
                if "content" in itm:
                    itm["content"] = redact_content(itm["content"])

        # Generate output
        output_format = config["output_format"]
        if output_format == "text":
            await send_text_results(chat_id, items, config, context)
        elif output_format == "json":
            await send_file_results(chat_id, items, config, context, "json")
        elif output_format == "csv":
            await send_file_results(chat_id, items, config, context, "csv")

        await context.bot.send_message(
            chat_id,
            f"✅ Scan complete. Found **{len(items)}** .env files.",
            parse_mode=ParseMode.MARKDOWN,
        )
    except asyncio.CancelledError:
        await context.bot.send_message(chat_id, "🛑 Scan cancelled.")
    except Exception as e:
        logger.exception("Scan error for chat %s", chat_id)
        await context.bot.send_message(chat_id, f"❌ Error: {e}")
    finally:
        await searcher.close()


async def send_text_results(
    chat_id: int,
    items: List[Dict],
    config: Dict[str, Any],
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Format results as text and send, splitting if necessary."""
    lines: List[str] = []
    for itm in items:
        repo_full = itm["repository"]["full_name"]
        file_path = itm["path"]
        html_url = itm["html_url"]
        entry = (
            f"📁 <b>{repo_full}</b>\n"
            f"📄 <code>{file_path}</code>\n"
            f"🔗 <a href='{html_url}'>View file</a>"
        )
        if config["include_content"] and "content" in itm:
            snippet = itm["content"][:500]  # first 500 chars
            entry += f"\n📝 <pre>{snippet}</pre>"
        lines.append(entry)

    # Join with double newline
    text = "\n\n".join(lines)
    await send_long_message(context, chat_id, text, parse_mode=ParseMode.HTML)


async def send_file_results(
    chat_id: int,
    items: List[Dict],
    config: Dict[str, Any],
    context: ContextTypes.DEFAULT_TYPE,
    fmt: str,
) -> None:
    """Generate JSON or CSV and send as a document."""
    rows = []
    for itm in items:
        row = {
            "repository": itm["repository"]["full_name"],
            "path": itm["path"],
            "html_url": itm["html_url"],
            "score": itm.get("score", ""),
        }
        if config["include_content"] and "content" in itm:
            row["content"] = itm["content"]
        rows.append(row)

    if fmt == "json":
        import json
        data = json.dumps(rows, indent=2, ensure_ascii=False).encode("utf-8")
        filename = "env_results.json"
    else:  # csv
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        data = output.getvalue().encode("utf-8")
        filename = "env_results.csv"

    await context.bot.send_document(
        chat_id=chat_id,
        document=io.BytesIO(data),
        filename=filename,
    )


# --------------------------------------------------------------------------- #
#  Command handlers
# --------------------------------------------------------------------------- #
def get_user_config(chat_id: int) -> Dict[str, Any]:
    if chat_id not in user_configs:
        user_configs[chat_id] = DEFAULT_CONFIG.copy()
    return user_configs[chat_id]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome = (
        "👋 <b>GitHub .env File Discovery Bot</b>\n\n"
        "⚠️ <b>ETHICAL USE ONLY</b>\n"
        "This tool searches publicly committed .env files on GitHub.\n"
        "Use it <u>only</u> on repositories you own or have explicit "
        "permission to scan. Discovered secrets must be reported responsibly.\n\n"
        "Set your GitHub token with /settoken, then use /scan.\n"
        "Type /help for all commands."
    )
    await update.message.reply_html(welcome)
    # Initialise user config silently
    get_user_config(update.effective_chat.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>Available Commands</b>\n\n"
        "/start – Welcome & ethical warning\n"
        "/help  – This message\n"
        "/settoken &lt;token&gt; – Set GitHub token\n"
        "/scan [org:X] [user:X] [lang:X] [max:N] [content] [redact] – Run scan\n"
        "/setorg &lt;org&gt; – Restrict to organisation\n"
        "/setuser &lt;user&gt; – Restrict to user\n"
        "/setlang &lt;lang&gt; – Filter by language\n"
        "/setmax &lt;1-1000&gt; – Max results\n"
        "/setcontent on|off – Toggle file content fetch\n"
        "/setredact on|off – Toggle secret redaction\n"
        "/setoutput text|json|csv – Output format\n"
        "/status – Show current settings\n"
        "/cancel – Cancel running scan\n\n"
        "<i>Examples:</i>\n"
        "/setorg mycompany\n"
        "/scan org:mycompany lang:python max:50 content\n"
    )
    await update.message.reply_html(text)


async def settoken(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)
    if not context.args:
        await update.message.reply_text(
            "Please provide a token: /settoken &lt;your_github_token&gt;"
        )
        return
    token = context.args[0].strip()
    config["token"] = token
    await update.message.reply_text(
        f"✅ GitHub token set: <code>{mask_token(token)}</code>", parse_mode=ParseMode.HTML
    )


async def setorg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)
    org = context.args[0].strip() if context.args else None
    config["org"] = org
    await update.message.reply_text(
        f"🔧 Organisation filter set to: <b>{org or 'none'}</b>", parse_mode=ParseMode.HTML
    )


async def setuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)
    user = context.args[0].strip() if context.args else None
    config["user"] = user
    await update.message.reply_text(
        f"🔧 User filter set to: <b>{user or 'none'}</b>", parse_mode=ParseMode.HTML
    )


async def setlang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)
    lang = context.args[0].strip() if context.args else None
    config["lang"] = lang
    await update.message.reply_text(
        f"🔧 Language filter set to: <b>{lang or 'none'}</b>", parse_mode=ParseMode.HTML
    )


async def setmax(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)
    if not context.args:
        await update.message.reply_text("Usage: /setmax &lt;1-1000&gt;")
        return
    try:
        value = int(context.args[0])
        if not 1 <= value <= 1000:
            raise ValueError
        config["max_results"] = value
        await update.message.reply_text(f"🔧 Max results set to: {value}")
    except ValueError:
        await update.message.reply_text("❌ Invalid number. Must be between 1 and 1000.")


async def setcontent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /setcontent on|off")
        return
    config["include_content"] = context.args[0].lower() == "on"
    state = "ON" if config["include_content"] else "OFF"
    await update.message.reply_text(f"🔧 Content fetching: <b>{state}</b>", parse_mode=ParseMode.HTML)


async def setredact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /setredact on|off")
        return
    config["redact"] = context.args[0].lower() == "on"
    state = "ON" if config["redact"] else "OFF"
    await update.message.reply_text(f"🔧 Redaction: <b>{state}</b>", parse_mode=ParseMode.HTML)


async def setoutput(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)
    if not context.args or context.args[0].lower() not in ("text", "json", "csv"):
        await update.message.reply_text("Usage: /setoutput text|json|csv")
        return
    config["output_format"] = context.args[0].lower()
    await update.message.reply_text(
        f"🔧 Output format set to: <b>{config['output_format']}</b>", parse_mode=ParseMode.HTML
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)
    token_display = mask_token(config["token"]) if config["token"] else "not set"
    text = (
        f"<b>Current Configuration</b>\n"
        f"🔑 Token: <code>{token_display}</code>\n"
        f"🏢 Org: {config['org'] or 'none'}\n"
        f"👤 User: {config['user'] or 'none'}\n"
        f"🌐 Language: {config['lang'] or 'none'}\n"
        f"📊 Max results: {config['max_results']}\n"
        f"📝 Content fetch: {'ON' if config['include_content'] else 'OFF'}\n"
        f"🛡 Redaction: {'ON' if config['redact'] else 'OFF'}\n"
        f"📎 Output: {config['output_format']}"
    )
    await update.message.reply_html(text)


async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    config = get_user_config(chat_id)

    # Check if a scan is already running
    if chat_id in scan_tasks and not scan_tasks[chat_id].done():
        await update.message.reply_text("⏳ A scan is already running. Use /cancel to stop it.")
        return

    # Parse optional overrides from arguments
    overrides = parse_scan_args(context.args)
    # Create a copy of the config with overrides applied
    scan_config = config.copy()
    for k, v in overrides.items():
        scan_config[k] = v

    if not scan_config["token"]:
        await update.message.reply_text("❌ Please set a GitHub token first with /settoken.")
        return

    # Create cancellation event
    cancel_event = asyncio.Event()
    cancel_events[chat_id] = cancel_event

    # Launch background task
    task = asyncio.create_task(
        run_scan(chat_id, scan_config, context, cancel_event)
    )
    scan_tasks[chat_id] = task

    # Clean up after task finishes
    task.add_done_callback(lambda t: cleanup_scan(chat_id))

    await update.message.reply_text("🚀 Scan started. You will receive progress updates.")


def parse_scan_args(args: List[str]) -> Dict[str, Any]:
    """Parse /scan arguments like org:X, user:X, lang:X, max:N, content, redact."""
    overrides: Dict[str, Any] = {}
    for arg in args:
        arg = arg.strip()
        if not arg:
            continue
        if ":" in arg:
            key, _, value = arg.partition(":")
            key = key.lower()
            value = value.strip()
            if key == "org":
                overrides["org"] = value
            elif key == "user":
                overrides["user"] = value
            elif key == "lang":
                overrides["lang"] = value
            elif key == "max":
                try:
                    num = int(value)
                    if 1 <= num <= 1000:
                        overrides["max_results"] = num
                except ValueError:
                    pass  # ignore invalid numbers
        else:
            flag = arg.lower()
            if flag == "content":
                overrides["include_content"] = True
            elif flag == "redact":
                overrides["redact"] = True
    return overrides


def cleanup_scan(chat_id: int) -> None:
    """Remove task and event references after scan finishes."""
    scan_tasks.pop(chat_id, None)
    cancel_events.pop(chat_id, None)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    task = scan_tasks.get(chat_id)
    event = cancel_events.get(chat_id)

    if task and not task.done():
        # Signal cancellation
        if event:
            event.set()
        task.cancel()
        await update.message.reply_text("🛑 Cancelling scan…")
    else:
        await update.message.reply_text("ℹ️ No active scan to cancel.")


# --------------------------------------------------------------------------- #
#  Application entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        print("❌ Error: BOT_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)

    app = Application.builder().token(bot_token).build()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settoken", settoken))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("setorg", setorg))
    app.add_handler(CommandHandler("setuser", setuser))
    app.add_handler(CommandHandler("setlang", setlang))
    app.add_handler(CommandHandler("setmax", setmax))
    app.add_handler(CommandHandler("setcontent", setcontent))
    app.add_handler(CommandHandler("setredact", setredact))
    app.add_handler(CommandHandler("setoutput", setoutput))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel_command))

    logger.info("Bot started. Polling…")
    app.run_polling()


if __name__ == "__main__":
    main()
