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
            
        # Check if bot has admin permissions
        try:
            bot_member = await chat.get_member(context.bot.id)
            if not bot_member.can_restrict_members or not bot_member.can_delete_messages:
                logger.warning(f"Bot doesn't have required permissions in chat {chat.title}")
                return
        except Exception as e:
            logger.error(f"Error checking bot permissions: {e}")
            return
        
        # Check for abusive words
        for word in abuse_words:
            if re.search(r'\b' + re.escape(word) + r'\b', text):
                try:
                    # Delete the abusive message
                    await update.message.delete()
                    logger.info(f"Deleted abusive message from {user.full_name}: {text}")
                    
                    # Mute user for 2 hours
                    until_date = update.message.date + timedelta(hours=2)
                    await chat.restrict_member(
                        user.id,
                        ChatPermissions(can_send_messages=False),
                        until_date=until_date,
                    )
                    logger.info(f"Muted user {user.full_name} for 2 hours")
                    
                    # Send stylish warning message
                    mute_msg = await context.bot.send_message(
                        chat_id=chat.id,
                        text=(
                            f"🚫 <b>Action Taken</b>\n\n"
                            f"👤 <b>User:</b> {mention_html(user.id, user.full_name)}\n"
                            f"⏰ <b>Duration:</b> 2 Hours Mute\n"
                            f"📝 <b>Reason:</b> Abusive Language\n\n"
                            f"<i>Please maintain respectful conversation.</i>"
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                    
                    # Delete warning after 30 seconds
                    await asyncio.sleep(30)
                    await mute_msg.delete()
                    
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
                        await asyncio.sleep(30)
                        await warning_msg.delete()
                    except Exception as e2:
                        logger.error(f"Couldn't send warning message: {e2}")
                except Forbidden as e:
                    logger.error(f"Bot doesn't have permission to restrict user: {e}")
                except Exception as e:
                    logger.error(f"Unexpected error handling abuse: {e}")
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

async def main():
    """Start the bot"""
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add handlers
    app.add_handler(ChatMemberHandler(send_welcome, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, monitor_message))
    
    logger.info("Bot started with stylish messages and abuse detection...")
    logger.info(f"Monitoring for {len(abuse_words)} abuse words")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())