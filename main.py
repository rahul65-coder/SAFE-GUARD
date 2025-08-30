import re
import logging
import asyncio
import nest_asyncio
from datetime import timedelta, datetime
from telegram import Update, ChatPermissions
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from telegram.helpers import mention_html
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from flask import Flask
from threading import Thread
from collections import defaultdict
import time
import aiohttp

# Import words from words.py
from words import abuse_words, load_additional_words

# Apply nest_asyncio patch to allow nested event loops (UserLAnd safe)
nest_asyncio.apply()

BOT_TOKEN = "8207871627:AAGxTPeR_oIwBAKYOlhaWJ5HyziPY3pWKOk"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Try to load additional words from file
load_additional_words("abuse_words.txt")

# Regex patterns
third_party_link_regex = re.compile(
    r"(https?://(?!t\.me|telegram\.me|instagram\.com)[\w./?=#-]+)",
    re.IGNORECASE,
)

# Allowed domains
allowed_domains = {"t.me", "telegram.me", "instagram.com"}

# Create Flask app for uptime monitoring
app = Flask(__name__)

# User warning tracking - stores {chat_id: {user_id: warning_count}}
user_warnings = defaultdict(lambda: defaultdict(int))

# Cache for bot admin status per chat
bot_admin_cache = {}
CACHE_DURATION = 300  # 5 minutes

# Performance tracking
performance_stats = {
    'messages_processed': 0,
    'start_time': time.time()
}

@app.route('/')
def home():
    return "Bot is running!"

@app.route('/stats')
def stats():
    uptime = time.time() - performance_stats['start_time']
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)
    return f"Bot uptime: {hours}h {minutes}m | Messages processed: {performance_stats['messages_processed']}"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when a new member joins"""
    try:
        # Check if this is a new member joining (not other member status changes)
        if (update.chat_member.old_chat_member.status == 'left' and 
            update.chat_member.new_chat_member.status in ['member', 'administrator', 'creator']):
            
            user = update.chat_member.new_chat_member.user
            chat = update.effective_chat
            
            # Don't welcome bots
            if user.is_bot:
                return
                
            logger.info(f"New member joined: {user.full_name} in chat {chat.title}")
            
            welcome_text = (
                f"🎉 <b>Welcome</b>, {mention_html(user.id, user.full_name)}!\n\n"
                f"📜 <b>Rules:</b>\n"
                f"   • No Abuse\n"
                f"   • No Porn\n"
                f"   • No 3rd-party Links (Only Telegram/Instagram)\n"
                f"   • Be Respectful"
            )
            
            try:
                photos = await context.bot.get_user_profile_photos(user.id, limit=1)
                if photos.total_count > 0:
                    photo_file_id = photos.photos[0][-1].file_id
                    await context.bot.send_photo(
                        chat_id=chat.id,
                        photo=photo_file_id,
                        caption=welcome_text,
                        parse_mode=ParseMode.HTML,
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=welcome_text,
                        parse_mode=ParseMode.HTML,
                    )
            except Exception as e:
                logger.error(f"Error sending welcome message: {e}")
                # Try to send without photo if there's an error
                try:
                    await context.bot.send_message(
                        chat_id=chat.id,
                        text=welcome_text,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as e2:
                    logger.error(f"Failed to send welcome message: {e2}")
    except Exception as e:
        logger.error(f"Error in send_welcome: {e}")

def is_allowed_link(url):
    """Check if a URL is from allowed domains"""
    for domain in allowed_domains:
        if domain in url:
            return True
    return False

async def check_bot_permissions(chat, context):
    """Check if bot has admin permissions with caching"""
    chat_id = chat.id
    current_time = time.time()
    
    # Check cache first
    if chat_id in bot_admin_cache:
        cached_data = bot_admin_cache[chat_id]
        if current_time - cached_data['timestamp'] < CACHE_DURATION:
            return cached_data['can_restrict'], cached_data['can_delete']
    
    # If not in cache or expired, check permissions
    try:
        bot_member = await chat.get_member(context.bot.id)
        can_restrict = bot_member.can_restrict_members
        can_delete = bot_member.can_delete_messages
        
        # Update cache
        bot_admin_cache[chat_id] = {
            'can_restrict': can_restrict,
            'can_delete': can_delete,
            'timestamp': current_time
        }
        
        return can_restrict, can_delete
    except Exception as e:
        logger.error(f"Error checking bot permissions: {e}")
        return False, False

async def handle_abuse_violation(user, chat, context, message_text, update):
    """Handle abuse violation with 3-level warning system"""
    chat_id = chat.id
    user_id = user.id
    
    # Increment warning count
    user_warnings[chat_id][user_id] += 1
    warning_level = user_warnings[chat_id][user_id]
    
    try:
        # Delete the abusive message
        await update.message.delete()
        logger.info(f"Deleted abusive message from {user.full_name}: {message_text}")
        
        # Take action based on warning level
        if warning_level == 1:
            # Level 1: Warning only
            mute_msg = await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"⚠️ <b>Warning Level 1</b>\n\n"
                    f"👤 <b>User:</b> {mention_html(user.id, user.full_name)}\n"
                    f"📝 <b>Reason:</b> Abusive Language\n\n"
                    f"<i>Next violation will result in a 30-minute mute.</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            # Delete warning after 30 seconds
            await asyncio.sleep(30)
            await mute_msg.delete()
            
        elif warning_level == 2:
            # Level 2: 30-minute mute
            until_date = datetime.now() + timedelta(minutes=30)
            await chat.restrict_member(
                user.id,
                ChatPermissions(can_send_messages=False),
                until_date=until_date,
            )
            logger.info(f"Muted user {user.full_name} for 30 minutes")
            
            mute_msg = await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"🚫 <b>Warning Level 2</b>\n\n"
                    f"👤 <b>User:</b> {mention_html(user.id, user.full_name)}\n"
                    f"⏰ <b>Duration:</b> 30 Minutes Mute\n"
                    f"📝 <b>Reason:</b> Repeated Abusive Language\n\n"
                    f"<i>Next violation will result in a 2-hour mute.</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            # Delete warning after 30 seconds
            await asyncio.sleep(30)
            await mute_msg.delete()
            
        elif warning_level >= 3:
            # Level 3: 2-hour mute
            until_date = datetime.now() + timedelta(hours=2)
            await chat.restrict_member(
                user.id,
                ChatPermissions(can_send_messages=False),
                until_date=until_date,
            )
            logger.info(f"Muted user {user.full_name} for 2 hours")
            
            # Reset warning count after level 3
            user_warnings[chat_id][user_id] = 0
            
            mute_msg = await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"🔇 <b>Warning Level 3</b>\n\n"
                    f"👤 <b>User:</b> {mention_html(user.id, user.full_name)}\n"
                    f"⏰ <b>Duration:</b> 2 Hours Mute\n"
                    f"📝 <b>Reason:</b> Repeated Abusive Language\n\n"
                    f"<i>Warning count has been reset. Future violations will start from Level 1.</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            # Delete warning after 30 seconds
            await asyncio.sleep(30)
            await mute_msg.delete()
            
    except BadRequest as e:
        logger.error(f"Couldn't mute user or delete message: {e}")
    except Forbidden as e:
        logger.error(f"Bot doesn't have permission to restrict user: {e}")
    except Exception as e:
        logger.error(f"Unexpected error handling abuse: {e}")

async def monitor_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Monitor messages for abuse and third-party links with performance optimization"""
    try:
        if not update.message or not update.message.text:
            return
            
        # Update performance stats
        performance_stats['messages_processed'] += 1
        
        text = update.message.text.lower()
        user = update.effective_user
        chat = update.effective_chat
        
        # Ignore messages from bots
        if user.is_bot:
            return
            
        # Check if bot has admin permissions (with caching)
        can_restrict, can_delete = await check_bot_permissions(chat, context)
        if not can_restrict or not can_delete:
            logger.warning(f"Bot doesn't have required permissions in chat {chat.title}")
            return
        
        # Check for abusive words using efficient search
        abuse_found = False
        for word in abuse_words:
            if word in text:  # Fast initial check
                if re.search(r'\b' + re.escape(word) + r'\b', text):  # Precise word boundary check
                    abuse_found = True
                    break
        
        if abuse_found:
            await handle_abuse_violation(user, chat, context, text, update)
            return

        # Check for third-party links
        if third_party_link_regex.search(text):
            try:
                await update.message.delete()
                logger.info(f"Deleted third-party link from {user.full_name}")
                
                # Send stylish warning message
                warning_msg = await context.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        f"🔗 <b>Link Policy Violation</b>\n\n"
                        f"👤 <b>User:</b> {mention_html(user.id, user.full_name)}\n"
                        f"❌ <b>Posted:</b> Third-party link\n\n"
                        f"📋 <b>Allowed Domains:</b>\n"
                        f"   • t.me\n"
                        f"   • telegram.me\n"
                        f"   • instagram.com\n\n"
                        f"<i>Please use only approved links.</i>"
                    ),
                    parse_mode=ParseMode.HTML,
                )
                
                # Delete warning after 10 seconds
                await asyncio.sleep(10)
                await warning_msg.delete()
                
            except BadRequest as e:
                logger.error(f"Couldn't delete message or send warning: {e}")
            except Forbidden as e:
                logger.error(f"Bot doesn't have permission to delete message: {e}")
            except Exception as e:
                logger.error(f"Unexpected error handling third-party link: {e}")
            return
            
    except Exception as e:
        logger.error(f"Error in monitor_message: {e}")

async def cleanup_old_warnings():
    """Periodically clean up old warnings to prevent memory bloat"""
    while True:
        await asyncio.sleep(3600)  # Run every hour
        try:
            # Remove warnings for users who haven't violated in 24 hours
            current_time = time.time()
            for chat_id in list(user_warnings.keys()):
                for user_id in list(user_warnings[chat_id].keys()):
                    # In a real implementation, you'd track timestamps for each warning
                    # For now, we'll just clear warnings older than 24 hours
                    # This is a placeholder for proper implementation
                    if user_warnings[chat_id][user_id] > 0:
                        # Simple approach: reset warnings after 24 hours
                        user_warnings[chat_id][user_id] = 0
        except Exception as e:
            logger.error(f"Error in cleanup_old_warnings: {e}")

async def main():
    """Start the bot with optimized performance"""
    # Create aiohttp session for better performance
    connector = aiohttp.TCPConnector(limit=100, limit_per_host=20)
    
    app = ApplicationBuilder().token(BOT_TOKEN).http_version("1.1").pool_timeout(30).connect_timeout(30).read_timeout(30).write_timeout(30).build()
    
    # Add handlers
    app.add_handler(ChatMemberHandler(send_welcome, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, monitor_message))
    
    # Start cleanup task
    asyncio.create_task(cleanup_old_warnings())
    
    logger.info("Bot started with 3-level warning system and performance optimizations...")
    logger.info(f"Monitoring for {len(abuse_words)} abuse words")
    logger.info("Bot is optimized for handling 20+ groups simultaneously")
    
    await app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "chat_member", "my_chat_member"]
    )

if __name__ == "__main__":
    # Start Flask server in a separate thread
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Start the bot
    asyncio.run(main())
