import re
import logging
import asyncio
import nest_asyncio
from datetime import timedelta
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

# User warning tracking - {chat_id: {user_id: warning_count}}
user_warnings = defaultdict(lambda: defaultdict(int))

# Cache for performance optimization
permission_cache = {}
last_permission_check = {}

# Message tracking for bulk deletion
recent_messages = defaultdict(list)

# Create Flask app for uptime monitoring
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

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
    """Check if bot has admin permissions with caching for performance"""
    current_time = time.time()
    cache_key = f"{chat.id}_{context.bot.id}"
    
    # Check cache first (valid for 5 minutes)
    if cache_key in permission_cache and current_time - last_permission_check.get(cache_key, 0) < 300:
        return permission_cache[cache_key]
    
    try:
        bot_member = await chat.get_member(context.bot.id)
        has_permissions = bot_member.can_restrict_members and bot_member.can_delete_messages
        permission_cache[cache_key] = has_permissions
        last_permission_check[cache_key] = current_time
        return has_permissions
    except Exception as e:
        logger.error(f"Error checking bot permissions: {e}")
        permission_cache[cache_key] = False
        last_permission_check[cache_key] = current_time
        return False

async def delete_user_messages(chat, user_id, context):
    """Delete all recent messages from a user"""
    try:
        if chat.id in recent_messages and user_id in recent_messages[chat.id]:
            message_ids = [msg.message_id for msg in recent_messages[chat.id][user_id]]
            if message_ids:
                # Delete messages in batches of 100 (Telegram API limit)
                for i in range(0, len(message_ids), 100):
                    batch = message_ids[i:i+100]
                    await context.bot.delete_messages(chat.id, batch)
                
                logger.info(f"Deleted {len(message_ids)} messages from user {user_id}")
            
            # Clear the user's message history
            if user_id in recent_messages[chat.id]:
                del recent_messages[chat.id][user_id]
                
    except Exception as e:
        logger.error(f"Error deleting user messages: {e}")

async def ban_multiple_users(chat, user_ids, context, reason="Violating group rules"):
    """Ban multiple users at once with efficient batch processing"""
    if not user_ids:
        return
    
    success_count = 0
    failed_count = 0
    
    # Process users in batches to avoid rate limiting
    batch_size = 20  # Process 20 users at a time with a small delay
    
    for i in range(0, len(user_ids), batch_size):
        batch = user_ids[i:i+batch_size]
        
        # Create tasks for all users in this batch
        tasks = []
        for user_id in batch:
            tasks.append(ban_single_user(chat, user_id, context, reason))
        
        # Execute all tasks concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Count successes and failures
        for result in results:
            if isinstance(result, Exception):
                failed_count += 1
                logger.error(f"Failed to ban user: {result}")
            else:
                success_count += 1
        
        # Small delay between batches to avoid rate limiting
        if i + batch_size < len(user_ids):
            await asyncio.sleep(1)
    
    logger.info(f"Banned {success_count} users successfully, {failed_count} failures")
    return success_count, failed_count

async def ban_single_user(chat, user_id, context, reason="Violating group rules"):
    """Ban a single user with error handling"""
    try:
        await chat.ban_member(user_id)
        logger.info(f"Banned user {user_id} for: {reason}")
        return True
    except BadRequest as e:
        logger.error(f"Could not ban user {user_id}: {e}")
        raise e
    except Forbidden as e:
        logger.error(f"Bot doesn't have permission to ban user {user_id}: {e}")
        raise e
    except Exception as e:
        logger.error(f"Unexpected error banning user {user_id}: {e}")
        raise e

async def handle_abuse(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle abusive language with 3-level warning system"""
    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id
    chat_id = chat.id
    
    # Check for abusive words
    found_abuses = []
    for word in abuse_words:
        if re.search(r'\b' + re.escape(word) + r'\b', text):
            found_abuses.append(word)
    
    if not found_abuses:
        return False
    
    try:
        # Delete ALL recent messages from this user
        await delete_user_messages(chat, user_id, context)
        
        # Increment warning count
        user_warnings[chat_id][user_id] += 1
        warning_level = user_warnings[chat_id][user_id]
        
        # Take action based on warning level
        if warning_level == 1:
            # Level 1: Warning only
            mute_msg = await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"⚠️ <b>Warning Level 1</b>\n\n"
                    f"👤 <b>User:</b> {mention_html(user.id, user.full_name)}\n"
                    f"📝 <b>Reason:</b> Abusive Language\n"
                    f"❌ <b>Detected Words:</b> {', '.join(found_abuses[:3])}{'...' if len(found_abuses) > 3 else ''}\n\n"
                    f"<i>Next violation will result in a 30-minute mute.</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(10)
            await mute_msg.delete()
            
        elif warning_level == 2:
            # Level 2: 30-minute mute
            until_date = update.message.date + timedelta(minutes=30)
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
                    f"📝 <b>Reason:</b> Abusive Language\n"
                    f"❌ <b>Detected Words:</b> {', '.join(found_abuses[:3])}{'...' if len(found_abuses) > 3 else ''}\n\n"
                    f"<i>Next violation will result in a 2-hour mute.</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(10)
            await mute_msg.delete()
            
        elif warning_level >= 3:
            # Level 3: 2-hour mute
            until_date = update.message.date + timedelta(hours=2)
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
                    f"📝 <b>Reason:</b> Abusive Language\n"
                    f"❌ <b>Detected Words:</b> {', '.join(found_abuses[:3])}{'...' if len(found_abuses) > 3 else ''}\n\n"
                    f"<i>Warning counter has been reset.</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(10)
            await mute_msg.delete()
        
        return True
        
    except BadRequest as e:
        logger.error(f"Couldn't mute user or delete message: {e}")
        # Try to send a warning even if we can't mute
        try:
            warning_msg = await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"⚠️ <b>Warning</b>\n\n"
                    f"👤 {mention_html(user.id, user.full_name)}\n"
                    f"❌ Used abusive language!\n\n"
                    f"<i>This behavior is not allowed.</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(10)
            await warning_msg.delete()
        except Exception as e2:
            logger.error(f"Couldn't send warning message: {e2}")
    except Forbidden as e:
        logger.error(f"Bot doesn't have permission to restrict user: {e}")
    except Exception as e:
        logger.error(f"Unexpected error handling abuse: {e}")
    
    return False

async def monitor_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Monitor messages for abuse and third-party links"""
    try:
        if not update.message or not update.message.text:
            return
            
        text = update.message.text.lower()
        user = update.effective_user
        chat = update.effective_chat
        
        # Ignore messages from bots
        if user.is_bot:
            return
            
        # Store message for potential bulk deletion
        if chat.id not in recent_messages:
            recent_messages[chat.id] = defaultdict(list)
        
        # Keep only last 50 messages per user to avoid memory issues
        if len(recent_messages[chat.id][user.id]) >= 50:
            recent_messages[chat.id][user.id].pop(0)
        
        recent_messages[chat.id][user.id].append(update.message)
            
        # Check if bot has admin permissions (with caching for performance)
        if not await check_bot_permissions(chat, context):
            logger.warning(f"Bot doesn't have required permissions in chat {chat.title}")
            return
        
        # Check for abusive words
        abuse_detected = await handle_abuse(update, context, text)
        if abuse_detected:
            return

        # Check for third-party links
        links = third_party_link_regex.findall(text)
        for link in links:
            if not is_allowed_link(link):
                try:
                    # Delete the message with the third-party link
                    await update.message.delete()
                    
                    # Send a warning that will auto-delete
                    warning_msg = await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            f"⚠️ <b>Link Warning</b>\n\n"
                            f"👤 {mention_html(user.id, user.full_name)}\n"
                            f"🔗 Third-party links are not allowed!\n\n"
                            f"<i>Only Telegram and Instagram links are permitted.</i>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                    
                    # Delete warning after 10 seconds
                    await asyncio.sleep(10)
                    await warning_msg.delete()
                    
                    break  # Only warn once per message
                except BadRequest as e:
                    logger.error(f"Couldn't delete message: {e}")
                except Forbidden as e:
                    logger.error(f"Bot doesn't have permission to delete message: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error handling link: {e}")
                    
    except Exception as e:
        logger.error(f"Error in monitor_message: {e}")

def main():
    # Start Flask server in a separate thread for uptime monitoring
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    # Create the Application
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(ChatMemberHandler(send_welcome, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, monitor_message))
    
    # Start the bot
    application.run_polling()

if __name__ == "__main__":
    main()
