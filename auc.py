import os
import re
import sqlite3
import json
import html
import socket
import sys
import telegram
import threading
import time
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ForceReply
from telegram import Update, Message
from telegram import BotCommand, BotCommandScopeChat
from telegram.utils.helpers import escape_markdown
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    CallbackQueryHandler,
    Filters,
    ConversationHandler
)
from telegram.error import Conflict
from dotenv import load_dotenv
from datetime import datetime
from contextlib import contextmanager
import logging
from typing import Optional


def debug_log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] DEBUG: {message}")

SELECT_CATEGORY, GET_POKEMON_NAME, GET_NATURE, GET_IVS, GET_MOVESET, GET_BOOST_INFO, GET_BASE_PRICE, GET_TM_DETAILS = range(2, 10)

@contextmanager
def db_connection(db_name='auctions.db'):
    conn = None
    try:
        conn = sqlite3.connect(db_name)
        conn.row_factory = sqlite3.Row
        yield conn
    except Exception as e:
        debug_log(f"Database error: {str(e)}")
        raise
    finally:
        if conn:
            conn.close()

def init_db():
    try:
        with db_connection() as conn:
            c = conn.cursor()

            c.execute('''CREATE TABLE IF NOT EXISTS auctions
                          (auction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                          item_text TEXT NOT NULL,
                          photo_id TEXT,
                          base_price REAL NOT NULL,
                          current_bid REAL,
                          current_bidder_id INTEGER,
                          current_bidder TEXT,
                          previous_bidder TEXT,
                          is_active BOOLEAN DEFAULT 1,
                          auction_status TEXT DEFAULT 'active',
                          channel_message_id INTEGER,
                          discussion_message_id INTEGER,
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                          seller_id INTEGER,
                          seller_name TEXT)''')

            c.execute('''CREATE TABLE IF NOT EXISTS bids
                         (bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
                          auction_id INTEGER NOT NULL,
                          bidder_id INTEGER NOT NULL,
                          bidder_name TEXT NOT NULL,
                          amount REAL NOT NULL,
                          timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                          is_active BOOLEAN DEFAULT 1,
                          FOREIGN KEY(auction_id) REFERENCES auctions(auction_id))''')

            c.execute('''CREATE TABLE IF NOT EXISTS submissions
                         (submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER NOT NULL,
                          data TEXT NOT NULL,
                          status TEXT DEFAULT 'pending',
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                          channel_message_id INTEGER)''')

            c.execute('''CREATE TABLE IF NOT EXISTS temp_data
                         (user_id INTEGER PRIMARY KEY,
                          data TEXT,
                          timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')

            c.execute('''CREATE TABLE IF NOT EXISTS system_status
                         (id INTEGER PRIMARY KEY,
                          submissions_open BOOLEAN DEFAULT 0,
                          auctions_open BOOLEAN DEFAULT 0)''')

            c.execute('''CREATE TABLE IF NOT EXISTS verification_messages
                         (submission_id INTEGER,
                          admin_id INTEGER,
                          message_id INTEGER,
                          PRIMARY KEY (submission_id, admin_id),
                          FOREIGN KEY(submission_id) REFERENCES submissions(submission_id))''')


            c.execute("PRAGMA table_info(auctions)")
            existing_columns = [col[1] for col in c.fetchall()]

            if 'seller_id' not in existing_columns:
                c.execute("ALTER TABLE auctions ADD COLUMN seller_id INTEGER")
                debug_log("Added seller_id column to auctions table")

            if 'seller_name' not in existing_columns:
                c.execute("ALTER TABLE auctions ADD COLUMN seller_name TEXT")
                debug_log("Added seller_name column to auctions table")

            if 'auction_status' not in existing_columns:
                c.execute("ALTER TABLE auctions ADD COLUMN auction_status TEXT DEFAULT 'active'")
                debug_log("Added auction_status column to auctions table")

            c.execute('''INSERT OR IGNORE INTO system_status (id, submissions_open, auctions_open)
                         VALUES (1, 0, 0)''')

            conn.commit()
            debug_log("Database initialized successfully with all required columns")
    except Exception as e:
        debug_log(f"Database initialization failed: {str(e)}")
        raise

def init_verified_users_db():
    try:
        with db_connection('verified_users.db') as conn:
            c = conn.cursor()
            c.execute("PRAGMA foreign_keys = ON")

            tables = {
                'verified_users': '''
                    CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT NOT NULL,
                        verified_by INTEGER NOT NULL,
                        verified_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        last_active DATETIME,
                        total_submissions INTEGER DEFAULT 0,
                        total_bids INTEGER DEFAULT 0,
                        FOREIGN KEY (verified_by) REFERENCES verified_users(user_id)
                    )''',

                'verification_requests': '''
                    CREATE TABLE IF NOT EXISTS verification_requests (
                        request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER UNIQUE NOT NULL,
                        username TEXT NOT NULL,
                        request_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES verified_users(user_id) ON DELETE CASCADE
                    )''',

                'user_activity': '''
                    CREATE TABLE IF NOT EXISTS user_activity (
                        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        details TEXT,
                        FOREIGN KEY (user_id) REFERENCES verified_users(user_id) ON DELETE CASCADE
                    )'''
            }

            for table_name, schema in tables.items():
                c.execute(schema)

                c.execute(f"PRAGMA table_info({table_name})")
                existing_columns = [col[1] for col in c.fetchall()]

                if table_name == 'verified_users' and 'last_active' not in existing_columns:
                    c.execute("ALTER TABLE verified_users ADD COLUMN last_active DATETIME")
                if table_name == 'verified_users' and 'total_bids' not in existing_columns:
                    c.execute("ALTER TABLE verified_users ADD COLUMN total_bids INTEGER DEFAULT 0")

            c.execute('''CREATE INDEX IF NOT EXISTS idx_verified_users_id ON verified_users(user_id)''')
            c.execute('''CREATE INDEX IF NOT EXISTS idx_activity_user ON user_activity(user_id)''')
            c.execute('''CREATE INDEX IF NOT EXISTS idx_requests_date ON verification_requests(request_date)''')

            conn.commit()
            debug_log("Verified users database initialized")
    except Exception as e:
        debug_log(f"Verified users DB init failed: {str(e)}")
        raise

def leaderboard_connection(db_name="leaderboard.db"):
    conn = sqlite3.connect(db_name)
    conn.row_factory = sqlite3.Row
    return conn

def init_leaderboard_db():
    try:
        conn = leaderboard_connection()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS leaderboard (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT NOT NULL,
                        total_wins INTEGER DEFAULT 0,
                        total_sales INTEGER DEFAULT 0,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )''')
        conn.commit()
        conn.close()
        debug_log("Leaderboard DB initialized")
    except Exception as e:
        debug_log(f"Leaderboard DB init failed: {str(e)}")
        raise

def profile_connection(db_name='user_profiles.db'):
    try:
        conn = sqlite3.connect(db_name)
        conn.row_factory = sqlite3.Row
        debug_log(f"Connected to profile database: {db_name}")
        return conn
    except Exception as e:
        debug_log(f"Error connecting to profile database {db_name}: {str(e)}")
        raise

def init_profiles_db():
    try:
        with profile_connection() as conn:
            c = conn.cursor()

            c.execute('''CREATE TABLE IF NOT EXISTS user_profiles
                         (user_id INTEGER PRIMARY KEY,
                          username TEXT,
                          first_name TEXT,
                          total_submissions INTEGER DEFAULT 0,
                          approved_submissions INTEGER DEFAULT 0,
                          rejected_submissions INTEGER DEFAULT 0,
                          pending_submissions INTEGER DEFAULT 0,
                          revoked_submissions INTEGER DEFAULT 0,
                          is_banned BOOLEAN DEFAULT 0,
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                          updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')

            c.execute('''CREATE INDEX IF NOT EXISTS idx_profiles_user_id
                         ON user_profiles(user_id)''')

            conn.commit()
            debug_log("User profiles database initialized successfully")
    except Exception as e:
        debug_log(f"Profiles database initialization failed: {str(e)}")
        raise

load_dotenv(".env")
TOKEN = os.getenv("BOT_TOKEN", "8271241418:AAHOBL2smhLaixCUFCrR3YzKn8J9_mJsdcQ")
ADMINS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "6468620868").split(",") if admin_id]
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1003321180638"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@sjsjwhabb")
DISCUSSION_ID = int(os.getenv("DISCUSSION_ID", "-1003333433940"))
LOGS_CHANNEL_ID = int(os.getenv("LOGS_CHANNEL_ID", "-1003333433940"))

def ensure_single_instance():
    try:
        lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        lock_socket.bind('\0' + 'legendauc_bot_lock')  
        return True
    except socket.error:
        print("Error: Another bot instance is already running")
        return False

def format_html_safe(*lines, escape_all=True):
    formatted_lines = []
    for line in lines:
        if escape_all:
            line = html.escape(str(line))
        if '\n' in line:
            line = line.replace('\n', '<br>')
        formatted_lines.append(line)
    return '<br>'.join(formatted_lines)

def set_bot_commands(updater):
    user_commands = [
        BotCommand('start', 'Start the bot'),
        BotCommand('add', 'Submit new item'),
        BotCommand('help', 'Show all commands'),
        BotCommand('items', 'View auction items'),
        BotCommand('myitems', 'View your approved items'),
        BotCommand('mybids', 'View your active bids'),
        BotCommand('topsellers', 'View Top sellers'),
        BotCommand('topbuyers', 'View Top buyers'),
        BotCommand('profile', 'View your Profie'),
        BotCommand('cancel', 'Cancel adding item'),
    ]

    admin_commands = [
        BotCommand('verify', 'Verify users'),
        BotCommand('startsubmission', 'Open submissions'),
        BotCommand('endsubmission', 'Close submissions'),
        BotCommand('startauction', 'Start auctions'),
        BotCommand('endauction', 'End auctions'),
        BotCommand('removebid', 'Remove last bid'),
        BotCommand('removeitem', 'Remove the item from Auction'),
        BotCommand('broad', 'Broadcast a message'),
        BotCommand('unverify', 'Unverify a user'),
        BotCommand('msg', 'Message a speciic user'),
    ]

    try:
        updater.bot.set_my_commands(user_commands)
        for admin_id in ADMINS:
            try:
                updater.bot.set_my_commands(
                    user_commands + admin_commands,
                    scope=BotCommandScopeChat(admin_id)
                )
            except Exception as e:
                debug_log(f"Failed to set admin commands for {admin_id}: {str(e)}")
    except Exception as e:
        debug_log(f"Error setting bot commands: {str(e)}")

def show_help(update: Update, context: CallbackContext):
    is_admin = update.effective_user.id in ADMINS

    help_text = [
        "🤖 <b>Bot Commands</b> 🤖",
        "",
        "🛠 <b>General Commands:</b>",
        "/start - Start the bot",
        "/add - Submit new item",
        "/help - Show this help message",
        "/items - View auction items",
        "/myitems - View your approved items",
        "/mybids - View your active bids",
        "/topsellers - View Top Sellers",
        "/topbuyers - View Top Buyers",
        "/profile - View your profile",
    ]

    if is_admin:
        help_text.extend([
            "",
            "🔐 <b>Admin Commands:</b>",
            "/verify - Verify users",
            "/startsubmission - Open submissions",
            "/endsubmission - Close submissions",
            "/startauction - Start auctions",
            "/endauction - End auctions",
            "/removebid - Remove last bid",
            "/removeitem - Remove item from Auction",
            "/unverify - Unverify a user",
            "/broad - Broadcast a message",
            "/msg - Message to specific user",
        ])

    update.message.reply_text("\n".join(help_text), parse_mode='HTML')

def admin_only(func):
    def wrapper(update: Update, context: CallbackContext):
        if update.effective_user.id not in ADMINS:
            update.message.reply_text("🚫 Admin only command")
            return
        return func(update, context)
    return wrapper

def is_forwarded_from_hexamon(update: Update) -> bool:
    if not update.message or not update.message.forward_from:
        return False
    forward_from = update.message.forward_from
    return (forward_from.username and
            forward_from.username.lower().replace(" ", "") == "hexamonbot")

def get_min_increment(current_bid):
    if current_bid is None or current_bid == 0:
        return 1000
    try:
        current_bid = float(current_bid)
        if current_bid < 20000:
            return 1000
        elif current_bid < 40000:
            return 2000
        elif current_bid < 70000:
            return 3000
        elif current_bid < 100000:
            return 4000
        elif current_bid < 200000:
            return 5000
        elif current_bid < 400000:
            return 10000
        elif current_bid < 600000:
            return 20000
        elif current_bid < 800000:
            return 30000
        elif current_bid < 1000000:
            return 40000
        else:  
            return 50000
    except (TypeError, ValueError):
        return 1000

def format_bid_amount(amount):
    try:
        amount = float(amount)

        if amount >= 1000000:
            millions = amount / 1000000
            if millions == int(millions):
                return f"{int(millions)}M"
            else:
                formatted = f"{millions:.2f}"
                if formatted.endswith('.00'):
                    return f"{int(millions)}M"
                elif formatted.endswith('0'):
                    return f"{millions:.1f}M"
                else:
                    return f"{millions:.2f}M"

        elif amount >= 1000:
            thousands = amount / 1000
            if thousands == int(thousands):
                return f"{int(thousands)}k"
            else:
                formatted = f"{thousands:.2f}"
                if formatted.endswith('.00'):
                    return f"{int(thousands)}k"
                elif formatted.endswith('0'):
                    return f"{thousands:.1f}k"
                else:
                    return f"{thousands:.2f}k"

        else:
            return f"{int(amount):,}"

    except (ValueError, TypeError):
        return str(amount)

def parse_bid_amount(text):
    if not text:
        return None

    text = str(text).strip().lower().replace(',', '')

    try:
        if text.endswith('k'):
            number_part = text[:-1]
            return int(round(float(number_part) * 1000))

        elif text.endswith('m'):
            number_part = text[:-1]
            return int(round(float(number_part) * 1000000))

        else:
            return int(round(float(text)))

    except (ValueError, AttributeError):
        return None

def extract_base_price(text):
    try:
        if not text:
            return None

        text = text.lower().replace("base:", "").replace(",", "").strip()

        if text == "0":
            return 0

        return parse_bid_amount(text)
    except (ValueError, AttributeError):
        return None

def save_auction(item_text, photo_id, base_price, seller_id, seller_name, channel_msg_id=None):
    try:
        if not item_text or base_price is None:
            raise ValueError("Missing required fields (item_text or base_price)")

        if seller_name:
            seller_name = seller_name.replace('\\', '')

        with db_connection() as conn:
            c = conn.cursor()

            if channel_msg_id:
                c.execute('''SELECT 1 FROM auctions WHERE channel_message_id=?''', (channel_msg_id,))
                if c.fetchone():
                    debug_log("Auction with this channel message ID already exists")
                    return None

            c.execute('''INSERT INTO auctions
                        (item_text, photo_id, base_price, channel_message_id, is_active, seller_id, seller_name)
                        VALUES (?, ?, ?, ?, 1, ?, ?)''',
                    (str(item_text),
                     str(photo_id) if photo_id else None,
                     float(base_price),
                     channel_msg_id,
                     seller_id,
                     seller_name))

            auction_id = c.lastrowid
            conn.commit()
            debug_log(f"Successfully saved auction ID {auction_id}")
            return auction_id

    except Exception as e:
        debug_log(f"Critical error saving auction: {str(e)}")
        raise

def verify_auction_integrity():
    with db_connection() as conn:
        c = conn.cursor()

        c.execute('''SELECT s.submission_id
                    FROM submissions s
                    LEFT JOIN auctions a ON s.channel_message_id = a.channel_message_id
                    WHERE s.status='approved' AND a.auction_id IS NULL''')
        orphaned = c.fetchall()

        if orphaned:
            debug_log(f"Found {len(orphaned)} approved submissions without auctions")
            return False

        c.execute('''SELECT a.auction_id
                    FROM auctions a
                    LEFT JOIN submissions s ON a.channel_message_id = s.channel_message_id
                    WHERE s.submission_id IS NULL''')
        unlinked = c.fetchall()

        if unlinked:
            debug_log(f"Found {len(unlinked)} auctions without submissions")
            return False

        return True

def record_bid(auction_id, bidder_id, bidder_name, amount, context=None):
    try:
        if bidder_id not in ADMINS and not check_verification_status(bidder_id):
            debug_log(f"Unverified user {bidder_id} attempted to place bid")
            raise ValueError("User not verified")

        with db_connection() as conn:
            c = conn.cursor()

            if bidder_name and 'tg://user?id=' in bidder_name:
                bidder_parts = bidder_name.split(' ')
                if len(bidder_parts) > 1:
                    plain_bidder_name = bidder_parts[-1]  
                else:
                    plain_bidder_name = bidder_name
            else:
                plain_bidder_name = bidder_name.replace('\\', '') if bidder_name else "Unknown"

            bidder_display = f"{plain_bidder_name} ({bidder_id})" if plain_bidder_name else f"User ({bidder_id})"

            c.execute('''SELECT bidder_id, bidder_name, amount
                         FROM bids
                         WHERE auction_id=? AND is_active=1
                         ORDER BY amount DESC
                         LIMIT 1''', (auction_id,))
            prev_bidder = c.fetchone()

            c.execute('''INSERT INTO bids (auction_id, bidder_id, bidder_name, amount)
                         VALUES (?, ?, ?, ?)''',
                     (auction_id, bidder_id, plain_bidder_name, amount))

            if prev_bidder:
                previous_bidder_name = prev_bidder['bidder_name'] if prev_bidder['bidder_name'] else None
            else:
                previous_bidder_name = None

            c.execute('''UPDATE auctions SET
                         current_bid=?,
                         current_bidder_id=?,
                         previous_bidder=?,
                         current_bidder=?
                         WHERE auction_id=?''',
                      (amount, bidder_id, previous_bidder_name, bidder_display, auction_id))

            conn.commit()
            
            if context:
                try:
                    send_bid_log(context, auction_id, bidder_id, bidder_name, amount, prev_bidder)
                except Exception as e:
                    debug_log(f"Failed to send bid log: {str(e)}")
            
            return prev_bidder, auction_id

    except Exception as e:
        debug_log(f"Error in record_bid: {str(e)}")
        raise

def send_bid_log(context, auction_id, bidder_id, bidder_name, amount, previous_bid):
    try:
        if not LOGS_CHANNEL_ID:
            return

        auction = get_auction(auction_id)
        if not auction:
            return

        user = context.bot.get_chat(bidder_id)
        full_name = user.first_name
        if user.last_name:
            full_name += f" {user.last_name}"
        
        username = f"@{user.username}" if user.username else "No username"
        
        item_name = extract_item_name(auction['item_text'])
        channel_username = CHANNEL_USERNAME or f"c/{str(CHANNEL_ID).replace('-100', '')}"
        message_link = f"https://t.me/{channel_username}/{auction['channel_message_id']}"
        
        formatted_bid = format_bid_amount(amount)
        previous_amount = previous_bid['amount'] if previous_bid else auction.get('base_price', 0)
        formatted_previous = format_bid_amount(previous_amount)
        
        log_message = (
            "🪙 <b>New Bid Placed</b> 🪙\n\n"
            f"👤 <b>Bidder:</b> {html.escape(full_name)}\n"
            f"📱 <b>Username:</b> {username}\n"
            f"🆔 <b>User ID:</b> <code>{bidder_id}</code>\n\n"
            f"💰 <b>Bid Amount:</b> {formatted_bid} pd\n"
            f"📈 <b>Previous Bid:</b> {formatted_previous} pd\n\n"
            f"📦 <b>Item:</b> <a href='{message_link}'>{html.escape(item_name)}</a>\n"
            f"🏷️ <b>Auction ID:</b> #{auction_id}\n\n"
            f"⏰ <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        context.bot.send_message(
            chat_id=LOGS_CHANNEL_ID,
            text=log_message,
            parse_mode='HTML',
            disable_web_page_preview=True
        )
        
    except Exception as e:
        debug_log(f"Error sending bid log: {str(e)}")

def get_auction(auction_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM auctions WHERE auction_id=? AND auction_status='active' ''', (auction_id,))
            result = c.fetchone()

            if result:
                auction_dict = dict(result)

                defaults = {
                    'seller_id': None,
                    'seller_name': 'Unknown',
                    'auction_status': 'active'
                }

                for key, default_value in defaults.items():
                    if key not in auction_dict or auction_dict[key] is None:
                        auction_dict[key] = default_value

                return auction_dict
            return None
    except Exception as e:
        debug_log(f"Error in get_auction: {str(e)}")
        return None

def get_auction_by_channel_id_any_status(channel_message_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM auctions
                         WHERE channel_message_id=?''',
                     (channel_message_id,))
            result = c.fetchone()
            return dict(result) if result else None
    except Exception as e:
        debug_log(f"Error in get_auction_by_channel_id_any_status: {str(e)}")
        return None

def get_auction_by_channel_id(channel_message_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM auctions
                         WHERE channel_message_id = ? AND is_active = 1 AND auction_status = 'active' ''',
                     (channel_message_id,))
            result = c.fetchone()
            return dict(result) if result else None
    except Exception as e:
        debug_log(f"Error in get_auction_by_channel_id: {str(e)}")
        return None

def save_submission(user_id, data):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO submissions (user_id, data)
                         VALUES (?, ?)''',
                     (user_id, json.dumps(data)))
            submission_id = c.lastrowid
            conn.commit()
            return submission_id
    except Exception as e:
        debug_log(f"Error saving submission: {str(e)}")
        raise

def get_submission(submission_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM submissions WHERE submission_id=?''', (submission_id,))
            result = c.fetchone()
            if result:
                return {
                    'submission_id': result['submission_id'],
                    'user_id': result['user_id'],
                    'data': json.loads(result['data']),
                    'status': result['status'],
                    'created_at': result['created_at'],
                    'channel_message_id': result['channel_message_id']
                }
            return None
    except Exception as e:
        debug_log(f"Error getting submission: {str(e)}")
        return None

def save_temp_data(user_id, data):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''INSERT OR REPLACE INTO temp_data (user_id, data)
                         VALUES (?, ?)''',
                     (user_id, json.dumps(data)))
            conn.commit()
    except Exception as e:
        debug_log(f"Temp data save failed: {str(e)}")
        raise

def load_temp_data(user_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT data FROM temp_data WHERE user_id=?''', (user_id,))
            result = c.fetchone()
            return json.loads(result[0]) if result else {}
    except Exception as e:
        debug_log(f"Temp data load failed: {str(e)}")
        return {}

def cleanup_temp_data(user_id):
    try:
        with db_connection() as conn:
            conn.execute('''DELETE FROM temp_data WHERE user_id=?''', (user_id,))
            conn.commit()
    except Exception as e:
        debug_log(f"Cleanup failed: {str(e)}")

def get_user_active_bids(user_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT a.auction_id, a.item_text, b.amount
                         FROM bids b
                         JOIN auctions a ON b.auction_id = a.auction_id
                         WHERE b.bidder_id = ? AND a.auction_status = 'active' AND b.is_active = 1
                         ORDER BY b.timestamp DESC''', (user_id,))
            return c.fetchall()
    except Exception as e:
        debug_log(f"Error getting user bids: {str(e)}")
        return []

def format_auction(auction):
    try:
        auction_id = str(auction.get('auction_id', '?'))

        item_text = auction.get('item_text', 'No description available')
        item_text = item_text.replace('\\', '')

        current_bid = auction.get('current_bid')
        base_price = auction.get('base_price', 0)

        current_bid_str = format_bid_amount(current_bid) if current_bid is not None else 'None'
        base_price_str = format_bid_amount(base_price) if base_price else '0'

        current_bidder_display = auction.get('current_bidder', 'None')

        lines = [
            item_text,
            f"<blockquote>🔼 Current Bid: {current_bid_str}\n"
            f"👤 Bidder: {current_bidder_display}</blockquote>",
        ]

        return "\n".join(lines)
    except Exception as e:
        debug_log(f"Error in format_auction: {str(e)}")
        current_bid = auction.get('current_bid')
        current_bid_str = format_bid_amount(current_bid) if current_bid is not None else 'None'
        return f"Item #{auction.get('auction_id', '?')}\n\n{item_text}\n\nCurrent Bid: {current_bid_str}"

def format_pokemon_auction_item(data, auction_id=None):
    seller_id = data.get('seller_id', '')
    seller_username = data.get('seller_username', 'Unknown')
    seller_first_name = data.get('seller_first_name', 'User')

    if seller_username and seller_username != 'Unknown':
        seller_username = seller_username.replace('\\', '')
    if seller_first_name:
        seller_first_name = seller_first_name.replace('\\', '')

    category = data.get('category', '')
    if category == 'nonlegendary':
        display_category = '0L'
    elif category == 'shiny':
        display_category = 'Shiny✨'
    elif category == 'legendary':
        display_category = '6L'
    else:
        display_category = category.title()

    pokemon_name = data['pokemon_name']
    base_price = f"{data.get('base_price', 0):,}"

    nature_text = data['nature'].get('text', '')
    level = "Unknown"
    nature = "Unknown"

    level_match = re.search(r'Lv\.\s*(\d+)', nature_text)
    nature_match = re.search(r'Nature:\s*([A-Za-z]+)', nature_text)

    if level_match:
        level = level_match.group(1)
    if nature_match:
        nature = nature_match.group(1)

    ivs_text = data['ivs'].get('text', '')

    moveset_text = data['moveset'].get('text', '')

    boost_info = data.get('boost_info', 'Boost information not provided')

    if seller_username and seller_username != 'Unknown':
        seller_display = f"@{seller_username}"
    else:
        seller_display = seller_first_name

    lines = [
        f"<blockquote><b>#{display_category}</b></blockquote>"
    ]

    if auction_id:
        lines.insert(0, f"<blockquote><b>Item #{auction_id}</b></blockquote>")

    lines.extend([
        f"<blockquote>Pokémon: {pokemon_name}\n"
        f"Level: {level}\n"
        f"Nature: {nature}</blockquote>",
        "",
        ivs_text,
        "",
        moveset_text,
        "",
        f"<blockquote> Boosted: {boost_info}\n"
        f" Seller: {seller_display}\n"
        f" Seller ID: {seller_id}\n"
        f" Base Price: {base_price}</blockquote>"
    ])

    return "\n".join(lines)

def format_tm_auction_item(data, auction_id=None):
    seller_id = data.get('seller_id', '')
    seller_username = data.get('seller_username', 'Unknown')
    seller_first_name = data.get('seller_first_name', 'User')

    if seller_username and seller_username != 'Unknown':
        seller_username = seller_username.replace('\\', '')
    if seller_first_name:
        seller_first_name = seller_first_name.replace('\\', '')

    tm_details_text = data.get('tm_details', {}).get('text', 'TM details not available')
    tm_details_text = tm_details_text.replace('\\', '')  

    lines = tm_details_text.split('\n')
    cleaned_lines = []

    for line in lines:
        if re.search(r'you can sell this tm', line, re.IGNORECASE):
            continue
        if not cleaned_lines and not line.strip():
            continue
        cleaned_lines.append(line)

    cleaned_text = '\n'.join(cleaned_lines).strip()

    base_price = f"{data.get('base_price', 0):,}"

    if seller_username and seller_username != 'Unknown':
        seller_display = f"@{seller_username}"
    else:
        seller_display = seller_first_name

    lines = []

    if auction_id:
        lines.append(f"<blockquote><b>Item #{auction_id}</b></blockquote>")

    lines.extend([
        f"<blockquote><b>#TMs</b></blockquote>",
        "",
        cleaned_text,
        "",
        f"<blockquote>👤 Seller: {seller_display}\n"
        f"🆔 Seller ID: {seller_id}\n"
        f"💰 Base Price: {base_price}</blockquote>"
    ])

    return "\n".join(lines)

def increment_win(user_id, username):
    try:
        conn = leaderboard_connection()
        c = conn.cursor()
        c.execute('''INSERT INTO leaderboard (user_id, username, total_wins)
                     VALUES (?, ?, 1)
                     ON CONFLICT(user_id) DO UPDATE SET
                     total_wins = total_wins + 1,
                     username = excluded.username,
                     updated_at = CURRENT_TIMESTAMP''',
                  (user_id, username or "Unknown"))
        conn.commit()
        conn.close()
    except Exception as e:
        debug_log(f"Error incrementing win: {str(e)}")

def increment_sale(user_id, username):
    try:
        conn = leaderboard_connection()
        c = conn.cursor()
        c.execute('''INSERT INTO leaderboard (user_id, username, total_sales)
                     VALUES (?, ?, 1)
                     ON CONFLICT(user_id) DO UPDATE SET
                     total_sales = total_sales + 1,
                     username = excluded.username,
                     updated_at = CURRENT_TIMESTAMP''',
                  (user_id, username or "Unknown"))
        conn.commit()
        conn.close()
    except Exception as e:
        debug_log(f"Error incrementing sale: {str(e)}")

def get_top_buyers(limit=5):
    try:
        conn = leaderboard_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, username, total_wins FROM leaderboard WHERE total_wins > 0 ORDER BY total_wins DESC, updated_at ASC LIMIT ?", (limit,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        debug_log(f"Error fetching top buyers: {str(e)}")
        return []

def get_top_sellers(limit=5):
    try:
        conn = leaderboard_connection()
        c = conn.cursor()
        c.execute("SELECT user_id, username, total_sales FROM leaderboard WHERE total_sales > 0 ORDER BY total_sales DESC, updated_at ASC LIMIT ?", (limit,))
        rows = c.fetchall()
        conn.close()
        return rows
    except Exception as e:
        debug_log(f"Error fetching top sellers: {str(e)}")
        return []

def start(update: Update, context: CallbackContext):
    if update.message.chat.type != "private":
        update.message.reply_text("❌ Please use this bot in private messages (DM) only!")
        return

    if context.args and context.args[0].startswith('bid_'):
        try:
            user_id = update.effective_user.id
            if user_id not in ADMINS and not check_verification_status(user_id):
                update.message.reply_text(
                    "🔒 Verification Required\n\n"
                    "Contact an admin for verification.\n"
                )
                return

            auction_id = int(context.args[0].split('_')[1])
            auction = get_auction(auction_id)

            if not auction:
                update.message.reply_text("❌ Auction not found!")
                return


            current_amount = auction.get('current_bid') or auction.get('base_price', 0)
            min_bid = current_amount + get_min_increment(current_amount)

            context.user_data['bid_context'] = {
                'auction_id': auction_id,
                'channel_msg_id': auction['channel_message_id'],
                'min_bid': min_bid,
                'current_bidder': auction.get('current_bidder'),
                'item_text': auction['item_text']
            }

            update.message.reply_text(
                f"Item #{auction_id}\n\n"
                f"Current Bid: {current_amount:,}\n"
                f"Minimum Bid: {min_bid:,}\n\n"
                "Please enter your bid amount:"
            )
            return
        except Exception as e:
            debug_log(f"Error in deep link handling: {str(e)}")

    if update.effective_user.id not in ADMINS and not check_verification_status(update.effective_user.id):
        update.message.reply_text(
            "🔒 Verification Required\n\n"
            "Before using this bot, please contact an admin for verification.\n"
        )
        return

    with db_connection() as conn:
        status = conn.execute("SELECT submissions_open, auctions_open FROM system_status WHERE id=1").fetchone()

    submissions_open = "🔓 OPEN" if status[0] else "🔒 CLOSED"
    auctions_open = "🔓 OPEN" if status[1] else "🔒 CLOSED"

    response = [
        "🏪 Pokemart Bot 🏪",
        "",
        f"🎒 Item Submissions: {submissions_open}",
        f"💸 Auctions: {auctions_open}",
        "",
        "👋 Welcome to the Pokemart Bot",
        "",
        "👉 Also Join :-",
        "🌟 Channel :- @pokesforsell",
        "🌟 Trade GC :- @pokerookies",
        "",
        "💡 Use /help to see commands",
    ]

    try:
        photo_file_id = "AgACAgUAAyEFAASmmkB5AAIPb2kB1zosvYrdb1aAJno16UE0f4__AALlC2sbgIAIVMJLQGx7qL4rAQADAgADeQADNgQ"  

        update.message.reply_photo(
            photo=photo_file_id,
            caption="\n".join(response).strip(),
            parse_mode='HTML'
        )
    except Exception as e:
        debug_log(f"Error sending photo: {str(e)}")
        update.message.reply_text("\n".join(response).strip())

@admin_only
def end_submission(update: Update, context: CallbackContext):
    with db_connection() as conn:
        conn.execute("UPDATE system_status SET submissions_open=0 WHERE id=1")
        conn.commit()
    update.message.reply_text("✅ Item submissions are now CLOSED")

@admin_only
def start_submission(update: Update, context: CallbackContext):
    with db_connection() as conn:
        conn.execute("UPDATE system_status SET submissions_open=1 WHERE id=1")
        conn.commit()
    update.message.reply_text("✅ Item submissions are now OPEN")

@admin_only
def start_auction(update: Update, context: CallbackContext):
    with db_connection() as conn:
        conn.execute("UPDATE system_status SET auctions_open=1 WHERE id=1")
        conn.commit()
    update.message.reply_text("✅ Auctions are now OPEN")

def send_win_notifications(context):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT a.auction_id, a.item_text, a.channel_message_id, 
                                b.bidder_id, b.bidder_name, b.amount
                         FROM auctions a
                         JOIN bids b ON a.auction_id = b.auction_id
                         WHERE a.auction_status = 'active'
                         AND b.amount = (
                             SELECT MAX(amount) 
                             FROM bids 
                             WHERE auction_id = a.auction_id AND is_active = 1
                         )
                         AND b.is_active = 1''')
            winning_bids = c.fetchall()

        try:
            channel_entity = context.bot.get_chat(CHANNEL_ID)
            channel_username = channel_entity.username
            if not channel_username:
                channel_username = f"c/{str(CHANNEL_ID).replace('-100', '')}"
        except:
            channel_username = None

        notifications_sent = 0
        notifications_failed = 0

        for bid in winning_bids:
            auction_id, item_text, channel_msg_id, bidder_id, bidder_name, amount = bid
            
            item_name = extract_item_name(item_text)
            
            if channel_username and channel_msg_id:
                message_link = f"https://t.me/{channel_username}/{channel_msg_id}"
                item_display = f'<a href="{message_link}">{html.escape(item_name)}</a>'
            else:
                item_display = html.escape(item_name)

            formatted_bid = format_bid_amount(amount)

            message = (
                f"<b>You have won the bid!</b>\n\n"
                f"💫Item: {item_display}\n"
                f"🤑Your Bid: {formatted_bid} pd"
            )

            try:
                context.bot.send_message(
                    chat_id=bidder_id,
                    text=message,
                    parse_mode='HTML',
                    disable_web_page_preview=False
                )
                notifications_sent += 1
                debug_log(f"Sent win notification to user {bidder_id} for auction {auction_id}")
                
                time.sleep(0.2)
                
            except telegram.error.Unauthorized:
                debug_log(f"User {bidder_id} blocked the bot - cannot send win notification")
                notifications_failed += 1
            except Exception as e:
                debug_log(f"Failed to send win notification to user {bidder_id}: {str(e)}")
                notifications_failed += 1

        debug_log(f"Win notifications: {notifications_sent} sent, {notifications_failed} failed")
        return notifications_sent, notifications_failed

    except Exception as e:
        debug_log(f"Error in send_win_notifications: {str(e)}")
        return 0, 0

@admin_only
def end_auction(update: Update, context: CallbackContext):
    try:
        with db_connection() as conn:
            conn.execute("UPDATE system_status SET auctions_open=0 WHERE id=1")
            conn.commit()

        update.message.reply_text("📤 Sending win notifications to highest bidders...")
        notifications_sent, notifications_failed = send_win_notifications(context)

        with db_connection() as conn:
            active_auctions = conn.execute(
                "SELECT * FROM auctions WHERE auction_status = 'active'"
            ).fetchall()

        updated_buyers = 0
        updated_sellers = 0

        for auction in active_auctions:
            with db_connection() as conn:
                winner = conn.execute(
                    "SELECT bidder_id, bidder_name FROM bids WHERE auction_id = ? AND is_active = 1 ORDER BY amount DESC LIMIT 1",
                    (auction["auction_id"],)
                ).fetchone()

            if winner and winner["bidder_id"]:
                winner_id = winner["bidder_id"]
                winner_username = winner["bidder_name"] or f"User_{winner_id}"

                seller_id = auction["seller_id"]
                seller_username = auction["seller_name"] or f"User_{seller_id}" if seller_id else "Unknown"

                try:
                    increment_win(winner_id, winner_username)
                    updated_buyers += 1
                except Exception as e:
                    debug_log(f"Failed to update buyer leaderboard for user {winner_id}: {str(e)}")

                if seller_id:
                    try:
                        increment_sale(seller_id, seller_username)
                        updated_sellers += 1
                    except Exception as e:
                        debug_log(f"Failed to update seller leaderboard for user {seller_id}: {str(e)}")

        removed_buttons_count = remove_bid_buttons_from_all_auctions(context)

        response = (
            "✅ Auction bidding is now CLOSED\n\n"
            f"📨 Notifications: {notifications_sent} sent, {notifications_failed} failed\n"
            f"👥 Leaderboard: {updated_buyers} buyers, {updated_sellers} sellers updated\n"
            f"🔒 Buttons removed from: {removed_buttons_count} auctions"
        )

        update.message.reply_text(response)

    except Exception as e:
        debug_log(f"Error in end_auction: {str(e)}")
        update.message.reply_text("❌ Error closing auctions. Check logs.")

def remove_bid_buttons_from_all_auctions(context):
    try:
        with db_connection() as conn:
            active_auctions = conn.execute(
                "SELECT auction_id, channel_message_id FROM auctions WHERE auction_status = 'active'"
            ).fetchall()

        removed_count = 0
        
        for auction in active_auctions:
            try:
                if auction['channel_message_id']:
                    context.bot.edit_message_reply_markup(
                        chat_id=CHANNEL_ID,
                        message_id=auction['channel_message_id'],
                        reply_markup=None
                    )
                    removed_count += 1
            except telegram.error.BadRequest as e:
                if "Message is not modified" in str(e):
                    removed_count += 1
                elif "message to edit not found" in str(e):
                    debug_log(f"Message {auction['channel_message_id']} not found for auction {auction['auction_id']}")
                else:
                    debug_log(f"Couldn't remove buttons from auction {auction['auction_id']}: {str(e)}")
            except Exception as e:
                debug_log(f"Error removing buttons from auction {auction['auction_id']}: {str(e)}")

        return removed_count
        
    except Exception as e:
        debug_log(f"Error in remove_bid_buttons_from_all_auctions: {str(e)}")
        return 0

def ensure_all_auctions_active():
    try:
        with db_connection() as conn:
            conn.execute("UPDATE auctions SET auction_status = 'active' WHERE auction_status = 'ended'")
            count = conn.cursor().rowcount
            if count > 0:
                debug_log(f"Reset {count} ended auctions back to active status")
            conn.commit()
    except Exception as e:
        debug_log(f"Error ensuring auctions are active: {str(e)}")

@admin_only
def verify_user(update: Update, context: CallbackContext):
    if not update.message.reply_to_message:
        update.message.reply_text("❌ Please reply to a user's message with /verify")
        return

    target_user = update.message.reply_to_message.from_user

    try:
        with db_connection('verified_users.db') as conn:
            c = conn.cursor()

            c.execute('SELECT 1 FROM verified_users WHERE user_id=?', (target_user.id,))
            if c.fetchone():
                update.message.reply_text("⚠️ User is already verified")
                return

            c.execute('''INSERT INTO verified_users
                        (user_id, username, verified_by)
                        VALUES (?, ?, ?)''',
                    (target_user.id,
                     target_user.username or target_user.first_name,
                     update.effective_user.id))

            conn.commit()

            try:
                context.bot.send_message(
                    target_user.id,
                    "✅ Verification Approved!\n\n"
                    "You can now access all bot features.\n"
                    "Please /start the bot again to refresh your status."
                )
            except telegram.error.BadRequest as e:
                if "chat not found" in str(e).lower():
                    debug_log(f"User {target_user.id} has not started the bot or blocked it")
                else:
                    debug_log(f"Failed to send verification message to user {target_user.id}: {str(e)}")
            except Exception as e:
                debug_log(f"Error sending verification message: {str(e)}")

            update.message.reply_text(f"✅ Verified @{target_user.username or target_user.id}")

    except Exception as e:
        debug_log(f"Verification error: {str(e)}")
        update.message.reply_text("❌ Failed to verify user")

def request_verification(update: Update, context: CallbackContext):
    user = update.effective_user

    try:
        with db_connection('verified_users.db') as conn:
            c = conn.cursor()

            c.execute('SELECT 1 FROM verified_users WHERE user_id=?', (user.id,))
            if c.fetchone():
                update.message.reply_text("✅ You're already verified!")
                return

            c.execute('SELECT 1 FROM verification_requests WHERE user_id=?', (user.id,))
            if c.fetchone():
                update.message.reply_text("⏳ Your verification request is pending. Please wait for admin approval.")
                return

            c.execute('''INSERT INTO verification_requests
                        (user_id, username)
                        VALUES (?, ?)''',
                    (user.id, user.username or user.first_name))
            conn.commit()

            for admin_id in ADMINS:
                try:
                    context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🆕 Verification Request\n\n"
                             f"User: @{user.username or user.first_name} (ID: {user.id})\n"
                             f"Requested at: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                             f"Reply to this user's message with /verify to approve"
                    )
                except Exception as e:
                    debug_log(f"Failed to notify admin {admin_id}: {str(e)}")

            update.message.reply_text(
                "📨 Verification request sent to admins!\n"
                "You'll be notified once approved."
            )

    except Exception as e:
        debug_log(f"Verification request error: {str(e)}")
        update.message.reply_text("❌ Failed to process verification request")

@admin_only
def list_verified_users(update: Update, context: CallbackContext):
    try:
        with db_connection('verified_users.db') as conn:
            total_users = conn.execute('SELECT COUNT(*) FROM verified_users').fetchone()[0]
            
            if total_users == 0:
                update.message.reply_text("No verified users found.")
                return

            total_pages = (total_users + 19) // 20  
            
            context.user_data['verified_users_pagination'] = {
                'total_pages': total_pages,
                'current_page': 1,
                'total_users': total_users
            }
            
            display_verified_users_page(update, context, page=1)
            
    except Exception as e:
        debug_log(f"Error listing users: {str(e)}")
        update.message.reply_text("❌ Error fetching user list")

def display_verified_users_page(update, context, page):
    try:
        with db_connection('verified_users.db') as conn:
            offset = (page - 1) * 20
            
            users = conn.execute('''SELECT user_id, username, verified_at 
                                   FROM verified_users 
                                   ORDER BY verified_at DESC 
                                   LIMIT 20 OFFSET ?''', (offset,)).fetchall()

            if not users:
                if update.callback_query:
                    update.callback_query.edit_message_text("❌ No users found for this page.")
                else:
                    update.message.reply_text("❌ No users found for this page.")
                return

            response_lines = [f"✅ <b>Verified Users - Page {page}/{context.user_data['verified_users_pagination']['total_pages']}</b>\n"]
            response_lines.append(f"📊 Total Users: {context.user_data['verified_users_pagination']['total_users']}\n")

            for i, user in enumerate(users, offset + 1):
                user_id, username, verified_at = user
                username = username or f"User_{user_id}"
                
                response_lines.extend([
                    f"\n{i}. 👤 @{username}",
                    f"   🆔 ID: <code>{user_id}</code>",
                    f"   📅 Verified: {verified_at}"
                ])

            message_text = "\n".join(response_lines)
            
            keyboard = create_pagination_buttons(page, context.user_data['verified_users_pagination']['total_pages'])
            
            if update.callback_query:
                update.callback_query.edit_message_text(
                    text=message_text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                update.message.reply_text(
                    text=message_text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

    except Exception as e:
        debug_log(f"Error displaying user page: {str(e)}")
        error_msg = "❌ Error displaying users"
        if update.callback_query:
            try:
                update.callback_query.edit_message_text(error_msg)
            except:
                pass
        else:
            update.message.reply_text(error_msg)

def create_pagination_buttons(current_page, total_pages):
    keyboard = []
    
    if total_pages > 1:
        row = []
        
        if current_page > 1:
            row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"verified_prev_{current_page-1}"))
        
        if current_page < total_pages:
            row.append(InlineKeyboardButton("Next ➡️", callback_data=f"verified_next_{current_page+1}"))
        
        if row:  
            keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="verified_close")])
    
    return keyboard

def handle_verified_pagination(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    data = query.data
    
    if data == "verified_close":
        query.delete_message()
        return
    
    try:
        if data.startswith("verified_prev_"):
            page = int(data.split("_")[2])
        elif data.startswith("verified_next_"):
            page = int(data.split("_")[2])
        else:
            return
        
        if 'verified_users_pagination' not in context.user_data:
            try:
                with db_connection('verified_users.db') as conn:
                    total_users = conn.execute('SELECT COUNT(*) FROM verified_users').fetchone()[0]
                    total_pages = (total_users + 19) // 20
                    context.user_data['verified_users_pagination'] = {
                        'total_pages': total_pages,
                        'current_page': page,
                        'total_users': total_users
                    }
            except Exception as e:
                debug_log(f"Error recreating pagination data: {str(e)}")
                query.edit_message_text("❌ Session expired. Use /listverified again.")
                return
        else:
            context.user_data['verified_users_pagination']['current_page'] = page
        
        display_verified_users_page(update, context, page)
        
    except Exception as e:
        debug_log(f"Error handling pagination: {str(e)}")
        try:
            query.edit_message_text("❌ Error navigating pages. Use /listverified again.")
        except:
            pass

@admin_only
def remove_verification(update: Update, context: CallbackContext):
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        user_id = target_user.id
        username = target_user.username or target_user.first_name
    elif context.args:
        try:
            user_id = int(context.args[0])
            username = f"User_{user_id}"  
        except ValueError:
            update.message.reply_text("❌ Invalid user ID. Please provide a numeric user ID.")
            return
    else:
        update.message.reply_text(
            "❌ Usage: \n"
            "• /unverify <user_id>\n"
            "• Or reply to a user's message with /unverify"
        )
        return

    try:
        with db_connection('verified_users.db') as conn:
            c = conn.cursor()
            c.execute('SELECT username FROM verified_users WHERE user_id=?', (user_id,))
            user_data = c.fetchone()

            if not user_data:
                update.message.reply_text(f"❌ User {user_id} is not verified!")
                return

            db_username = user_data['username'] if user_data else f"User_{user_id}"

            conn.execute("DELETE FROM verified_users WHERE user_id=?", (user_id,))

            conn.execute("DELETE FROM verification_requests WHERE user_id=?", (user_id,))
            conn.commit()

        update.message.reply_text(f"✅ Verification removed for user: {db_username} (ID: {user_id})")

        try:
            context.bot.send_message(
                user_id,
                "⚠️ Your verification status has been removed by admin.\n\n"
                "You'll need to get verified again to use bot features.\n"
            )
        except telegram.error.BadRequest as e:
            if "chat not found" in str(e).lower():
                debug_log(f"User {user_id} has not started the bot or blocked it")
            else:
                debug_log(f"Failed to send unverification message to user {user_id}: {str(e)}")
        except Exception as e:
            debug_log(f"Error sending unverification message: {str(e)}")

    except Exception as e:
        debug_log(f"Error removing verification: {str(e)}")
        update.message.reply_text("❌ Failed to remove verification. Check logs for details.")

def check_verification_status(user_id):
    try:
        with db_connection('verified_users.db') as conn:
            return conn.execute('''SELECT 1 FROM verified_users
                                 WHERE user_id=?''', (user_id,)).fetchone() is not None
    except Exception as e:
        debug_log(f"Verification check error: {str(e)}")
        return False

def verified_only(func):
    def wrapper(update: Update, context: CallbackContext):
        user = update.effective_user

        if user.id in ADMINS:
            return func(update, context)

        try:
            with db_connection('verified_users.db') as conn:
                c = conn.cursor()

                c.execute('''SELECT user_id FROM verified_users
                            WHERE user_id=?''',
                         (user.id,))
                is_verified = c.fetchone()

                if not is_verified:
                    update.message.reply_text(
                        "🔒 Verification Required\n\n"
                        "Please contact an admin to get verified first.\n"
                    )
                    return

                try:
                    c.execute('''UPDATE verified_users SET
                                last_active=CURRENT_TIMESTAMP,
                                username=?
                                WHERE user_id=?''',
                             (user.username or user.first_name, user.id))
                    conn.commit()
                except sqlite3.OperationalError as e:
                    debug_log(f"Optional columns not available: {str(e)}")
                    conn.rollback()

                return func(update, context)

        except Exception as e:
            debug_log(f"Verification check failed: {str(e)}")
            update.message.reply_text(
                "⚠️ Temporary verification error\n"
                "Admins have been notified. Please try again later."
            )
            return
    return wrapper

def cleanup_verification_requests():
    try:
        with db_connection('verified_users.db') as conn:
            conn.execute('''DELETE FROM verification_requests
                           WHERE request_date < datetime('now', '-30 days')''')
            conn.commit()
            debug_log("Cleaned up old verification requests")
    except Exception as e:
        debug_log(f"Verification cleanup failed: {str(e)}")

def check_system_status(status_type):
    def decorator(func):
        def wrapper(update: Update, context: CallbackContext):
            with db_connection() as conn:
                status = conn.execute(f"SELECT {status_type} FROM system_status WHERE id=1").fetchone()[0]

                if not status:
                    if status_type == "submissions_open":
                        update.message.reply_text("❌ Item submissions are currently closed.")
                    elif status_type == "auctions_open":
                        update.message.reply_text("❌ Auctions are currently closed.")
                    else:
                        update.message.reply_text("❌ This feature is currently disabled.")
                    return
                return func(update, context)
        return wrapper
    return decorator

@admin_only
def broadcast_message(update: Update, context: CallbackContext):
    if not update.message.reply_to_message:
        update.message.reply_text("❌ Please reply to a message with /broad to broadcast it")
        return

    message_to_broadcast = update.message.reply_to_message
    admin = update.effective_user
    total_sent = 0
    total_failed = 0

    update.message.reply_text("📤 Starting broadcast...")

    try:
        with db_connection('verified_users.db') as conn:
            users = conn.execute('SELECT user_id FROM verified_users').fetchall()
        
        total_users = len(users)

        send_broadcast_start_log(context, admin, message_to_broadcast, total_users)

        for user in users:
            user_id = user['user_id']
            try:
                context.bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message_to_broadcast.chat.id,
                    message_id=message_to_broadcast.message_id
                )

                total_sent += 1

                time.sleep(0.1)

            except telegram.error.BadRequest as e:
                error_msg = str(e).lower()
                if "chat not found" in error_msg:
                    debug_log(f"User {user_id} has not started the bot")
                elif "bot was blocked" in error_msg:
                    debug_log(f"User {user_id} blocked the bot")
                else:
                    debug_log(f"Failed to send broadcast to user {user_id}: {str(e)}")
                total_failed += 1
            except Exception as e:
                debug_log(f"Error sending to user {user_id}: {str(e)}")
                total_failed += 1

        send_broadcast_completion_log(context, admin, total_sent, total_failed, total_users)

        update.message.reply_text(
            f"📊 Broadcast Complete!\n\n"
            f"✅ Successfully sent to: {total_sent} users\n"
            f"❌ Failed to send to: {total_failed} users\n"
            f"👥 Total users: {total_users}\n\n"
            f"📝 Check logs channel for detailed report."
        )

    except Exception as e:
        debug_log(f"Broadcast error: {str(e)}")
        update.message.reply_text("❌ Error during broadcast. Check logs.")

def detect_all_formatting(message):
    text = message.text or message.caption or ""
    formats = []
    
    if '<b>' in text or '**' in text:
        formats.append("Bold")
    if '<i>' in text or '__' in text:
        formats.append("Italic")
    if '<code>' in text or '`' in text:
        formats.append("Monospace")
    if '<pre>' in text or '```' in text:
        formats.append("Code Block")
    if '<u>' in text:
        formats.append("Underline")
    if '<s>' in text or '~~' in text:
        formats.append("Strikethrough")
    if '<tg-spoiler>' in text or '||' in text:
        formats.append("Spoiler")
    if '<blockquote>' in text or '&gt;' in text:
        formats.append("Quote")
    if '<a href=' in text:
        formats.append("Links")
    
    return formats if formats else ["Plain text"]

def get_detailed_content_preview(message, max_length=200):
    content = ""
    
    if message.text:
        content = message.text
    elif message.caption:
        content = message.caption
    else:
        content = f"[{get_message_type(message)}]"
    
    formatting_indicators = ""
    if message.text or message.caption:
        formats = detect_all_formatting(message)
        formatting_indicators = " | ".join(formats)
    
    content = html.escape(content)
    if len(content) > max_length:
        content = content[:max_length] + "..."
    
    if formatting_indicators:
        return f"{content}\n🎨 Formatting: {formatting_indicators}"
    else:
        return content

def send_broadcast_start_log(context, admin, message, total_users):
    try:
        if not LOGS_CHANNEL_ID:
            return

        admin_name = admin.first_name
        if admin.last_name:
            admin_name += f" {admin.last_name}"
        admin_username = f"@{admin.username}" if admin.username else "No username"
        
        message_type = get_message_type(message)
        content_preview = get_detailed_content_preview(message)
        formatting_types = detect_all_formatting(message)
        formatting_display = ", ".join(formatting_types) if formatting_types else "Plain text"
        
        is_forwarded = "Yes" if (message.forward_from or message.forward_from_chat) else "No"
        is_quoted = "Yes" if message.reply_to_message else "No"
        
        forward_source = ""
        if message.forward_from:
            forward_source = f"User: {message.forward_from.first_name}"
            if message.forward_from.username:
                forward_source += f" (@{message.forward_from.username})"
        elif message.forward_from_chat:
            forward_source = f"Channel: {message.forward_from_chat.title}"
        
        quote_info = ""
        if message.reply_to_message:
            quoted_msg = message.reply_to_message
            quoted_sender = quoted_msg.from_user
            if quoted_sender:
                quote_info = f"Quoting: {quoted_sender.first_name}"
                if quoted_sender.username:
                    quote_info += f" (@{quoted_sender.username})"

        log_message = (
            "📢 <b>Broadcast Started</b> 📢\n\n"
            f"👨‍💼 <b>Admin:</b> {html.escape(admin_name)}\n"
            f"📱 <b>Username:</b> {admin_username}\n"
            f"🆔 <b>Admin ID:</b> <code>{admin.id}</code>\n\n"
            f"📄 <b>Message Type:</b> {message_type}\n"
            f"🎨 <b>Formatting:</b> {formatting_display}\n"
            f"🔄 <b>Forwarded:</b> {is_forwarded}\n"
            f"↪️ <b>Quoted:</b> {is_quoted}\n"
            f"📡 <b>Source:</b> {forward_source if forward_source else 'Original'}\n"
            f"📝 <b>Content Preview:</b>\n<code>{content_preview}</code>\n\n"
            f"👥 <b>Target Audience:</b> {total_users} verified users\n"
            f"⏰ <b>Started at:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"<i>🚀 Broadcast in progress...</i>"
        )
        
        context.bot.send_message(
            chat_id=LOGS_CHANNEL_ID,
            text=log_message,
            parse_mode='HTML'
        )
        
    except Exception as e:
        debug_log(f"Error sending broadcast start log: {str(e)}")

def send_broadcast_completion_log(context, admin, sent, failed, total_users):
    try:
        if not LOGS_CHANNEL_ID:
            return

        success_rate = (sent / total_users) * 100 if total_users > 0 else 0
        
        log_message = (
            "✅ <b>Broadcast Completed</b> ✅\n\n"
            f"📊 <b>Delivery Statistics:</b>\n"
            f"   ✅ Success: {sent} users\n"
            f"   ❌ Failed: {failed} users\n"
            f"   👥 Total: {total_users} users\n"
            f"   📈 Success Rate: {success_rate:.1f}%\n\n"
            f"⏰ <b>Completed at:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"<i>🎯 Broadcast finished</i>"
        )
        
        context.bot.send_message(
            chat_id=LOGS_CHANNEL_ID,
            text=log_message,
            parse_mode='HTML'
        )
        
    except Exception as e:
        debug_log(f"Error sending broadcast completion log: {str(e)}")

def get_message_type(message):
    if message.text:
        return "Text"
    elif message.photo:
        return "Photo"
    elif message.video:
        return "Video"
    elif message.document:
        return "Document"
    elif message.audio:
        return "Audio"
    elif message.voice:
        return "Voice"
    elif message.sticker:
        return "Sticker"
    elif message.video_note:
        return "Video Note"
    elif message.animation:
        return "GIF Animation"
    elif message.contact:
        return "Contact"
    elif message.location:
        return "Location"
    elif message.poll:
        return "Poll"
    else:
        return "Unknown"

def get_content_preview(message, max_length=100):
    content = ""
    
    if message.text:
        content = message.text
    elif message.caption:
        content = message.caption
    else:
        content = f"[{get_message_type(message)}]"
    
    content = html.escape(content)
    if len(content) > max_length:
        content = content[:max_length] + "..."
    
    return content if content else "No content"

@verified_only
@check_system_status("submissions_open")
def start_add(update: Update, context: CallbackContext):
    if update.message.chat.type != "private":
        update.message.reply_text("❌ Please DM me to add items!")
        return ConversationHandler.END

    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("Legendary", callback_data="cat_legendary")],
        [InlineKeyboardButton("Non-Legendary", callback_data="cat_nonlegendary")],
        [InlineKeyboardButton("Shiny", callback_data="cat_shiny")],
        [InlineKeyboardButton("TMs", callback_data="cat_tms")]
    ]
    update.message.reply_text(
        "📝 Select category for your item:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_CATEGORY

def handle_category(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    category = query.data.split("_")[1]
    context.user_data['category'] = category

    if category == 'tms':
        query.edit_message_text(
            "📝 Please forward the TM details from @HexaMonBot\n"
            "(This should include all TM information)"
        )
        return GET_TM_DETAILS
    else:
        query.edit_message_text("🔤 Please enter the Pokémon's name:")
        return GET_POKEMON_NAME

def is_tm_message(message: Message) -> bool:
    if not message:
        return False
    text = message.text or message.caption or ""
    return any(indicator in text for indicator in ['💿', 'TM:', 'Technical Machine'])

def handle_tm_details(update: Update, context: CallbackContext):
    try:
        if not update.message or not update.message.forward_from:
            update.message.reply_text("❌ Please forward the original message from @HexaMonBot")
            return GET_TM_DETAILS

        if update.message.forward_from.username.lower() != "hexamonbot":
            update.message.reply_text("❌ Please forward directly from @HexaMonBot")
            return GET_TM_DETAILS

        tm_text = update.message.text or update.message.caption or ""
        if not tm_text.strip():
            update.message.reply_text("❌ No TM details found in the message")
            return GET_TM_DETAILS

        context.user_data['tm_details'] = {'text': tm_text}

        update.message.reply_text(
            "💰 Please enter the base price for this TM"
        )
        return GET_BASE_PRICE

    except Exception as e:
        debug_log(f"Error in handle_tm_details: {str(e)}")
        update.message.reply_text("❌ Failed to process TM details. Please try /add again.")
        return ConversationHandler.END

def handle_pokemon_name(update: Update, context: CallbackContext):
    pokemon_name = update.message.text.strip()
    if not pokemon_name or len(pokemon_name) > 30:
        update.message.reply_text("❌ Invalid name! Please enter a valid Pokémon name (max 30 chars)")
        return GET_POKEMON_NAME

    context.user_data['pokemon_name'] = pokemon_name
    context.user_data['seller_id'] = update.effective_user.id
    save_temp_data(update.effective_user.id, context.user_data)
    update.message.reply_text(
        f"🌿 Now forward {pokemon_name}'s Nature page from @HexaMonBot"
    )
    return GET_NATURE

def handle_nature(update: Update, context: CallbackContext):
    if not is_forwarded_from_hexamon(update):
        update.message.reply_text("❌ Please forward directly from @HexaMonBot!")
        return GET_NATURE

    try:
        context.user_data['nature'] = {
            'photo': update.message.photo[-1].file_id,
            'text': update.message.caption or "Nature details not available"
        }
        save_temp_data(update.effective_user.id, context.user_data)
        update.message.reply_text("📊 Now forward IVs/EVs page from @HexaMonBot")
        return GET_IVS
    except Exception as e:
        debug_log(f"Nature handling failed: {str(e)}")
        update.message.reply_text("❌ Error saving nature data. Please restart with /add")
        return ConversationHandler.END

def handle_ivs(update: Update, context: CallbackContext):
    if not is_forwarded_from_hexamon(update):
        update.message.reply_text(
            "❌ Invalid IV/EV page!\n"
            "Please forward the original message directly from @HexaMonBot"
        )
        return GET_IVS

    if not update.message.photo:
        update.message.reply_text("❌ No IV/EV photo detected!")
        return GET_IVS

    context.user_data['ivs'] = {
        'photo': update.message.photo[-1].file_id,
        'text': update.message.caption or "No IV/EV details provided"
    }
    save_temp_data(update.effective_user.id, context.user_data)
    update.message.reply_text("⚔️ Now forward the Moveset page from @HexaMonBot")
    return GET_MOVESET

def handle_moveset(update: Update, context: CallbackContext):
    if not is_forwarded_from_hexamon(update):
        update.message.reply_text(
            "❌ Invalid moveset page!\n"
            "1. Open @HexaMonBot\n"
            "2. Find the moveset\n"
            "3. Forward it here"
        )
        return GET_MOVESET

    if not update.message.photo:
        update.message.reply_text("❌ Where's the moveset photo?")
        return GET_MOVESET

    context.user_data['moveset'] = {
        'photo': update.message.photo[-1].file_id,
        'text': update.message.caption or "No moveset details provided"
    }
    save_temp_data(update.effective_user.id, context.user_data)

    update.message.reply_text(
        "Is the Pokemon Boosted? If yes then specify."
    )
    return GET_BOOST_INFO

def handle_boosted(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    boosted_status = query.data.split("_")[1]
    context.user_data['boosted'] = boosted_status
    save_temp_data(query.from_user.id, context.user_data)

    if boosted_status == 'yes':
        query.edit_message_text("🔮 Please specify the boosted stat(s)")
        return GET_BOOST_DETAILS
    else:
        context.user_data['boost_details'] = 'Unboosted'
        save_temp_data(query.from_user.id, context.user_data)
        query.edit_message_text("💰 Now enter the base price:")
        return GET_BASE_PRICE

def handle_boost_info(update: Update, context: CallbackContext):
    boost_info = update.message.text.strip()

    if not boost_info or len(boost_info) > 100:
        update.message.reply_text("❌ Please provide valid boost information (max 100 characters)")
        return GET_BOOST_INFO

    context.user_data['boost_info'] = boost_info
    save_temp_data(update.effective_user.id, context.user_data)
    update.message.reply_text("💰 Now enter the base price:")
    return GET_BASE_PRICE

def handle_base_price(update: Update, context: CallbackContext):
    if not context.user_data:
        update.message.reply_text("❌ Session expired. Please start over with /add")
        return ConversationHandler.END

    user = update.effective_user
    update_user_profile(user.id, user.username, user.first_name)

    if context.user_data.get('category') == 'tms':
        return handle_tm_price(update, context)
    else:
        return handle_pokemon_price(update, context)

def handle_tm_price(update: Update, context: CallbackContext):
    try:
        base_price = extract_base_price(update.message.text)

        if base_price is None:
            update.message.reply_text("❌ Please enter a valid price (e.g., '0', '5000' or 'Base: 5k')")
            return GET_BASE_PRICE

        seller_username = update.effective_user.username
        seller_first_name = update.effective_user.first_name
        seller_id = update.effective_user.id

        if seller_username:
            seller_username = seller_username.replace('\\', '')
        if seller_first_name:
            seller_first_name = seller_first_name.replace('\\', '')

        context.user_data.update({
            'base_price': base_price,
            'seller_username': seller_username,
            'seller_first_name': seller_first_name,
            'seller_id': seller_id
        })

        caption = format_tm_auction_item(context.user_data)

        submission_id = save_submission(update.effective_user.id, context.user_data)

        update_submission_stats(update.effective_user.id, 'pending', is_new_submission=True)

        for admin_id in ADMINS:
            try:
                context.bot.send_message(
                    chat_id=admin_id,
                    text=caption,
                    parse_mode='HTML'
                )

                context.bot.send_message(
                    chat_id=admin_id,
                    text="Verify this TM?",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Approve", callback_data=f"verify_{submission_id}"),
                            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{submission_id}")
                        ]
                    ])
                )
            except Exception as e:
                debug_log(f"Failed to alert admin {admin_id}: {str(e)}")

        update.message.reply_text("✅ TM submitted for approval!")
        cleanup_temp_data(update.effective_user.id)
        return ConversationHandler.END

    except Exception as e:
        debug_log(f"TM submission failed: {str(e)}")
        update.message.reply_text("❌ Submission error. Please try /add again.")
        return ConversationHandler.END

def handle_pokemon_price(update: Update, context: CallbackContext):
    try:
        base_price = extract_base_price(update.message.text)

        if base_price is None:
            update.message.reply_text("❌ Please enter a valid price (e.g., '0', '5000' or 'Base: 5k')")
            return GET_BASE_PRICE

        user_data = context.user_data
        if not user_data:
            update.message.reply_text("❌ Session expired. Please start over with /add")
            return ConversationHandler.END

        required_fields = {
            'nature': "Nature page",
            'ivs': "IV/EV page",
            'moveset': "Moveset page",
            'pokemon_name': "Pokémon name",
            'boost_info': "Boost information"
        }

        missing = [name for field, name in required_fields.items() if field not in user_data]
        if missing:
            update.message.reply_text(
                f"❌ Missing data: {', '.join(missing)}\n"
                "Please restart with /add"
            )
            return ConversationHandler.END

        user_data['base_price'] = base_price
        user_data['seller_username'] = update.effective_user.username
        user_data['seller_first_name'] = update.effective_user.first_name

        caption = format_pokemon_auction_item(user_data)

        submission_id = save_submission(
            update.effective_user.id,
            user_data
        )

        update_submission_stats(update.effective_user.id, 'pending', is_new_submission=True)

        admin_notification_sent = False

        for admin_id in ADMINS:
            try:
                context.bot.send_photo(
                    chat_id=admin_id,
                    photo=user_data['nature']['photo'],
                    caption=caption,
                    parse_mode='HTML'
                )

                context.bot.send_message(
                    chat_id=admin_id,
                    text="Verify this submission?",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Approve", callback_data=f"verify_{submission_id}"),
                            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{submission_id}")
                        ]
                    ])
                )

                admin_notification_sent = True
                debug_log(f"Successfully sent submission {submission_id} to admin {admin_id}")

            except Exception as e:
                debug_log(f"Failed to send to admin {admin_id}: {str(e)}")
                try:
                    context.bot.send_message(
                        chat_id=admin_id,
                        text=caption,
                        parse_mode='HTML'
                    )
                    context.bot.send_message(
                        chat_id=admin_id,
                        text="Verify this submission?",
                        reply_markup=InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("✅ Approve", callback_data=f"verify_{submission_id}"),
                                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{submission_id}")
                            ]
                        ])
                    )
                    admin_notification_sent = True
                    debug_log(f"Sent text-only submission {submission_id} to admin {admin_id}")
                except Exception as inner_e:
                    debug_log(f"Failed to send text-only to admin {admin_id}: {str(inner_e)}")

        if not admin_notification_sent:
            debug_log(f"WARNING: Submission {submission_id} was not sent to any admin!")
            update.message.reply_text("❌ Could not send submission to admins. Please try again.")
            return ConversationHandler.END

        update.message.reply_text("✅ Submission sent to admins for verification!")
        cleanup_temp_data(update.effective_user.id)
        return ConversationHandler.END

    except Exception as e:
        debug_log(f"Error in handle_pokemon_price: {str(e)}")
        update.message.reply_text("❌ An error occurred. Please try again.")
        return ConversationHandler.END

def handle_verification(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    action, submission_id = query.data.split('_')
    submission_id = int(submission_id)

    admin_id = query.from_user.id
    admin_name = query.from_user.username or query.from_user.first_name

    submission = get_submission(submission_id)
    if not submission:
        query.edit_message_text("❌ Submission not found in database!")
        return

    if submission['status'] != 'pending':
        query.edit_message_text(f"⚠️ This submission was already {submission['status']}!")
        return

    try:
        submission_data = submission['data']
        if isinstance(submission_data, str):
            submission_data = json.loads(submission_data)

        with db_connection() as conn:
            status = 'processing' if action == 'verify' else 'rejected'
            conn.execute("UPDATE submissions SET status=? WHERE submission_id=?",
                        (status, submission_id))
            conn.commit()

        if action == 'verify':
            update_submission_stats(submission['user_id'], 'approved')
        else:
            update_submission_stats(submission['user_id'], 'rejected')

        if action == 'verify':
            try:
                if submission_data.get('category') != 'tms':
                    seller_username = submission_data.get('seller_username', 'Unknown')
                    seller_first_name = submission_data.get('seller_first_name', 'User')

                    if seller_username and seller_username != 'Unknown':
                        submission_data['seller_username'] = seller_username.replace('\\', '')
                    if seller_first_name:
                        submission_data['seller_first_name'] = seller_first_name.replace('\\', '')

                temp_item_text = "Item #PLACEHOLDER - Creating auction..."

                if submission_data.get('category') == 'tms':
                    new_auction_id = save_auction(
                        item_text=temp_item_text,
                        photo_id=None,
                        base_price=submission_data['base_price'],
                        seller_id=submission['user_id'],
                        seller_name=submission_data.get('seller_username', submission_data.get('seller_first_name', 'Unknown'))
                    )
                else:
                    new_auction_id = save_auction(
                        item_text=temp_item_text,
                        photo_id=submission_data['nature']['photo'],
                        base_price=submission_data['base_price'],
                        seller_id=submission['user_id'],
                        seller_name=submission_data.get('seller_username', submission_data.get('seller_first_name', 'Unknown'))
                    )

                if not new_auction_id:
                    raise Exception("Failed to save auction")

                if submission_data.get('category') == 'tms':
                    item_text = format_tm_auction_item(submission_data, new_auction_id)
                else:
                    item_text = format_pokemon_auction_item(submission_data, new_auction_id)

                with db_connection() as conn:
                    conn.execute('''UPDATE auctions SET item_text=? WHERE auction_id=?''',
                               (item_text, new_auction_id))
                    conn.commit()

                bot_username = context.bot.username
                deep_link = f"https://t.me/{bot_username}?start=bid_{new_auction_id}"

                keyboard = [
                    [
                        InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{new_auction_id}"),
                        InlineKeyboardButton("💰 Place Bid", url=deep_link)
                    ]
                ]

                if submission_data.get('category') == 'tms':
                    message = context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=item_text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='HTML'
                    )
                else:
                    message = context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=submission_data['nature']['photo'],
                        caption=item_text,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode='HTML'
                    )

                with db_connection() as conn:
                    conn.execute('''UPDATE submissions SET
                                  status='approved',
                                  channel_message_id=?
                                  WHERE submission_id=?''',
                               (message.message_id, submission_id))
                    conn.execute('''UPDATE auctions SET
                                   channel_message_id=?
                                   WHERE auction_id=?''',
                                (message.message_id, new_auction_id))
                    conn.commit()

                context.bot.send_message(
                    chat_id=submission['user_id'],
                    text=f"🎉 Your item has been approved and listed! Item ID: #{new_auction_id}"
                )

            except Exception as e:
                debug_log(f"Error during auction creation: {str(e)}")
                with db_connection() as conn:
                    conn.execute("UPDATE submissions SET status='failed' WHERE submission_id=?",
                               (submission_id,))
                    conn.commit()
                raise

        else:  
            try:
                item_type = "TM" if submission_data.get('category') == 'tms' else "Pokémon"
                item_name = submission_data.get('pokemon_name', 'TM') if submission_data.get('category') != 'tms' else "TM"

                rejection_message = (
                    f"❌ Your {item_type} submission was rejected\n\n"
                    f"📦 Item: {item_name}\n"
                    f"⏰ Submitted: {str(submission['created_at'])}\n\n"
                    "You can submit a new item using /add"
                )

                context.bot.send_message(
                    chat_id=submission['user_id'],
                    text=rejection_message
                )

                debug_log(f"Rejection notification sent to user {submission['user_id']}")

            except Exception as e:
                debug_log(f"Failed to send rejection notification to user {submission['user_id']}: {str(e)}")

        result_text = f"{'✅ APPROVED' if action == 'verify' else '❌ REJECTED'} by @{admin_name}"

        try:
            query.edit_message_text(
                text=result_text,
                reply_markup=None  
            )
        except Exception as e:
            debug_log(f"Could not update the clicked message: {str(e)}")

        action_result = "approved" if action == "verify" else "rejected"
        for other_admin_id in ADMINS:
            if other_admin_id != admin_id:  
                try:
                    context.bot.send_message(
                        chat_id=other_admin_id,
                        text=f"Submission #{submission_id} was {action_result} by @{admin_name}"
                    )
                except Exception as e:
                    debug_log(f"Could not notify admin {other_admin_id}: {str(e)}")

    except Exception as e:
        debug_log(f"Verification failed: {str(e)}")
        try:
            query.edit_message_text("❌ Processing failed. Check logs.")
        except:
            try:
                context.bot.send_message(
                    chat_id=admin_id,
                    text="❌ Processing failed. Check logs."
                )
            except:
                pass  

def handle_bid_amount(update: Update, context: CallbackContext):
    if 'bid_context' not in context.user_data:
        return

    user_id = update.effective_user.id
    if user_id not in ADMINS and not check_verification_status(user_id):
        update.message.reply_text(
            "🔒 Verification Required\n\n"
            "Contact an admin for verification.\n"
        )
        context.user_data.pop('bid_context', None)
        return

    try:
        with db_connection() as conn:
            auctions_open = conn.execute("SELECT auctions_open FROM system_status WHERE id=1").fetchone()[0]
            
        if not auctions_open:
            update.message.reply_text("❌ Auctions are currently closed. Bidding is not allowed.")
            context.user_data.pop('bid_context', None)
            return

        bid_text = update.message.text.replace(',', '').strip()
        bid_amount = parse_bid_amount(bid_text)

        if bid_amount is None:
            update.message.reply_text(
                "❌ Please enter a valid bid amount!"
            )
            return

        bid_context = context.user_data['bid_context']

        auction = get_auction(bid_context['auction_id'])
        if not auction:
            update.message.reply_text("❌ This auction no longer exists.")
            context.user_data.pop('bid_context', None)
            return
            

        current_amount = auction.get('current_bid') or auction.get('base_price', 0)
        min_bid = current_amount + get_min_increment(current_amount)


        bid_amount_int = int(bid_amount)
        current_amount_int = int(current_amount)
        min_bid_int = int(min_bid)

        debug_log(f"BID DEBUG: bid_amount_int={bid_amount_int}, current_amount_int={current_amount_int}, min_bid_int={min_bid_int}")

        if bid_amount_int < min_bid_int:
            current_formatted = format_bid_amount(current_amount)
            min_formatted = format_bid_amount(min_bid)
            increment_formatted = format_bid_amount(get_min_increment(current_amount))

            debug_log(f"BID REJECTED: {bid_amount_int} < {min_bid_int}")

            update.message.reply_text(
                f"❌ Bid must be at least {min_formatted}\n"
                f"Current bid: {current_formatted}\n"
                f"Minimum increment: {increment_formatted}\n\n"
                f"💡 Your bid: {format_bid_amount(bid_amount)}"
            )
            return

        bidder_name = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name
        prev_bidder, auction_id = record_bid(
            bid_context['auction_id'],
            update.effective_user.id,
            bidder_name,
            bid_amount_int , 
            context
        )

        context.user_data.pop('bid_context', None)

        updated_auction = get_auction(bid_context['auction_id'])
        if not updated_auction:
            update.message.reply_text("❌ Error updating auction.")
            return

        caption = format_auction(updated_auction)

        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start=bid_{updated_auction['auction_id']}"

        keyboard = [[
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{updated_auction['auction_id']}"),
            InlineKeyboardButton("💰 Place Bid", url=deep_link)
        ]]

        try:
            if updated_auction.get('photo_id'):
                context.bot.edit_message_caption(
                    chat_id=CHANNEL_ID,
                    message_id=bid_context['channel_msg_id'],
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=bid_context['channel_msg_id'],
                    text=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
        except Exception as e:
            debug_log(f"Channel update failed: {str(e)}")
            try:
                plain_caption = caption.replace('<br>', '\n').replace('<a href="', '').replace('">', ' ').replace('</a>', '')
                if updated_auction.get('photo_id'):
                    context.bot.edit_message_caption(
                        chat_id=CHANNEL_ID,
                        message_id=bid_context['channel_msg_id'],
                        caption=plain_caption,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    context.bot.edit_message_text(
                        chat_id=CHANNEL_ID,
                        message_id=bid_context['channel_msg_id'],
                        text=plain_caption,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
            except Exception as fallback_error:
                debug_log(f"Fallback update failed: {str(fallback_error)}")

        if prev_bidder and prev_bidder[0] != update.effective_user.id:
            try:
                send_outbid_notification(
                    context,
                    prev_bidder,
                    bid_context['item_text'],
                    bid_amount_int,
                    auction_id
                )
            except Exception as e:
                debug_log(f"Couldn't notify outbid user: {str(e)}")

        formatted_bid = format_bid_amount(bid_amount)
        update.message.reply_text(f"✅ Your bid of {formatted_bid} has been placed!")

    except ValueError:
        update.message.reply_text(
            "❌ Please enter a valid bid amount!"
        )
    except Exception as e:
        debug_log(f"Error in handle_bid_amount: {str(e)}")
        update.message.reply_text("❌ An error occurred. Your bid was recorded but the display may not update.")
        context.user_data.pop('bid_context', None)

def send_outbid_notification(context, prev_bidder, item_text, bid_amount, auction_id):
    if not prev_bidder or not prev_bidder[0]:
        return

    outbid_user_id = prev_bidder[0]

    try:
        auction = get_auction(auction_id)
        if not auction:
            return

        item_name = extract_item_name(item_text)

        current_bidder_name = get_current_bidder_name(auction_id) or "Unknown"
        current_bidder_name = current_bidder_name.replace('\\', '')

        try:
            channel_entity = context.bot.get_chat(CHANNEL_ID)
            channel_username = channel_entity.username
        except:
            channel_username = None

        message_link = None
        if channel_username and auction.get('channel_message_id'):
            message_link = f"https://t.me/{channel_username}/{auction['channel_message_id']}"
        else:
            try:
                message_link = f"https://t.me/c/{str(CHANNEL_ID).replace('-100', '')}/{auction['channel_message_id']}"
            except:
                pass

        formatted_bid = format_bid_amount(bid_amount)

        if message_link:
            message = (
                f"Oof, You have been outbid on <a href='{message_link}'>{html.escape(item_name)}</a> 😬\n"
                f"<b>{html.escape(current_bidder_name)}</b> just showed you how it's really done 👑\n"
                f"<blockquote><b><i>New bid: {formatted_bid} 💸</i></b></blockquote>\n"
                f"Gonna let them get away with that 🤨, or are you still in this fight? 🥊"
            )
        else:
            message = (
                f"Oof, You have been outbid on {html.escape(item_name)} 😬\n"
                f"<b>{html.escape(current_bidder_name)}</b> just showed you how it's really done 👑\n"
                f"<blockquote><b><i>New bid: {formatted_bid} 💸</i></b></blockquote>\n"
                f"Gonna let them get away with that 🤨, or are you still in this fight? 🥊"
            )

        context.bot.send_message(
            chat_id=outbid_user_id,
            text=message,
            parse_mode='HTML',
            disable_web_page_preview=False
        )

    except telegram.error.Unauthorized:
        debug_log(f"User {outbid_user_id} blocked the bot")
    except Exception as e:
        debug_log(f"Error sending outbid notification: {str(e)}")

def extract_item_name(item_text):
    if not item_text:
        return "Unknown Item"

    tm_match = re.search(r'(TM\d+)[^\n]*', item_text)
    if tm_match:
        return tm_match.group(1).strip()  

    pokemon_match = re.search(r'Pokémon:\s*([^\n]+)', item_text, re.IGNORECASE)
    if pokemon_match:
        return pokemon_match.group(1).strip()

    tm_patterns = [
        r'💿\s*([^\n]+)',  
        r'Technical Machine[^\n]*',  
        r'TM:\s*([^\n]+)',  
    ]

    for pattern in tm_patterns:
        tm_match = re.search(pattern, item_text, re.IGNORECASE)
        if tm_match:
            if len(tm_match.groups()) > 0:
                return tm_match.group(1).strip()
            return tm_match.group(0).strip()

    lines = item_text.split('\n')
    for line in lines:
        if line.strip():  
            if re.search(r'TM\d+', line):
                tm_match = re.search(r'(TM\d+)', line)
                if tm_match:
                    return tm_match.group(1)
            return line[:30] + "..." if len(line) > 30 else line

    return "Auction Item"

def get_current_bidder_name(auction_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT current_bidder FROM auctions
                         WHERE auction_id=?''', (auction_id,))
            result = c.fetchone()
            if result and result['current_bidder']:
                bidder_name = result['current_bidder']
                bidder_name = bidder_name.replace('\\', '')
                return bidder_name
    except Exception as e:
        debug_log(f"Error getting current bidder name: {str(e)}")

    return "Unknown"

def handle_bid_button(update: Update, context: CallbackContext):
    query = update.callback_query

    try:
        try:
            query.answer()
        except Exception as e:
            debug_log(f"Couldn't answer callback query: {str(e)}")

        auction_id = int(query.data.split('_')[1])

        auction = get_auction(auction_id)

        if not auction:
            debug_log(f"Auction #{auction_id} not found - may be expired or closed")

            try:
                context.bot.edit_message_reply_markup(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    reply_markup=None  
                )
            except telegram.error.BadRequest as e:
                if "Message is not modified" in str(e):
                    pass
                elif "message to edit not found" in str(e) or "Message can't be edited" in str(e):
                    debug_log(f"Couldn't edit old message {query.message.message_id}")
                else:
                    debug_log(f"Couldn't edit message buttons: {str(e)}")
            except Exception as e:
                debug_log(f"Error editing message: {str(e)}")

            return

        if auction.get('auction_status') != 'active':
            debug_log(f"Auction #{auction_id} is not active (status: {auction.get('auction_status')})")

            try:
                context.bot.edit_message_reply_markup(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    reply_markup=None
                )
            except Exception as e:
                debug_log(f"Couldn't remove buttons from inactive auction: {str(e)}")

            return

        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start=bid_{auction['auction_id']}"

        keyboard = [[
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data=f"refresh_{auction_id}"
            ),
            InlineKeyboardButton(
                "💰 Place Bid",
                url=deep_link  
            )
        ]]

        context.user_data['bid_context'] = {
            'auction_id': auction['auction_id'],
            'channel_msg_id': query.message.message_id,
            'min_bid': (auction.get('current_bid') or auction.get('base_price', 0)) + get_min_increment(auction.get('current_bid')),
            'current_bidder': auction.get('current_bidder'),
            'item_text': auction['item_text']
        }

        try:
            context.bot.edit_message_reply_markup(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                pass
            elif "Query is too old" in str(e):
                try:
                    context.bot.send_message(
                        chat_id=query.message.chat.id,
                        text=f"",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        reply_to_message_id=query.message.message_id
                    )
                except Exception as send_error:
                    debug_log(f"Couldn't send new message: {str(send_error)}")
            else:
                debug_log(f"Couldn't update buttons: {str(e)}")
        except Exception as e:
            debug_log(f"Couldn't update buttons: {str(e)}")

    except ValueError:
        debug_log(f"Invalid auction ID format in callback data: {query.data}")
        try:
            query.answer("❌ Invalid auction", show_alert=False)
        except:
            pass
    except Exception as e:
        debug_log(f"Error in handle_bid_button: {str(e)}")

def handle_refresh_button(update: Update, context: CallbackContext):
    query = update.callback_query

    try:
        try:
            query.answer("🔄 Refreshing...")
        except Exception as e:
            debug_log(f"Couldn't answer refresh callback: {str(e)}")

        time.sleep(0.3)

        auction_id = int(query.data.split('_')[1])

        auction = get_auction(auction_id)

        if not auction or auction.get('auction_status') != 'active':
            try:
                query.answer("❌ Auction not available", show_alert=True)
            except:
                pass
            return

        caption = format_auction(auction)

        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start=bid_{auction_id}"

        keyboard = [[
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data=f"refresh_{auction_id}"
            ),
            InlineKeyboardButton(
                "💰 Place Bid",
                url=deep_link
            )
        ]]

        try:
            if auction.get('photo_id'):
                context.bot.edit_message_caption(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                context.bot.edit_message_text(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    text=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )

            try:
                query.answer("✅ Refreshed!")
            except:
                pass

        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                try:
                    query.answer("✅ Already up to date!")
                except:
                    pass
            else:
                debug_log(f"Couldn't refresh auction message: {str(e)}")
                try:
                    query.answer("❌ Refresh failed", show_alert=True)
                except:
                    pass
        except Exception as e:
            debug_log(f"Error refreshing auction: {str(e)}")
            try:
                query.answer("❌ Refresh failed", show_alert=True)
            except:
                pass

    except Exception as e:
        debug_log(f"Error in handle_refresh_button: {str(e)}")

@admin_only
def remove_item(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text(
            "❌ Usage: /removeitem <item_id>\n\n"
            "To find Item IDs, use /items command or check the auction message in the channel."
        )
        return

    try:
        auction_id = int(context.args[0])

        auction = get_auction(auction_id)
        if not auction:
            update.message.reply_text(f"❌ Item #{auction_id} not found!")
            return

        submission = None
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT s.user_id, s.data
                         FROM submissions s
                         WHERE s.channel_message_id = ?''',
                     (auction['channel_message_id'],))
            submission = c.fetchone()

        message_deletion_status = "not_attempted"
        deletion_error = None

        if auction.get('channel_message_id'):
            try:
                context.bot.delete_message(
                    chat_id=CHANNEL_ID,
                    message_id=auction['channel_message_id']
                )
                message_deletion_status = "success"
                debug_log(f"Deleted auction message {auction['channel_message_id']} from channel")

            except telegram.error.BadRequest as e:
                if "message to delete not found" in str(e).lower():
                    message_deletion_status = "already_deleted"
                    debug_log(f"Message {auction['channel_message_id']} was already deleted from channel")
                else:
                    message_deletion_status = "failed"
                    deletion_error = str(e)
                    debug_log(f"Failed to delete message: {str(e)}")

            except Exception as e:
                message_deletion_status = "failed"
                deletion_error = str(e)
                debug_log(f"Error deleting message: {str(e)}")

        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''UPDATE auctions SET is_active = 0, auction_status = 'removed'
                         WHERE auction_id = ?''', (auction_id,))
            conn.commit()

        if submission:
            seller_id = submission['user_id']
            update_submission_stats(seller_id, 'revoked')
            debug_log(f"Updated revoked count for user {seller_id}")

        if submission:
            try:
                seller_id = submission['user_id']
                data = json.loads(submission['data']) if isinstance(submission['data'], str) else submission['data']

                if data.get('category') == 'tms':
                    item_name = "TM"
                else:
                    item_name = data.get('pokemon_name', 'Unknown Pokémon')

                notification_text = (
                    "❌ Your auction item has been removed by admin\n\n"
                    f"📦 Item: {item_name}\n"
                    f"🏷️ Item ID: {auction_id}\n\n"
                    "ℹ️ If you believe this was a mistake, please contact an admin."
                )

                context.bot.send_message(chat_id=seller_id, text=notification_text)
                debug_log(f"Notified seller {seller_id} about removed auction {auction_id}")

            except Exception as e:
                debug_log(f"Could not notify seller: {str(e)}")

        item_preview = auction['item_text'][:50]
        response_text = (
            f"✅ Item #{auction_id} has been removed successfully!\n\n"
        )

        if message_deletion_status == "success":
            response_text += "🗑️ Channel Message: Deleted"
        elif message_deletion_status == "already_deleted":
            response_log_text += "ℹ️ Channel Message: Was already deleted"
        elif message_deletion_status == "failed":
            response_text += f"⚠️ Channel Message: Failed to delete ({deletion_error})"
        else:
            response_text += "ℹ️ Channel Message: No message ID found"

        update.message.reply_text(response_text)

    except ValueError:
        update.message.reply_text("❌ Please enter a valid item ID number!")
    except Exception as e:
        debug_log(f"Error in /removeitem: {str(e)}")
        update.message.reply_text("❌ Error removing item. Please check the item ID and try again.")

def get_active_auctions_by_category():
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT a.*, s.data
                         FROM auctions a
                         LEFT JOIN submissions s ON a.channel_message_id = s.channel_message_id
                         WHERE a.auction_status = 'active' 
                         ORDER BY a.created_at DESC''')
            auctions = c.fetchall()

            categorized = {
                'nonlegendary': [],
                'shiny': [],
                'legendary': [],
                'tms': []
            }

            for auction in auctions:
                submission_data = json.loads(auction['data']) if auction['data'] else {}
                category = submission_data.get('category', 'nonlegendary')

                if category == 'legendary':
                    categorized['legendary'].append(auction)
                elif category == 'shiny':
                    categorized['shiny'].append(auction)
                elif category == 'tms':
                    categorized['tms'].append(auction)
                else:
                    categorized['nonlegendary'].append(auction)

            return categorized

    except Exception as e:
        debug_log(f"Error getting auctions: {str(e)}")
        return None

@verified_only
def handle_items(update: Update, context: CallbackContext):
    try:
        categorized = get_active_auctions_by_category()
        if not categorized:
            update.message.reply_text("ℹ️ No active auctions currently.")
            return

        try:
            channel_entity = context.bot.get_chat(CHANNEL_ID)
            channel_username = channel_entity.username
            if not channel_username:
                channel_username = f"c/{str(CHANNEL_ID).replace('-100', '')}"
        except:
            channel_username = None

        category_to_show = 'legendary'
        items_to_display = categorized.get(category_to_show, [])

        if not items_to_display:
            for cat in ['legendary', 'nonlegendary', 'shiny', 'tms']:
                if categorized.get(cat):
                    category_to_show = cat
                    items_to_display = categorized.get(cat, [])
                    break

        response = [f"<b>{get_category_display_name(category_to_show)} Items</b>"]

        if not items_to_display:
            response.append("\nNo items in this category.")
        else:
            for i, auction in enumerate(items_to_display, 1):
                item_display = format_item_for_list(auction, channel_username)
                response.append(f"{i}. {item_display}")

        keyboard = [
            [
                InlineKeyboardButton("6L", callback_data="items_legendary"),
                InlineKeyboardButton("0L", callback_data="items_nonlegendary"),
                InlineKeyboardButton("Shiny", callback_data="items_shiny"),
                InlineKeyboardButton("TM", callback_data="items_tms")
            ]
        ]

        try:
            update.message.reply_text(
                "\n".join(response),
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except telegram.error.RetryAfter as e:
            debug_log(f"Flood control in /items, waiting {e.retry_after} seconds")
            time.sleep(e.retry_after)
            update.message.reply_text(
                "\n".join(response),
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except Exception as e:
        debug_log(f"Error in /items: {str(e)}")
        try:
            update.message.reply_text("❌ Error fetching active items. Please try again.")
        except:
            debug_log("Could not send error message for /items")

def get_category_display_name(category):
    display_names = {
        'legendary': '🌟 Legendary',
        'nonlegendary': '🔹 Non-Legendary',
        'shiny': '✨ Shiny',
        'tms': '💿 TM'
    }
    return display_names.get(category, category.title())

def format_item_for_list(auction_row, channel_username):
    auction = dict(auction_row)
    data_str = auction.get('data', '{}')
    try:
        submission_data = json.loads(data_str) if data_str else {}
    except:
        submission_data = {}

    if submission_data.get('category') == 'tms':
        item_text = auction.get('item_text', '')
        tm_match = re.search(r'TM\d+', item_text)
        tm_name = tm_match.group(0) if tm_match else "TM"
        display_name = f"{tm_name} 💿"
    else:
        pokemon_name = submission_data.get('pokemon_name', 'Unknown Pokémon')
        item_text = auction.get('item_text', '')
        nature_match = re.search(r'Nature:\s*([A-Za-z]+)', item_text)
        nature = nature_match.group(1) if nature_match else "Unknown"
        display_name = f"{pokemon_name}-{nature}"

    if channel_username and auction.get('channel_message_id'):
        message_link = f"https://t.me/{channel_username}/{auction['channel_message_id']}"
        return f'<a href="{message_link}">{display_name}</a>'
    else:
        return display_name

def handle_items_category_switch(update: Update, context: CallbackContext):
    query = update.callback_query

    try:
        query.answer()
    except Exception as e:
        debug_log(f"Error answering callback: {str(e)}")

    try:
        category = query.data.split('_')[1]  

        categorized = get_active_auctions_by_category()
        if not categorized:
            try:
                query.edit_message_text("ℹ️ No active auctions currently.")
            except Exception as e:
                debug_log(f"Error editing message for no auctions: {str(e)}")
            return

        items_to_display = categorized.get(category, [])

        response = [f"<b>{get_category_display_name(category)} Items</b>"]

        if not items_to_display:
            response.append("\nNo items in this category.")
        else:
            try:
                channel_entity = context.bot.get_chat(CHANNEL_ID)
                channel_username = channel_entity.username
                if not channel_username:
                    channel_username = f"c/{str(CHANNEL_ID).replace('-100', '')}"
            except:
                channel_username = None

            for i, auction in enumerate(items_to_display, 1):
                item_display = format_item_for_list(auction, channel_username)
                response.append(f"{i}. {item_display}")

        keyboard = [
            [
                InlineKeyboardButton("6L", callback_data="items_legendary"),
                InlineKeyboardButton("0L", callback_data="items_nonlegendary"),
                InlineKeyboardButton("Shiny", callback_data="items_shiny"),
                InlineKeyboardButton("TM", callback_data="items_tms")
            ]
        ]

        try:
            query.edit_message_text(
                "\n".join(response),
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                debug_log("Category button spam detected - message not modified")
                return
            elif "Message to edit not found" in str(e):
                debug_log("Message to edit not found - sending new message")
                context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text="\n".join(response),
                    parse_mode='HTML',
                    disable_web_page_preview=True,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                raise
        except telegram.error.RetryAfter as e:
            debug_log(f"Flood control, retrying after {e.retry_after} seconds")
            time.sleep(e.retry_after)
            query.edit_message_text(
                "\n".join(response),
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except Exception as e:
        debug_log(f"Error in category switch: {str(e)}")
        try:
            context.bot.send_message(
                chat_id=query.message.chat_id,
                text="❌ Error switching category. Please use /items again.",
                reply_to_message_id=query.message.message_id
            )
        except:
            debug_log("Could not send error notification")

def get_user_approved_items(user_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT s.*, a.auction_status 
                         FROM submissions s
                         LEFT JOIN auctions a ON s.channel_message_id = a.channel_message_id
                         WHERE s.user_id=? AND s.status='approved'
                         ORDER BY s.created_at DESC''', (user_id,))
            return c.fetchall()
    except Exception as e:
        debug_log(f"Error getting user items: {str(e)}")
        return []

@verified_only
def handle_myitems(update: Update, context: CallbackContext):
    try:
        user_id = update.effective_user.id
        items = get_user_approved_items(user_id)

        if not items:
            update.message.reply_text("📭 You don't have any approved items in auctions yet.")
            return

        try:
            channel_entity = context.bot.get_chat(CHANNEL_ID)
            channel_username = channel_entity.username
            if not channel_username:
                channel_username = f"c/{str(CHANNEL_ID).replace('-100', '')}"
        except:
            channel_username = None

        response = ["<b>📋 Your Auction Items</b>"]

        active_items = []
        ended_items = []

        for item in items:
            try:
                data = json.loads(item['data']) if isinstance(item['data'], str) else item['data'] or {}
                category = data.get('category', 'unknown')

                if category.lower() == 'tms':
                    name = "TM"
                else:
                    name = data.get('pokemon_name', 'Unknown Pokémon')

                if item['channel_message_id']:
                    auction = get_auction_by_channel_id_any_status(item['channel_message_id'])
                    if auction and auction.get('auction_status') == 'active':
                        active_items.append((item, name, category))
                    else:
                        ended_items.append((item, name, category))
                else:
                    ended_items.append((item, name, category))

            except Exception as e:
                debug_log(f"Error formatting item: {str(e)}")
                continue

        if active_items:
            response.append("\n<b>🟢 Active Items:</b>")
            for i, (item, name, category) in enumerate(active_items, 1):
                if channel_username and item['channel_message_id']:
                    message_link = f"https://t.me/{channel_username}/{item['channel_message_id']}"
                    item_display = f'<a href="{message_link}">{name}</a>'
                else:
                    item_display = name

                response.append(f"  {i}. {item_display} ({category.title()})")

        if ended_items:
            response.append("\n<b>🌀 Removed Items:</b>")
            for i, (item, name, category) in enumerate(ended_items, 1):
                response.append(f"  {i}. {name} ({category.title()})")

        if not active_items and not ended_items:
            response.append("\nNo items found")

        update.message.reply_text("\n".join(response), parse_mode='HTML', disable_web_page_preview=True)

    except Exception as e:
        debug_log(f"Error in /myitems: {str(e)}")
        update.message.reply_text("❌ Error fetching your items. Please try again.")

def handle_topbuyers(update: Update, context: CallbackContext):
    buyers = get_top_buyers()
    if not buyers:
        update.message.reply_text("📭 No buyers yet!")
        return

    response_lines = ["🏆 Top 5 Buyers 🏆"]
    for i, row in enumerate(buyers, 1):
        user_id, username, wins = row
        username = username or "Unknown"
        username = username.replace('@', '').replace('\\', '')
        response_lines.append(f"{i}. @{username} – {wins} item{'s' if wins != 1 else ''}")

    update.message.reply_text("\n".join(response_lines))

def handle_topsellers(update: Update, context: CallbackContext):
    sellers = get_top_sellers()
    if not sellers:
        update.message.reply_text("📭 No sellers yet!")
        return

    response_lines = ["💰 Top 5 Sellers 💰"]
    for i, row in enumerate(sellers, 1):
        user_id, username, sales = row
        username = username or "Unknown"
        username = username.replace('@', '').replace('\\', '')
        response_lines.append(f"{i}. @{username} – {sales} item{'s' if sales != 1 else ''}")

    update.message.reply_text("\n".join(response_lines))

def get_bid_history(auction_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT bid_id, bidder_name, amount, timestamp
                         FROM bids
                         WHERE auction_id=? AND is_active=1
                         ORDER BY amount DESC''', (auction_id,))
            return c.fetchall()
    except Exception as e:
        debug_log(f"Error getting bid history: {str(e)}")
        return []

def show_bid_history(update: Update, context: CallbackContext):
    try:
        if not context.args:
            update.message.reply_text("❌ Usage: /history <auction_id>")
            return

        auction_id = int(context.args[0])
        history = get_bid_history(auction_id)

        if not history:
            update.message.reply_text(f"No bid history found for Item #{auction_id}")
            return

        response = [f"📊 Bid History for Item #{auction_id}"]
        for bid in history:
            bid_id, bidder, amount, time = bid
            response.append(f"🏷️ Bid #{bid_id}: {bidder} - {amount:,} at {time}")

        update.message.reply_text("\n".join(response))

    except Exception as e:
        debug_log(f"Error in show_bid_history: {str(e)}")
        update.message.reply_text("❌ Error fetching bid history")

def remove_last_bid(auction_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()

            c.execute('''SELECT bid_id, bidder_id, bidder_name, amount
                         FROM bids
                         WHERE auction_id=? AND is_active=1
                         ORDER BY timestamp DESC, bid_id DESC
                         LIMIT 1''', (auction_id,))
            last_bid = c.fetchone()

            if not last_bid:
                return None

            bid_id, last_bidder_id, last_bidder_name, amount = last_bid

            c.execute('''UPDATE bids SET is_active=0 WHERE bid_id=?''', (bid_id,))

            c.execute('''SELECT bidder_id, bidder_name, amount FROM bids
                         WHERE auction_id=? AND is_active=1
                         ORDER BY amount DESC
                         LIMIT 1''', (auction_id,))
            new_top = c.fetchone()

            if new_top:
                new_bidder_id, new_bidder_name, new_amount = new_top
                c.execute('''UPDATE auctions SET
                             current_bid=?,
                             current_bidder_id=?,
                             current_bidder=?,
                             previous_bidder=?
                             WHERE auction_id=?''',
                          (new_amount, new_bidder_id, new_bidder_name, last_bidder_name, auction_id))
                result = (new_bidder_name, new_amount)
            else:
                c.execute('''UPDATE auctions SET
                             current_bid=NULL,
                             current_bidder_id=NULL,
                             current_bidder=NULL,
                             previous_bidder=?
                             WHERE auction_id=?''',
                          (last_bidder_name, auction_id))
                result = (None, None)

            conn.commit()
            return result

    except Exception as e:
        debug_log(f"Error in remove_last_bid: {str(e)}")
        return None

def handle_remove_bid(update: Update, context: CallbackContext):
    try:
        if update.effective_user.id not in ADMINS:
            update.message.reply_text("❌ Admin only command!")
            return

        if not context.args:
            update.message.reply_text("❌ Usage: /removebid <auction_id>")
            return

        auction_id = int(context.args[0])
        auction = get_auction(auction_id)

        if not auction:
            update.message.reply_text(f"❌ Item #{auction_id} not found!")
            return

        result = remove_last_bid(auction_id)

        if not result:
            update.message.reply_text(f"❌ No active bids to remove for Items #{auction_id}")
            return

        new_bidder, new_amount = result

        updated_auction = get_auction(auction_id)
        if not updated_auction:
            update.message.reply_text("❌ Error getting updated auction data")
            return

        new_amount = new_amount if new_amount else updated_auction['base_price']

        auction_id_text = str(auction_id)
        current_bid_str = f"{new_amount:,}" if new_amount is not None else 'None'

        current_bidder_name = new_bidder if new_bidder else 'None'

        current_bidder_id = updated_auction.get('current_bidder_id')
        if current_bidder_id and current_bidder_name != 'None':
            bidder_link = f'<a href="tg://user?id={current_bidder_id}">{current_bidder_name}</a>'
        else:
            bidder_link = current_bidder_name

        item_text = updated_auction['item_text']

        caption = (
            f"{item_text}\n"
            f"<blockquote>🔼 Current Bid: {current_bid_str}\n"
            f"👤 Bidder: {bidder_link}</blockquote>"
        )

        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start=bid_{auction_id}"

        keyboard = [
            [
                InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh_{auction_id}"),
                InlineKeyboardButton("💰 Place Bid", url=deep_link)
            ]
        ]

        update_success = True
        try:
            if updated_auction.get('photo_id'):
                context.bot.edit_message_caption(
                    chat_id=CHANNEL_ID,
                    message_id=updated_auction['channel_message_id'],
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
            else:
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=updated_auction['channel_message_id'],
                    text=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='HTML'
                )
        except Exception as e:
            debug_log(f"Channel update failed: {str(e)}")
            update_success = False

        response = (
            f"✅ Last bid removed from Item #{auction_id}\n"
            f"New top bid: {new_bidder or 'None'} with {new_amount:,}"
        )

        if not update_success:
            response += "\n⚠️ Note: Couldn't update auction message"

        update.message.reply_text(response)

    except Exception as e:
        debug_log(f"Error in handle_remove_bid: {str(e)}")
        update.message.reply_text("❌ Error removing bid. Please check the auction ID.")

def get_user_leading_bids(user_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()

            c.execute('''SELECT a.auction_id, a.item_text, s.data, b.amount, a.auction_status
                         FROM auctions a
                         JOIN bids b ON a.auction_id = b.auction_id
                         LEFT JOIN submissions s ON a.channel_message_id = s.channel_message_id
                         WHERE b.bidder_id = ?
                         AND b.is_active = 1
                         AND a.auction_status IN ('active', 'ended') 
                         AND b.amount = (
                             SELECT MAX(amount)
                             FROM bids
                             WHERE auction_id = a.auction_id
                             AND is_active = 1
                         )
                         ORDER BY b.timestamp DESC''', (user_id,))
            return c.fetchall()
    except Exception as e:
        debug_log(f"Error getting user leading bids: {str(e)}")
        return []

@verified_only
def handle_mybids(update: Update, context: CallbackContext):
    try:
        user_id = update.effective_user.id
        user_bids = get_user_leading_bids(user_id)

        if not user_bids:
            update.message.reply_text("You're not currently the highest bidder on any item.")
            return

        try:
            channel_entity = context.bot.get_chat(CHANNEL_ID)
            channel_username = channel_entity.username
            if not channel_username:
                channel_username = f"c/{str(CHANNEL_ID).replace('-100', '')}"
        except:
            channel_username = None

        response = ["<b>Your Current Bids</b>"]

        for i, bid_data in enumerate(user_bids, 1):
            auction_id = bid_data[0]
            item_text = bid_data[1]
            submission_data = bid_data[2]
            amount = bid_data[3]
            auction_status = bid_data[4]  

            auction = get_auction(auction_id)
            if not auction:
                continue

            item_name = "Unknown Item"
            if submission_data:
                try:
                    data = json.loads(submission_data) if isinstance(submission_data, str) else submission_data
                    if data.get('category') == 'tms':
                        tm_text = data.get('tm_details', {}).get('text', '')
                        tm_match = re.search(r'(TM\d+)', tm_text)
                        item_name = tm_match.group(1) if tm_match else "TM"
                    else:
                        item_name = data.get('pokemon_name', 'Unknown Pokémon')
                except:
                    item_name = extract_item_name(item_text)
            else:
                item_name = extract_item_name(item_text)

            if channel_username and auction.get('channel_message_id'):
                message_link = f"https://t.me/{channel_username}/{auction['channel_message_id']}"
                item_display = f'<a href="{message_link}">{item_name}</a>'
            else:
                item_display = item_name

            status_indicator = "🟢" if auction_status == 'active' else "🔴"
            response.append(f"{i}. {item_display} - {amount:,} 💵")

        update.message.reply_text("\n".join(response), parse_mode='HTML', disable_web_page_preview=True)

    except Exception as e:
        debug_log(f"Error in /mybids: {str(e)}")
        update.message.reply_text("❌ Error fetching your bids. Please try again.")

def update_user_profile(user_id, username, first_name):
    try:
        with profile_connection() as conn:
            c = conn.cursor()

            c.execute('SELECT * FROM user_profiles WHERE user_id=?', (user_id,))
            existing = c.fetchone()

            if existing:
                c.execute('''UPDATE user_profiles
                            SET username = ?, first_name = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE user_id = ?''',
                         (username, first_name, user_id))
                debug_log(f"Updated existing profile for user {user_id}")
            else:
                c.execute('''INSERT INTO user_profiles
                            (user_id, username, first_name, total_submissions, approved_submissions,
                             rejected_submissions, pending_submissions, revoked_submissions)
                            VALUES (?, ?, ?, 0, 0, 0, 0, 0)''',
                         (user_id, username, first_name))
                debug_log(f"Created new profile for user {user_id}")

            conn.commit()
    except Exception as e:
        debug_log(f"Error updating user profile: {str(e)}")

def update_submission_stats(user_id, status_change, is_new_submission=False):
    try:
        debug_log(f"=== START UPDATE SUBMISSION STATS ===")
        debug_log(f"User: {user_id}, Status: {status_change}, New: {is_new_submission}")

        with profile_connection() as conn:
            c = conn.cursor()

            c.execute('SELECT * FROM user_profiles WHERE user_id=?', (user_id,))
            existing_profile = c.fetchone()

            if not existing_profile:
                debug_log(f"Creating NEW profile for user {user_id}")
                c.execute('''INSERT INTO user_profiles
                            (user_id, username, first_name, total_submissions, approved_submissions,
                             rejected_submissions, pending_submissions, revoked_submissions)
                            VALUES (?, ?, ?, 0, 0, 0, 0, 0)''',
                         (user_id, None, None))
                conn.commit()
                debug_log("New profile created successfully")

            c.execute('''SELECT total_submissions, pending_submissions, approved_submissions,
                                rejected_submissions, revoked_submissions
                         FROM user_profiles WHERE user_id=?''', (user_id,))
            before = c.fetchone()
            debug_log(f"BEFORE - Total: {before['total_submissions']}, Pending: {before['pending_submissions']}, Approved: {before['approved_submissions']}, Rejected: {before['rejected_submissions']}, Revoked: {before['revoked_submissions']}")

            if is_new_submission:
                c.execute('''UPDATE user_profiles
                            SET total_submissions = total_submissions + 1,
                                pending_submissions = pending_submissions + 1,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE user_id = ?''', (user_id,))
                debug_log("ACTION: Incremented total and pending for new submission")
            else:
                if status_change == 'approved':
                    c.execute('''UPDATE user_profiles
                                SET pending_submissions = pending_submissions - 1,
                                    approved_submissions = approved_submissions + 1,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE user_id = ?''', (user_id,))
                    debug_log("ACTION: Moved from pending to approved")
                elif status_change == 'rejected':
                    c.execute('''UPDATE user_profiles
                                SET pending_submissions = pending_submissions - 1,
                                    rejected_submissions = rejected_submissions + 1,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE user_id = ?''', (user_id,))
                    debug_log("ACTION: Moved from pending to rejected")
                elif status_change == 'revoked':
                    c.execute('''UPDATE user_profiles
                                SET approved_submissions = approved_submissions - 1,
                                    revoked_submissions = revoked_submissions + 1,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE user_id = ?''', (user_id,))
                    debug_log("ACTION: Moved from approved to revoked")

            c.execute('''SELECT total_submissions, pending_submissions, approved_submissions,
                                rejected_submissions, revoked_submissions
                         FROM user_profiles WHERE user_id=?''', (user_id,))
            after_before_commit = c.fetchone()
            debug_log(f"AFTER (before commit) - Total: {after_before_commit['total_submissions']}, Pending: {after_before_commit['pending_submissions']}, Approved: {after_before_commit['approved_submissions']}, Rejected: {after_before_commit['rejected_submissions']}, Revoked: {after_before_commit['revoked_submissions']}")

            conn.commit()
            debug_log("Transaction committed successfully")

            c.execute('''SELECT total_submissions, pending_submissions, approved_submissions,
                                rejected_submissions, revoked_submissions
                         FROM user_profiles WHERE user_id=?''', (user_id,))
            after_commit = c.fetchone()
            debug_log(f"AFTER (after commit) - Total: {after_commit['total_submissions']}, Pending: {after_commit['pending_submissions']}, Approved: {after_commit['approved_submissions']}, Rejected: {after_commit['rejected_submissions']}, Revoked: {after_commit['revoked_submissions']}")

            debug_log(f"=== END UPDATE SUBMISSION STATS ===")

    except Exception as e:
        debug_log(f"Error updating submission stats: {str(e)}")
        import traceback
        debug_log(f"Traceback: {traceback.format_exc()}")

def get_user_profile(user_id):
    try:
        with profile_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM user_profiles WHERE user_id = ?''', (user_id,))
            result = c.fetchone()
            return dict(result) if result else None
    except Exception as e:
        debug_log(f"Error getting user profile: {str(e)}")
        return None

def handle_profile(update: Update, context: CallbackContext):
    try:
        user = update.effective_user
        user_id = user.id

        profile = get_user_profile(user_id)

        if not profile:
            profile = get_user_profile(user_id)

        profile_dict = profile or {}

        is_verified = check_verification_status(user_id)
        is_admin = user_id in ADMINS

        username_display = f"@{user.username}" if user.username else user.first_name
        first_name_display = user.first_name or "User"

        profile_html = [
            "===<b>✨ User Profile ✨</b>===",
            "",
            f"👤 <b>{html.escape(username_display)}</b>",
            f"📛 <b>{html.escape(first_name_display)}</b>",
            "",
            f"🆔 <b>User ID:</b> {user_id}",
            f"🌟 <b>Status:</b> {'Admin' if is_admin else 'Member'}",
            f"✅ <b>Verified:</b> {'Yes' if is_verified else 'No'}",
            f"🚫 <b>Banned:</b> {'Yes' if profile_dict.get('is_banned') else 'No'}",
            "",
            "=====<b>📊 Submissions</b>=====",
            "",
            f"📬 <b>Total:</b>     {profile_dict.get('total_submissions', 0)}",
            f"👍 <b>Approved:</b>  {profile_dict.get('approved_submissions', 0)}",
            f"👎 <b>Rejected:</b>  {profile_dict.get('rejected_submissions', 0)}",
            f"⏳ <b>Pending:</b>   {profile_dict.get('pending_submissions', 0)}",
            f"↩️ <b>Revoked:</b>   {profile_dict.get('revoked_submissions', 0)}",
            f"",
            f"====================="
        ]

        try:
            profile_photos = context.bot.get_user_profile_photos(user_id, limit=1)

            if profile_photos and profile_photos.total_count > 0:
                photo_file = profile_photos.photos[0][-1]  

                context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photo_file.file_id,
                    caption="\n".join(profile_html),
                    parse_mode='HTML'
                )
            else:
                update.message.reply_text(
                    "\n".join(profile_html),
                    parse_mode='HTML'
                )

        except telegram.error.BadRequest as e:
            if "user not found" in str(e).lower() or "bot was blocked" in str(e).lower():
                update.message.reply_text(
                    "\n".join(profile_html),
                    parse_mode='HTML'
                )
            else:
                debug_log(f"Error getting profile photos: {str(e)}")
                update.message.reply_text(
                    "\n".join(profile_html),
                    parse_mode='HTML'
                )

        except Exception as e:
            debug_log(f"Error getting profile photos: {str(e)}")
            update.message.reply_text(
                "\n".join(profile_html),
                parse_mode='HTML'
            )

    except Exception as e:
        debug_log(f"Error in /profile: {str(e)}")
        update.message.reply_text("❌ Error fetching profile. Please try again.")

@admin_only
def handle_admin_message(update: Update, context: CallbackContext):
    if not context.args or len(context.args) < 2:
        update.message.reply_text(
            "❌ Usage: /msg <user_id|username> <message>\n\n"
            "Examples:\n"
            "/msg 1234567890 Hello there!\n"
            "/msg @username This is a message"
        )
        return

    try:
        target = context.args[0]
        message_text = ' '.join(context.args[1:])

        if target.startswith('@'):
            username = target[1:]
            user_id = find_user_id_by_username(username)
            if not user_id:
                update.message.reply_text(f"❌ User @{username} not found in database")
                return
        else:
            try:
                user_id = int(target)
            except ValueError:
                update.message.reply_text("❌ Invalid user ID. Must be a number.")
                return

        try:
            context.bot.send_message(
                chat_id=user_id,
                text=f"📨 Message from admin:\n\n{message_text}"
            )
            update.message.reply_text(f"✅ Message sent to user {user_id}")

        except telegram.error.BadRequest as e:
            if "chat not found" in str(e).lower():
                update.message.reply_text(f"❌ User {user_id} has not started the bot or blocked it")
            else:
                update.message.reply_text(f"❌ Failed to send message: {str(e)}")

        except Exception as e:
            update.message.reply_text(f"❌ Error sending message: {str(e)}")

    except Exception as e:
        debug_log(f"Error in /msg: {str(e)}")
        update.message.reply_text("❌ Error processing command. Check logs.")

def find_user_id_by_username(username):
    try:
        with db_connection('verified_users.db') as conn:
            c = conn.cursor()
            c.execute('SELECT user_id FROM verified_users WHERE username = ?', (username,))
            result = c.fetchone()
            return result['user_id'] if result else None
    except Exception as e:
        debug_log(f"Error finding user by username: {str(e)}")
        return None

def handle_cleanup(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS:
        return

    try:
        with db_connection() as conn:
            conn.execute('''DELETE FROM submissions
                           WHERE status='rejected'
                           AND created_at < datetime('now', '-30 days')''')
            conn.commit()
        update.message.reply_text("✅ Database cleanup completed")
    except Exception as e:
        debug_log(f"Cleanup failed: {str(e)}")
        update.message.reply_text("❌ Cleanup failed")

def cancel_post_item(update: Update, context: CallbackContext):
    context.user_data.clear()
    update.message.reply_text(
        "🗑 Posting cancelled.\n"
        "You can start over with /add"
    )
    return ConversationHandler.END

@admin_only
def cleanup_old_auctions(update: Update, context: CallbackContext):
    try:
        with db_connection() as conn:
            inactive_auctions = conn.execute(
            ).fetchall()

        removed_count = 0
        failed_count = 0

        for auction in inactive_auctions:
            try:
                context.bot.edit_message_reply_markup(
                    chat_id=CHANNEL_ID,
                    message_id=auction['channel_message_id'],
                    reply_markup=None
                )
                removed_count += 1
            except telegram.error.BadRequest as e:
                if "message to edit not found" in str(e):
                    removed_count += 1
                else:
                    debug_log(f"Couldn't remove buttons from auction {auction['auction_id']}: {str(e)}")
                    failed_count += 1
            except Exception as e:
                debug_log(f"Error removing buttons from auction {auction['auction_id']}: {str(e)}")
                failed_count += 1

        update.message.reply_text(
            f"✅ Cleaned up {removed_count} old auctions\n"
            f"❌ Failed to clean {failed_count} auctions"
        )

    except Exception as e:
        debug_log(f"Error in cleanup_old_auctions: {str(e)}")
        update.message.reply_text("❌ Error cleaning up old auctions")

def error_handler(update: Update, context: CallbackContext):
    error = context.error
    debug_log(f"Error: {str(error)}\nUpdate: {update}\nContext: {context}")

    if isinstance(error, telegram.error.RetryAfter):
        debug_log(f"Flood control exceeded. Retry in {error.retry_after} seconds")
        return

    if isinstance(error, telegram.error.BadRequest):
        if "Query is too old" in str(error):
            debug_log("Query too old error - ignoring")
            return
        elif "Message is not modified" in str(error):
            debug_log("Message not modified - ignoring")
            return
        elif "Chat not found" in str(error):
            debug_log("Chat not found error - ignoring")
            return

    is_channel_error = False
    if update:
        try:
            if (hasattr(update, 'effective_chat') and
                update.effective_chat and
                update.effective_chat.type in ['channel', 'group']):
                is_channel_error = True
            elif (hasattr(update, 'callback_query') and
                  update.callback_query and
                  hasattr(update.callback_query, 'message') and
                  update.callback_query.message.chat.type in ['channel', 'group']):
                is_channel_error = True
        except Exception as e:
            debug_log(f"Error checking channel status: {str(e)}")

    if is_channel_error:
        debug_log("Error occurred in channel/group - suppressing error message")
        return

    try:
        if update and update.effective_message:
            if (hasattr(update.effective_chat, 'type') and
                update.effective_chat.type == 'private'):

                if isinstance(error, sqlite3.Error):
                    update.effective_message.reply_text("❌ Database error. Please try again later.")
                elif isinstance(error, telegram.error.NetworkError):
                    update.effective_message.reply_text("⚠️ Network issue. Please try again.")
                else:
                    update.effective_message.reply_text("❌ An unexpected error occurred.")

    except Exception as e:
        debug_log(f"Couldn't send error message: {str(e)}")

    if not isinstance(error, (telegram.error.RetryAfter, telegram.error.BadRequest)):
        error_message = f""

        if len(error_message) > 4000:
            error_message = error_message[:4000] + "..."

        for admin_id in ADMINS:
            try:
                context.bot.send_message(admin_id, error_message)
                break  
            except Exception as e:
                debug_log(f"Couldn't notify admin {admin_id}: {str(e)}")

def safe_reply(update: Update, message: str, **kwargs):
    try:
        if (hasattr(update, 'effective_chat') and
            update.effective_chat and
            update.effective_chat.type in ['private']):
            update.message.reply_text(message, **kwargs)
        else:
            debug_log(f"Attempted to send message to non-private chat: {message}")
    except Exception as e:
        debug_log(f"Error in safe_reply: {str(e)}")

def main():
    if not ensure_single_instance():
        sys.exit(1)

    try:
        init_db()
        init_verified_users_db()
        init_leaderboard_db()
        init_profiles_db()
        ensure_all_auctions_active()

        updater = Updater(token=TOKEN, use_context=True)
        dp = updater.dispatcher

        set_bot_commands(updater)

        try:
            bot = updater.bot
            chat = bot.get_chat(CHANNEL_ID)
            debug_log(f"Bot connected to channel: {chat.title}")
        except Exception as e:
            debug_log(f"FATAL: Channel access failed - {str(e)}")
            raise RuntimeError(f"Could not access channel {CHANNEL_ID}. Verify bot is admin.")

        dp.add_error_handler(error_handler)

        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("history", show_bid_history))
        dp.add_handler(CommandHandler("removebid", handle_remove_bid))
        dp.add_handler(CommandHandler("removeitem", remove_item))
        dp.add_handler(CommandHandler("items", handle_items))
        dp.add_handler(CommandHandler("myitems", handle_myitems))
        dp.add_handler(CommandHandler("mybids", handle_mybids))
        dp.add_handler(CommandHandler("endsubmission", end_submission))
        dp.add_handler(CommandHandler("startsubmission", start_submission))
        dp.add_handler(CommandHandler("startauction", start_auction))
        dp.add_handler(CommandHandler("endauction", end_auction))
        dp.add_handler(CommandHandler("verify_me", request_verification))
        dp.add_handler(CommandHandler("verify", verify_user))
        dp.add_handler(CommandHandler("unverify", remove_verification))
        dp.add_handler(CommandHandler("listverified", list_verified_users))
        dp.add_handler(CommandHandler("topbuyers", handle_topbuyers))
        dp.add_handler(CommandHandler("topsellers", handle_topsellers))
        dp.add_handler(CommandHandler("help", show_help))
        dp.add_handler(CommandHandler("broad", broadcast_message))
        dp.add_handler(CommandHandler("profile", handle_profile))
        dp.add_handler(CommandHandler("msg", handle_admin_message))
        dp.add_handler(CommandHandler("cleanup", handle_cleanup))
        dp.add_handler(CommandHandler("cleanup_auctions", cleanup_old_auctions))

        dp.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler('add', start_add)],
                states={
                    SELECT_CATEGORY: [CallbackQueryHandler(handle_category)],
                    GET_POKEMON_NAME: [MessageHandler(Filters.text & ~Filters.command, handle_pokemon_name)],
                    GET_NATURE: [MessageHandler(Filters.photo & Filters.forwarded, handle_nature)],
                    GET_IVS: [MessageHandler(Filters.photo & Filters.forwarded, handle_ivs)],
                    GET_MOVESET: [MessageHandler(Filters.photo & Filters.forwarded, handle_moveset)],
                    GET_BOOST_INFO: [MessageHandler(Filters.text & ~Filters.command, handle_boost_info)],
                    GET_TM_DETAILS: [MessageHandler(Filters.all & Filters.forwarded, handle_tm_details)],
                    GET_BASE_PRICE: [
                        MessageHandler(
                            Filters.text & ~Filters.command &
                            Filters.regex(r'(?i)^(base:)?\s*(\d+k?|\d{1,3}(,\d{3})*)$'),
                            handle_base_price
                        )
                    ]
                },
                fallbacks=[CommandHandler('cancel', cancel_post_item)],
                allow_reentry=True
            )
        )

        dp.add_handler(CallbackQueryHandler(handle_verification, pattern='^(verify|reject)_'))
        dp.add_handler(CallbackQueryHandler(handle_bid_button, pattern='^bid_'))
        dp.add_handler(MessageHandler(Filters.text & Filters.chat_type.private, handle_bid_amount))
        dp.add_handler(CallbackQueryHandler(handle_items_category_switch, pattern='^items_'))
        dp.add_handler(CallbackQueryHandler(handle_verified_pagination, pattern='^verified_'))


        debug_log("Bot starting with all features...")
        updater.start_polling()
        updater.idle()

    except Conflict:
        print("Error: Another instance is already polling updates")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()