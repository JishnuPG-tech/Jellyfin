"""Async Telegram bot bridge to Opencode backend.

The bot forwards messages and files to the FastAPI backend and displays
streaming responses with a fully interactive menu for remote control.
It also serves as a Cloud Storage bridge, saving uploaded files to the
user's workspace and allowing users to list/download files over chat.
"""
import asyncio
import os
import logging
import time
import re
import subprocess
from services.opencode_manager import OpencodeManager
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from backend.app.config import settings

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

opencode_mgr = OpencodeManager()


def parse_authorized_users() -> set[int]:
    raw = settings.authorized_users or os.getenv("AUTHORIZED_USERS", "")
    users: set[int] = set()
    for value in [item.strip() for item in raw.split(",") if item.strip()]:
        try:
            users.add(int(value))
        except Exception:
            continue
    return users


AUTHORIZED_USERS = parse_authorized_users()


def is_authorized(user_id: int) -> bool:
    return bool(AUTHORIZED_USERS) and int(user_id) in AUTHORIZED_USERS


def get_control_keyboard(user_id: str):
    """Generates the inline keyboard for interactive TUI session control (keypad layout)."""
    # Replace with Opencode Render service domain if deployed separately
    base_domain = os.getenv("RENDER_EXTERNAL_URL", "https://opencode-mobile.onrender.com")
    webapp_url = f"{base_domain}/webapp?user_id={user_id}"
    
    keyboard = [
        [
            InlineKeyboardButton("🖥️ Open Web Console", web_app=WebAppInfo(url=webapp_url))
        ],
        [
            InlineKeyboardButton("⬆️ Up", callback_data="key_Up"),
        ],
        [
            InlineKeyboardButton("⬅️ Left", callback_data="key_Left"),
            InlineKeyboardButton("🆗 Enter", callback_data="key_Enter"),
            InlineKeyboardButton("➡️ Right", callback_data="key_Right"),
        ],
        [
            InlineKeyboardButton("⬇️ Down", callback_data="key_Down"),
            InlineKeyboardButton("⇥ Tab", callback_data="key_Tab"),
            InlineKeyboardButton("⌫ Backspace", callback_data="key_BSpace"),
        ],
        [
            InlineKeyboardButton("🛑 Ctrl-C", callback_data="control_interrupt"),
            InlineKeyboardButton("🔄 Refresh Screen", callback_data="control_refresh"),
            InlineKeyboardButton("🚀 Launch opencode", callback_data="control_opencode"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Unauthorized.")
        return
    await update.message.reply_text(
        "👋 *Opencode Mobile Terminal Ready.*\n\n"
        "• Click the button below to open your fully interactive Web Console.\n"
        "• Upload any document/file/photo to save it directly to your workspace.\n"
        "• Use `/list` to view your workspace files, and `/download <filename>` to retrieve them.",
        parse_mode="Markdown",
        reply_markup=get_control_keyboard(str(user_id))
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Unauthorized.")
        return
    opencode_mgr.sm.interrupt(str(user_id))
    await update.message.reply_text("Interrupted.")


async def list_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lists all files in the user's workspace."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Unauthorized.")
        return
        
    project = "default"
    ws = os.path.join(opencode_mgr.sm.workspace_root, f"user_{user_id}", project)
    os.makedirs(ws, exist_ok=True)
    
    files = [f for f in os.listdir(ws) if os.path.isfile(os.path.join(ws, f))]
    if not files:
        await update.message.reply_text("📁 Your workspace directory is currently empty.")
        return
        
    file_list = "\n".join([f"• `{name}`" for name in files if not name.startswith(".")])
    await update.message.reply_text(
        f"📁 *Workspace Files (Cloud Storage):*\n\n{file_list}\n\n"
        f"To retrieve a file, type `/download <filename>`",
        parse_mode="Markdown"
    )


async def download_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Uploads a requested file from the workspace to the Telegram chat."""
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Unauthorized.")
        return
        
    if not context.args:
        await update.message.reply_text("Usage: `/download <filename>`", parse_mode="Markdown")
        return
        
    filename = " ".join(context.args)
    filename = os.path.basename(filename)
    
    project = "default"
    ws = os.path.join(opencode_mgr.sm.workspace_root, f"user_{user_id}", project)
    file_path = os.path.join(ws, filename)
    
    if not os.path.exists(file_path):
        await update.message.reply_text(f"❌ File `{filename}` not found in your workspace.", parse_mode="Markdown")
        return
        
    await update.message.reply_text(f"📤 Uploading `{filename}`...")
    try:
        with open(file_path, 'rb') as f:
            await update.message.reply_document(document=f, filename=filename)
    except Exception as e:
        await update.message.reply_text(f"Error transferring file: {e}")


def clean_terminal_output(text: str, keep_whitespace: bool = False) -> tuple[str, bool]:
    """Strips ANSI sequences and extracts Google login links into a clean layout."""
    # 1. Look for Google OAuth URL
    match = re.search(r'https://accounts\.google\.com/o/oauth2/auth\?[^\s\'"\x1b\\>]+', text)
    if match:
        auth_url = match.group(0)
        auth_url = re.split(r'[\x00-\x1f\x7f-\x9f\s\[\]]', auth_url)[0]
        auth_url = auth_url.rstrip(']').rstrip('[m').rstrip(';').rstrip('\\')
        
        url_match = re.search(r'(https://accounts\.google\.com/o/oauth2/auth\?[a-zA-Z0-9_\-=\+%\.&]+)', auth_url)
        if url_match:
            auth_url = url_match.group(1)
        return auth_url, True

    # 2. Strip ANSI escape sequences:
    cleaned = re.sub(r'\x1b\]8;[^\x1b\x07]*[\x1b\x07]', '', text)
    cleaned = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', cleaned)
    cleaned = re.sub(r'\x1b.', '', cleaned)
    if keep_whitespace:
        cleaned = re.sub(r'[\x00-\x07\x09\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
    else:
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', cleaned)
    
    # 3. Strip bracketed paste and leftover truncated CSI commands
    cleaned = cleaned.replace("[?2004l", "").replace("[?2004h", "")
    cleaned = re.sub(r'\[[0-9;?]*[mJKhHdDL]', '', cleaned)
    
    if not keep_whitespace:
        cleaned = cleaned.strip()
        if cleaned in [']', '', ']', ']];', ';', 'm', 'm ]8;;']:
            return "", False
        
    return cleaned, False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Unauthorized.")
        return

    project = "default"
    ws = os.path.join(opencode_mgr.sm.workspace_root, f"user_{user_id}", project)
    os.makedirs(ws, exist_ok=True)

    # 1. Handle Incoming Cloud Storage Documents
    if update.message.document:
        doc = update.message.document
        file_name = doc.file_name
        file_path = os.path.join(ws, file_name)
        
        sent_msg = await update.message.reply_text(f"📥 Downloading `{file_name}` to workspace...")
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(file_path)
            await sent_msg.edit_text(
                f"✅ Saved `{file_name}` to workspace.\nYou can now access/run it in the Opencode CLI!"
            )
        except Exception as e:
            await sent_msg.edit_text(f"❌ Failed to download file: {e}")
        return

    # 2. Handle Incoming Photos
    elif update.message.photo:
        photo = update.message.photo[-1]
        file_name = f"uploaded_photo_{int(time.time())}.jpg"
        file_path = os.path.join(ws, file_name)
        
        sent_msg = await update.message.reply_text("📸 Saving image to workspace...")
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            await tg_file.download_to_drive(file_path)
            await sent_msg.edit_text(
                f"✅ Saved image as `{file_name}` in workspace.\nCLI has full access to read it!"
            )
        except Exception as e:
            await sent_msg.edit_text(f"❌ Failed to download photo: {e}")
        return

    # 3. Handle Normal Text Input
    text = update.message.text or ""

    opencode_mgr.start_for_user(str(user_id))
    opencode_mgr.send_command(str(user_id), text)

    sent = await update.message.reply_text("Running...", reply_markup=get_control_keyboard(str(user_id)))

    async def streamer():
        try:
            async for chunk in opencode_mgr.sm.stream_output(str(user_id)):
                try:
                    display, is_card = clean_terminal_output(chunk)
                    if not display:
                        continue
                    
                    if is_card:
                        keyboard = [[InlineKeyboardButton("🔗 Log In (Google)", url=display)]]
                        reply_markup = InlineKeyboardMarkup(keyboard)
                        
                        await context.bot.edit_message_text(
                            chat_id=sent.chat_id,
                            message_id=sent.message_id,
                            text=(
                                "🔑 *Opencode Login Required*\n\n"
                                "Please click the button below to authorize, then copy the code from your browser and paste it here in the chat:"
                            ),
                            parse_mode="Markdown",
                            reply_markup=reply_markup
                        )
                    else:
                        if len(display) > 3500:
                            display = display[-3500:]
                        code_text = "```\n" + display + "\n```"
                        await context.bot.edit_message_text(
                            chat_id=sent.chat_id,
                            message_id=sent.message_id,
                            text=code_text,
                            parse_mode="Markdown",
                            reply_markup=get_control_keyboard(str(user_id))
                        )

                except Exception:
                    pass
        except asyncio.CancelledError:
            return

    context.application.create_task(streamer())


async def refresh_terminal_screen(query, user_id: int):
    """Captures the current terminal screen state and updates the message inline."""
    output = opencode_mgr.read(str(user_id), lines=40)
    display, is_card = clean_terminal_output(output)
    
    if is_card:
        keyboard = [[InlineKeyboardButton("🔗 Log In (Google)", url=display)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            text=(
                "🔑 *Opencode Login Required*\n\n"
                "Please click the button below to authorize, then copy the code and paste it here:"
            ),
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    elif display:
        code_text = "```\n" + display + "\n```"
        try:
            await query.edit_message_text(
                text=code_text,
                parse_mode="Markdown",
                reply_markup=get_control_keyboard(str(user_id))
            )
        except Exception as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Failed to edit terminal screen: {e}")
    else:
        try:
            await query.edit_message_text(
                text="`Terminal output is empty.`",
                parse_mode="Markdown",
                reply_markup=get_control_keyboard(str(user_id))
            )
        except Exception:
            pass


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes button presses from the control keyboard."""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await query.message.reply_text("Unauthorized.")
        return
        
    action = query.data
    logger.info(f"handle_callback: user {user_id} triggered action {action}")
    
    if action.startswith("key_"):
        key_name = action.split("_")[1]
        try:
            session = opencode_mgr.sm._session_name(str(user_id))
            subprocess.run(["tmux", "send-keys", "-t", session, key_name], check=True)
            await asyncio.sleep(0.15)
            await refresh_terminal_screen(query, user_id)
        except Exception as e:
            logger.error(f"Error sending key {key_name} to tmux: {e}")
            
    elif action == "control_interrupt":
        opencode_mgr.sm.interrupt(str(user_id))
        await query.message.reply_text("🛑 Sent Interrupt (Ctrl-C) to your session.")
        await asyncio.sleep(0.15)
        await refresh_terminal_screen(query, user_id)
        
    elif action == "control_refresh":
        await refresh_terminal_screen(query, user_id)
            
    elif action == "control_opencode":
        opencode_mgr.start_for_user(str(user_id))
        opencode_mgr.send_command(str(user_id), "opencode")
        await query.message.reply_text("🚀 Sent launch command: 'opencode'")
        await asyncio.sleep(0.3)
        await refresh_terminal_screen(query, user_id)
        
    elif action == "control_config":
        opencode_mgr.send_command(str(user_id), "/config")
        await query.message.reply_text("⚙️ Sent command: '/config'")
        await asyncio.sleep(0.3)
        await refresh_terminal_screen(query, user_id)


BOT_COMMANDS = [
    BotCommand("start", "Open interactive terminal console"),
    BotCommand("cancel", "Send Ctrl-C to terminal"),
    BotCommand("list", "List workspace files"),
    BotCommand("download", "Download file from workspace"),
]


# Global application reference for async execution
telegram_app = None

# Handler references for webhook mode (registered before initialize)
_handler_map = {}

def _register_handler(name, handler_fn, handler_type="command"):
    _handler_map[(handler_type, name)] = handler_fn


async def _handle_webhook_update(data: dict):
    """Manually route a raw Telegram update dict to the correct handler.

    Used in webhook mode on HuggingFace where the bot can't call initialize()
    (no outbound HTTP), so python-telegram-bot's process_update is unavailable.
    """
    msg = data.get("message") or data.get("callback_query")
    if not msg:
        return

    # Callback query (inline keyboard button press)
    if "callback_query" in data:
        cq = data["callback_query"]
        query_data = cq.get("data", "")
        user_id = str(cq["from"]["id"])
        chat_id = cq["message"]["chat"]["id"]
        msg_id = cq["message"]["message_id"]

        if not is_authorized(int(user_id)):
            return

        action = query_data
        logger.info(f"[Webhook CB] {user_id} -> {action}")

        opencode_mgr.start_for_user(user_id)

        if action.startswith("key_"):
            key_name = action.split("_", 1)[1]
            try:
                session = opencode_mgr.sm._session_name(user_id)
                subprocess.run(["tmux", "send-keys", "-t", session, key_name], check=True)
            except Exception as e:
                logger.error(f"Error sending key {key_name}: {e}")

        elif action == "control_interrupt":
            opencode_mgr.sm.interrupt(user_id)

        elif action == "control_refresh":
            pass  # handled below

        elif action == "control_opencode":
            opencode_mgr.start_for_user(user_id)
            opencode_mgr.send_command(user_id, "opencode")

        elif action == "control_config":
            opencode_mgr.send_command(user_id, "/config")

        # Send updated terminal screen
        await asyncio.sleep(0.15)
        output = opencode_mgr.read(user_id, lines=40)
        display, is_card = clean_terminal_output(output)
        if display:
            code_text = "```\n" + display + "\n```"
        else:
            code_text = "`Terminal output is empty.`"

        # Answer callback query (remove loading spinner)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                token = os.getenv("BOT_TOKEN") or settings.bot_token
                base_url = os.getenv("TELEGRAM_BASE_URL", "https://api.telegram.org")
                await client.get(
                    f"{base_url}/bot{token}/answerCallbackQuery",
                    params={"callback_query_id": cq["id"]}
                )
                await client.post(
                    f"{base_url}/bot{token}/editMessageText",
                    json={
                        "chat_id": chat_id,
                        "message_id": msg_id,
                        "text": code_text,
                        "parse_mode": "Markdown",
                        "reply_markup": _keyboard_to_dict(get_control_keyboard(user_id)),
                    }
                )
        except Exception as e:
            logger.debug(f"Webhook callback answer error: {e}")
        return

    # Regular message
    message = msg
    text = (message.get("text") or "").strip()
    user_id = str(message["from"]["id"])
    chat_id = message["chat"]["id"]

    if not is_authorized(int(user_id)):
        return

    # Handle / commands
    if text.startswith("/"):
        cmd = text.split()[0].lower()
        if cmd == "/start":
            await _handle_start_webhook(user_id, chat_id, message["from"].get("first_name", "User"))
            return
        elif cmd == "/cancel":
            opencode_mgr.sm.interrupt(user_id)
            await _send_telegram_message(chat_id, "Interrupted.")
            return
        elif cmd == "/list":
            await _handle_list_webhook(user_id, chat_id)
            return
        elif cmd in ("/download", "/get"):
            filename = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            await _handle_download_webhook(user_id, chat_id, filename)
            return
        return

    # Text input → send to tmux
    if not text:
        return

    opencode_mgr.start_for_user(user_id)
    opencode_mgr.send_command(user_id, text)

    # Send "Running..." then the terminal output
    await _send_telegram_message(chat_id, "Running...", reply_markup=_keyboard_to_dict(get_control_keyboard(user_id)))
    await asyncio.sleep(0.5)
    output = opencode_mgr.read(user_id, lines=40)
    display, is_card = clean_terminal_output(output)
    if display:
        code_text = "```\n" + display + "\n```"
    else:
        code_text = "`Starting...`"
    await _send_telegram_message(chat_id, code_text, parse_mode="Markdown", reply_markup=_keyboard_to_dict(get_control_keyboard(user_id)))


async def _handle_start_webhook(user_id: str, chat_id: int, name: str):
    opencode_mgr.start_for_user(user_id)
    try:
        session = opencode_mgr.sm._session_name(user_id)
        subprocess.run(["tmux", "resize-window", "-t", session, "-x", "100", "-y", "40"], capture_output=True)
    except Exception:
        pass
    # Launch opencode if not running
    try:
        res = subprocess.run(
            ["tmux", "display-message", "-p", "-t", f"{session}:0.0", "-F", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=2,
        )
        cmd = res.stdout.strip()
        if cmd in ("", "bash", "sh"):
            opencode_mgr.send_command(user_id, "opencode")
    except Exception:
        opencode_mgr.send_command(user_id, "opencode")

    text = (
        f"👋 *OpenCode Mobile Terminal Ready.*\n\n"
        f"• Click the button below to open your fully interactive Web Console.\n"
        f"• Upload any document/file/photo to save it directly to your workspace.\n"
        f"• Use `/list` to view your workspace files, and `/download <filename>` to retrieve them."
    )
    await _send_telegram_message(chat_id, text, parse_mode="Markdown", reply_markup=_keyboard_to_dict(get_control_keyboard(user_id)))


async def _handle_list_webhook(user_id: str, chat_id: int):
    ws = os.path.join(opencode_mgr.sm.workspace_root, f"user_{user_id}", "default")
    os.makedirs(ws, exist_ok=True)
    files = [f for f in os.listdir(ws) if os.path.isfile(os.path.join(ws, f)) and not f.startswith(".")]
    if not files:
        await _send_telegram_message(chat_id, "Your workspace directory is currently empty.")
        return
    file_list = "\n".join([f"• `{name}`" for name in files])
    await _send_telegram_message(
        chat_id,
        f"📁 *Workspace Files ({len(files)}):*\n\n{file_list}\n\n"
        f"Use `/download <filename>` to retrieve.",
        parse_mode="Markdown"
    )


async def _handle_download_webhook(user_id: str, chat_id: int, filename: str):
    if not filename:
        await _send_telegram_message(chat_id, "Usage: `/download <filename>`", parse_mode="Markdown")
        return
    filename = os.path.basename(filename)
    ws = os.path.join(opencode_mgr.sm.workspace_root, f"user_{user_id}", "default")
    file_path = os.path.join(ws, filename)
    if not os.path.exists(file_path):
        await _send_telegram_message(chat_id, f"File `{filename}` not found.", parse_mode="Markdown")
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = os.getenv("BOT_TOKEN") or settings.bot_token
            base_url = os.getenv("TELEGRAM_BASE_URL", "https://api.telegram.org")
            with open(file_path, "rb") as f:
                import aiofiles
                file_data = await asyncio.to_thread(f.read)
            await client.post(
                f"{base_url}/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": f"`{filename}`", "parse_mode": "Markdown"},
                files={"document": (filename, file_data)}
            )
    except Exception as e:
        await _send_telegram_message(chat_id, f"Upload failed: {e}")


def _keyboard_to_dict(markup):
    """Convert InlineKeyboardMarkup to a JSON-serializable dict for raw HTTP calls."""
    if not markup:
        return None
    rows = []
    for row in markup.inline_keyboard:
        rows.append([
            {"text": btn.text, "callback_data": btn.callback_data,
             "url": btn.url} if btn.web_app is None else
            {"text": btn.text, "web_app": {"url": btn.web_app.url}}
            for btn in row
        ])
    return {"inline_keyboard": rows}


async def _send_telegram_message(chat_id: int, text: str, parse_mode: str = None, reply_markup: dict = None):
    """Send a message via raw HTTP (works without bot.initialize())."""
    import httpx as _httpx
    try:
        token = os.getenv("BOT_TOKEN") or settings.bot_token
        base_url = os.getenv("TELEGRAM_BASE_URL", "https://api.telegram.org")
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with _httpx.AsyncClient(timeout=10.0) as client:
            await client.post(f"{base_url}/bot{token}/sendMessage", json=payload)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")

async def run_bot_async() -> None:
    global telegram_app
    logger.info("run_bot_async: starting bot initialization task...")
    try:
        token = os.getenv("BOT_TOKEN") or settings.bot_token
        if not token:
            logger.warning("BOT_TOKEN environment variable not set. Telegram bot will not start.")
            return

        base_url = os.getenv("TELEGRAM_BASE_URL")
        if base_url:
            logger.info(f"Using custom Telegram base URL: {base_url}")
            telegram_app = ApplicationBuilder().token(token).base_url(base_url).build()
        else:
            logger.info("Using default Telegram API URL (api.telegram.org)")
            telegram_app = ApplicationBuilder().token(token).build()

        telegram_app.add_handler(CommandHandler("start", start))
        telegram_app.add_handler(CommandHandler("cancel", cancel))
        telegram_app.add_handler(CommandHandler("list", list_files))
        telegram_app.add_handler(CommandHandler("download", download_file))
        telegram_app.add_handler(CommandHandler("get", download_file))
        telegram_app.add_handler(CallbackQueryHandler(handle_callback))

        telegram_app.add_handler(MessageHandler(
            (filters.TEXT | filters.Document.ALL | filters.PHOTO) & (~filters.COMMAND),
            handle_message
        ))

        # HuggingFace Spaces: no outbound HTTP — skip initialize/start/set_webhook.
        # The user must set the webhook manually (see instructions below).
        public_url = os.getenv("HF_URL")
        if public_url:
            logger.info("HF_URL detected — running in webhook-only mode (no outbound HTTP).")
            logger.info("Telegram updates will arrive at: " + public_url.rstrip('/') + "/api/telegram-webhook")
            logger.info("SET YOUR WEBHOOK MANUALLY with this command from your local machine:")
            logger.info(f'  curl "https://api.telegram.org/bot{token}/setWebhook?url={public_url.rstrip("/")}/api/telegram-webhook&drop_pending_updates=true"')
            # Register the app but do NOT call initialize/start (they need outbound HTTP)
            telegram_app.post_init = None
            telegram_app.post_shutdown = None
            logger.info("Bot handlers registered. Waiting for webhook updates.")
            return

        # Render / local: normal initialization with outbound HTTP + polling
        logger.info("Initializing Telegram bot application...")
        await telegram_app.initialize()
        try:
            await telegram_app.bot.set_my_commands(BOT_COMMANDS)
        except Exception:
            pass
        logger.info("Starting Telegram bot application...")
        await telegram_app.start()
        logger.info("Starting Telegram bot polling...")
        await telegram_app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Telegram bot is running!")
    except Exception as e:
        logger.error(f"FATAL ERROR starting Telegram bot: {e}", exc_info=True)


async def stop_bot_async() -> None:
    global telegram_app
    if telegram_app:
        logger.info("Stopping Telegram bot...")
        try:
            if telegram_app.updater and telegram_app.updater.running:
                await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()
        except Exception as e:
            logger.error(f"Error during bot shutdown: {e}")


def main() -> None:
    token = os.getenv("BOT_TOKEN") or settings.bot_token
    if not token:
        logger.error("BOT_TOKEN not set; Telegram bot will stay idle so the Space can continue starting")
        while True:
            time.sleep(60)
        return

    base_url = os.getenv("TELEGRAM_BASE_URL")
    if base_url:
        logger.info(f"Using custom Telegram base URL: {base_url}")
        app = ApplicationBuilder().token(token).base_url(base_url).build()
    else:
        app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("list", list_files))
    app.add_handler(CommandHandler("download", download_file))
    app.add_handler(CommandHandler("get", download_file))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.Document.ALL | filters.PHOTO) & (~filters.COMMAND), 
        handle_message
    ))

    logger.info("Starting Telegram bot")
    try:
        app.run_polling()
    except Exception:
        logger.exception("Telegram bot crashed; sleeping before retry")
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()
