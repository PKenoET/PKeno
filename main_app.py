import os
import sys
import logging
import asyncio
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# Third-party libraries
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
# CRITICAL: Added MessageHandler and filters to the import
from telegram import Update, Message
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters, PicklePersistence 
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from redis import asyncio as aioredis
from dotenv import load_dotenv
import httpx 

# Local imports
# Ensure db_setup.py is accessible in the same directory
# Assuming db_setup.py remains unchanged and accessible
from db_setup import init_db, create_db_and_tables, engine, User, Transaction, KenoRound, Bet

# --- Configuration & Initialization ---

load_dotenv() # Load .env file for local development

# Environment Variables (CRITICAL for deployment)
BOT_TOKEN = os.getenv("BOT_TOKEN")
FASTAPI_PUBLIC_URL = os.getenv("FASTAPI_PUBLIC_URL") # e.g., https://your-railway-domain.up.railway.app
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")

# Game Constants
GAME_INTERVAL_SECONDS = 60 # Time between draws
MIN_BET_AMOUNT = 5.0
KENO_MAX_NUMBERS = 80
KENO_DRAW_COUNT = 20
KENO_MAX_PICKS = 10
ADMIN_ID = 557555000 # Placeholder: Replace with your actual Telegram User ID (Used 557555000 from your file)

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Global State & Cache ---
redis_client: Optional[aioredis.Redis] = None
game_task: Optional[asyncio.Task] = None
game_loop_lock = asyncio.Lock() # Internal lock for game loop control

# --- Utility Functions ---

async def get_db_session() -> AsyncSession:
    """Dependency to get an async database session."""
    if not engine:
        # CRITICAL FIX: Raise a simpler RuntimeError in background tasks
        raise RuntimeError("Database engine not initialized. Check startup logs.")
    async with AsyncSession(engine) as session:
        yield session

async def get_or_create_user(session: AsyncSession, tg_id: int, username: str) -> User:
    """Fetches or creates a user in the database."""
    statement = select(User).where(User.telegram_id == tg_id)
    result = await session.exec(statement)
    user = result.first()
    
    if not user:
        # Create a new user with default balances
        user = User(telegram_id=tg_id, username=username, vault_balance=0.0, playground_balance=0.0)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info(f"New user created: {tg_id}")

        # Make the defined ADMIN_ID an admin upon creation
        if tg_id == ADMIN_ID:
             user.is_admin = True
             session.add(user)
             await session.commit()
             await session.refresh(user)
             logger.warning(f"Admin user assigned: {tg_id}")
    
    return user

async def get_current_round_id() -> int:
    """Fetches the current active round ID from Redis."""
    if redis_client:
        round_id = await redis_client.get('current_round_id')
        return int(round_id) if round_id else 1
    return 1 # Fallback to 1 if Redis is unavailable

async def get_next_draw_time() -> datetime:
    """Fetches the next scheduled draw time from Redis."""
    if redis_client:
        draw_time_str = await redis_client.get('next_draw_time')
        if draw_time_str:
            # FIX: No .decode() needed if decode_responses=True is used on aioredis client
            return datetime.fromisoformat(draw_time_str) 
    return datetime.utcnow() + timedelta(seconds=GAME_INTERVAL_SECONDS)

# --- Core Game Logic (Retained from original for simplicity) ---

async def execute_keno_draw(session: AsyncSession, current_round_id: int) -> List[int]:
    """Simulates the Keno draw and saves the result."""
    winning_numbers = random.sample(range(1, KENO_MAX_NUMBERS + 1), KENO_DRAW_COUNT)
    winning_numbers.sort()
    new_round = KenoRound(
        round_id=current_round_id,
        draw_time=datetime.utcnow(),
        winning_numbers=winning_numbers
    )
    session.add(new_round)
    await session.commit()
    return winning_numbers

async def settle_all_bets(session: AsyncSession, round_id: int, winning_numbers: List[int], context: ContextTypes.DEFAULT_TYPE):
    """Calculates the results for all bets in the finished round and pays out winners."""
    bet_statement = select(Bet).where(Bet.round_id == round_id, Bet.is_settled == False)
    bets = (await session.exec(bet_statement)).all()
    
    if not bets:
        logger.info(f"Round {round_id}: No bets to settle.")
        return

    for bet in bets:
        user_statement = select(User).where(User.telegram_id == bet.user_id)
        user = (await session.exec(user_statement)).first()
        if not user:
            logger.error(f"User {bet.user_id} not found for bet {bet.id}")
            continue

        selected_set = set(bet.selected_numbers)
        winning_set = set(winning_numbers)
        matched_count = len(selected_set.intersection(winning_set))
        bet.matched_count = matched_count
        payout_multiplier = 0.0
        if matched_count >= 5:
            payout_multiplier = matched_count * 2.0 
        payout_amount = bet.amount * payout_multiplier
        bet.payout_multiplier = payout_multiplier
        bet.payout_amount = payout_amount
        bet.is_settled = True
        session.add(bet)
        
        if payout_amount > 0:
            user.playground_balance += payout_amount 
            win_tx = Transaction(user_id=bet.user_id, amount=payout_amount, type="WIN", status="COMPLETED", request_details=f'{{"bet_id": {bet.id}, "round_id": {round_id}}}')
            session.add(win_tx)
            session.add(user)

        await session.commit()
        await session.refresh(user)
        
        # Send notification to user (simplified logic)

async def start_new_round(session: AsyncSession, context: ContextTypes.DEFAULT_TYPE):
    """Sets up the next round and informs users."""
    current_round_id = await get_current_round_id()
    new_round_id = current_round_id + 1
    next_draw_time = datetime.utcnow() + timedelta(seconds=GAME_INTERVAL_SECONDS)
    
    await redis_client.set('current_round_id', new_round_id)
    await redis_client.set('next_draw_time', next_draw_time.isoformat())
    
    draw_time_str = next_draw_time.strftime("%I:%M:%S %p")
    logger.info(f"New round {new_round_id} started. Draw at {draw_time_str}")
    
async def run_keno_game(context: ContextTypes.DEFAULT_TYPE):
    """The main continuous game loop, running in a background task."""
    logger.info("Keno Game Loop started.")
    
    # Ensure tables are created on startup (idempotent)
    await create_db_and_tables() 
    
    while True:
        try:
            now = datetime.utcnow()
            next_draw_time = await get_next_draw_time()
            
            # 1. Check if it's time for a draw
            if next_draw_time <= now:
                async with game_loop_lock:
                    current_round_id = await get_current_round_id()
                    logger.info(f"--- DRAW TIME HIT for Round {current_round_id} ---")

                    # Execute the draw and settlement inside a single DB session
                    async for session in get_db_session():
                        winning_numbers = await execute_keno_draw(session, current_round_id)
                        await settle_all_bets(session, current_round_id, winning_numbers, context)
                        await start_new_round(session, context)
                        break 

            # 2. Wait for the next check (e.g., check every 5 seconds)
            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Error in game loop: {e}", exc_info=True)
            # CRITICAL FIX: If the database engine is the problem, wait longer to avoid thrashing
            if "Database engine not initialized" in str(e):
                logger.critical("Database engine missing. Game loop cannot proceed. Waiting 60s for re-initialization...")
                await asyncio.sleep(60) 
            else:
                await asyncio.sleep(10) # Wait longer after a normal error

# --- Telegram Bot Handlers ---

# CRITICAL FIX: Add a generic error handler to prevent the "No error handlers registered" error
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a user-friendly message."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "🚨 *An unexpected error occurred.* The administrator has been notified. Please try again later.",
            parse_mode=ParseMode.MARKDOWN
        )

# CRITICAL FIX: Add the missing admin command handler, referenced in deposit/withdraw
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles admin commands like approving deposits and completing withdrawals."""
    tg_id = update.effective_user.id
    
    if tg_id != ADMIN_ID:
        await update.message.reply_text("🚫 Access Denied.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Admin commands:\n`/admin approve_deposit <TxID>`\n`/admin complete_withdrawal <TxID>`")
        return

    command = context.args[0].lower()
    try:
        tx_id = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Invalid Transaction ID.")
        return

    async for session in get_db_session():
        tx = (await session.exec(select(Transaction).where(Transaction.id == tx_id))).first()

        if not tx:
            await update.message.reply_text(f"Transaction with ID {tx_id} not found.")
            break

        if command == 'approve_deposit':
            if tx.type != "DEPOSIT" or tx.status != "PENDING":
                await update.message.reply_text(f"Tx {tx_id} is not a PENDING DEPOSIT.")
                break
            user = (await session.exec(select(User).where(User.telegram_id == tx.user_id))).first()
            if not user:
                await update.message.reply_text(f"User {tx.user_id} not found.")
                break
            tx.status = "COMPLETED"
            user.vault_balance += tx.amount
            session.add(tx)
            session.add(user)
            await session.commit()
            await update.message.reply_text(f"✅ DEPOSIT {tx_id} APPROVED.")
        
        elif command == 'complete_withdrawal':
            if tx.type != "WITHDRAW" or tx.status != "PENDING":
                await update.message.reply_text(f"Tx {tx_id} is not a PENDING WITHDRAWAL.")
                break
            tx.status = "COMPLETED"
            session.add(tx)
            await session.commit()
            await update.message.reply_text(f"✅ WITHDRAWAL {tx_id} COMPLETED.")
        
        else:
            await update.message.reply_text(f"Unknown admin command: {command}")
        break


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends a welcome message and creates the user profile."""
    tg_id = update.effective_user.id
    username = update.effective_user.username or f"User{tg_id}"
    
    async for session in get_db_session():
        user = await get_or_create_user(session, tg_id, username)
        
        welcome_message = (
            f"👋 *እንኳን ደህና መጡ! Welcome to ፐ ኬኖ!* (Telegram ID: `{tg_id}`)\n\n"
            "This is a high-speed Keno game. Please use the commands below to manage your account and play.\n\n"
            "💰 /profile - Check your balances and the next draw time.\n"
            "🕹️ /play - Start a new game and pick your numbers.\n"
            "📥 /deposit - Request to add funds to your Vault.\n"
            "📤 /withdraw - Request to cash out from your Vault.\n"
            "🔄 /transfer - Move funds between Vault and Playground.\n"
        )
        await update.message.reply_text(welcome_message, parse_mode=ParseMode.MARKDOWN)
        break

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays user balances and game status."""
    tg_id = update.effective_user.id
    
    async for session in get_db_session():
        user = (await session.exec(select(User).where(User.telegram_id == tg_id))).first()
        if not user:
            await update.message.reply_text("Please use /start first to register.")
            return

        next_draw_time = await get_next_draw_time()
        round_id = await get_current_round_id()
        time_left = next_draw_time - datetime.utcnow()
        time_left_str = str(timedelta(seconds=int(time_left.total_seconds())))

        profile_message = (
            f"👤 *Account Profile* (ID: `{tg_id}`)\n"
            f"--- Balances ---\n"
            f"🏦 *Vault Balance (Cold Storage):* {user.vault_balance:.2f} ETB\n"
            f"🕹️ *Playground Balance (Active Funds):* {user.playground_balance:.2f} ETB\n"
            f"--- Game Status ---\n"
            f"🔢 *Current Round:* {round_id}\n"
            f"⏱️ *Time Remaining:* {time_left_str}\n\n"
            "Use /play to bet now!"
        )
        
        await update.message.reply_text(profile_message, parse_mode=ParseMode.MARKDOWN)
        break

async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiates a deposit request for Admin approval."""
    tg_id = update.effective_user.id
    if not context.args or not context.args[0].replace('.', '', 1).isdigit():
        await update.message.reply_text("Please use the format: `/deposit <AMOUNT>`. E.g., `/deposit 500`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        amount = float(context.args[0])
        if amount <= 0: raise ValueError()
    except ValueError:
        await update.message.reply_text("Invalid amount.")
        return
    async for session in get_db_session():
        new_tx = Transaction(user_id=tg_id, amount=amount, type="DEPOSIT", status="PENDING", request_details=f'{{"method": "TBD-Mpesa/Bank", "user_note": ""}}')
        session.add(new_tx)
        await session.commit()
        await session.refresh(new_tx)
        await update.message.reply_text(f"✅ *Deposit Request Submitted!* (TxID: {new_tx.id})", parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 *NEW PENDING DEPOSIT* (TxID: {new_tx.id})", parse_mode=ParseMode.MARKDOWN)
        break

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiates a withdrawal request from the Vault."""
    tg_id = update.effective_user.id
    if not context.args or not context.args[0].replace('.', '', 1).isdigit():
        await update.message.reply_text("Please use the format: `/withdraw <AMOUNT>`. E.g., `/withdraw 100`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        amount = float(context.args[0])
        if amount <= 0: raise ValueError()
    except ValueError:
        await update.message.reply_text("Invalid amount.")
        return
    async for session in get_db_session():
        user = (await session.exec(select(User).where(User.telegram_id == tg_id))).first()
        if not user or user.vault_balance < amount:
            await update.message.reply_text("Insufficient funds in your Vault.")
            return
        user.vault_balance -= amount
        new_tx = Transaction(user_id=tg_id, amount=amount, type="WITHDRAW", status="PENDING", request_details=f'{{"method": "TBD-Bank", "amount": {amount}}}')
        session.add(user)
        session.add(new_tx)
        await session.commit()
        await session.refresh(new_tx)
        await update.message.reply_text(f"✅ *Withdrawal Request Submitted!* (TxID: {new_tx.id})", parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"🚨 *NEW PENDING WITHDRAWAL* (TxID: {new_tx.id})", parse_mode=ParseMode.MARKDOWN)
        break

async def transfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles fund transfers between Vault and Playground."""
    tg_id = update.effective_user.id
    if len(context.args) != 2 or not context.args[0].replace('.', '', 1).isdigit():
        await update.message.reply_text("Please use the format: `/transfer <AMOUNT> <vault|play>`.", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        amount = float(context.args[0])
        source = context.args[1].lower()
        if amount <= 0 or source not in ['vault', 'play']: raise ValueError()
    except ValueError:
        await update.message.reply_text("Invalid amount or source wallet. Use `vault` or `play`.")
        return
    async for session in get_db_session():
        user = (await session.exec(select(User).where(User.telegram_id == tg_id))).first()
        if not user:
            await update.message.reply_text("User not found. Please /start.")
            return

        try:
            if source == 'vault':
                if user.vault_balance < amount: await update.message.reply_text("Insufficient funds in your Vault."); return
                user.vault_balance -= amount; user.playground_balance += amount; tx_type_out, tx_type_in = "TRANSFER_OUT_V", "TRANSFER_IN_P"
            elif source == 'play':
                if user.playground_balance < amount: await update.message.reply_text("Insufficient funds in your Playground balance."); return
                user.playground_balance -= amount; user.vault_balance += amount; tx_type_out, tx_type_in = "TRANSFER_OUT_P", "TRANSFER_IN_V"
            
            session.add(user)
            tx_out = Transaction(user_id=tg_id, amount=amount, type=tx_type_out, status="COMPLETED", request_details=f'{{"from": "{source}"}}')
            tx_in = Transaction(user_id=tg_id, amount=amount, type=tx_type_in, status="COMPLETED", request_details=f'{{"to": "{ "play" if source == "vault" else "vault"}"}}')
            session.add(tx_out); session.add(tx_in)
            await session.commit(); await session.refresh(user)
            await update.message.reply_text(f"✅ *Transfer Complete!* Moved *{amount:.2f} ETB*.", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            logger.error(f"Transfer failed for user {tg_id}: {e}", exc_info=True)
            await update.message.reply_text("An error occurred during the transfer.")
        break

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts the number selection process."""
    tg_id = update.effective_user.id
    async for session in get_db_session():
        user = (await session.exec(select(User).where(User.telegram_id == tg_id))).first()
        if not user or user.playground_balance < MIN_BET_AMOUNT:
            await update.message.reply_text(f"Your Playground balance is too low (Min Bet: {MIN_BET_AMOUNT} ETB).")
            return
        round_id = await get_current_round_id()
        next_draw = await get_next_draw_time()
        if (next_draw - datetime.utcnow()).total_seconds() <= 5: 
            await update.message.reply_text("Sorry, the betting window for this round is closed.")
            return

        context.user_data['state'] = 'PICKING_NUMBERS'
        context.user_data['picks'] = []
        context.user_data['bet_round_id'] = round_id
        await update.message.reply_text(f"🔢 *Keno Round {round_id}: Pick up to {KENO_MAX_PICKS} numbers (1-{KENO_MAX_NUMBERS}).*", parse_mode=ParseMode.MARKDOWN)
        break

async def handle_picks_and_bet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the user's number submission and final bet confirmation."""
    if context.user_data.get('state') != 'PICKING_NUMBERS': return
    try:
        raw_picks = update.message.text.split()
        picks = []
        for p in raw_picks:
            num = int(p)
            if 1 <= num <= KENO_MAX_NUMBERS and num not in picks: picks.append(num)
        picks.sort()
        if not (1 <= len(picks) <= KENO_MAX_PICKS):
            await update.message.reply_text(f"Invalid number of picks. Please select between 1 and {KENO_MAX_PICKS} unique numbers.")
            return
        
        round_id = context.user_data['bet_round_id']
        context.user_data['final_picks'] = picks
        context.user_data['state'] = 'CONFIRMING_BET'

        keyboard = [[InlineKeyboardButton(f"Bet {MIN_BET_AMOUNT:.2f} ETB", callback_data=f'confirm_bet:{MIN_BET_AMOUNT}'), InlineKeyboardButton("Cancel", callback_data='cancel_bet')]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = (f"📝 *Bet Confirmation* (Round {round_id})\nYour Picks: *{', '.join(map(str, picks))}*")
        await update.message.reply_text(message, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("Please send valid numbers separated by spaces (e.g., `5 12 77 33`).")
        context.user_data['state'] = 'PICKING_NUMBERS'
        

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles inline button presses (e.g., bet confirmation/cancellation)."""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == 'cancel_bet':
        context.user_data.pop('state', None); context.user_data.pop('final_picks', None); context.user_data.pop('bet_round_id', None)
        await query.edit_message_text("❌ Bet cancelled.")
        return

    if data.startswith('confirm_bet:'):
        if context.user_data.get('state') != 'CONFIRMING_BET':
            await query.edit_message_text("Error: Betting window timed out.")
            return

        try:
            amount = float(data.split(':')[1])
            picks = context.user_data['final_picks']
            round_id = context.user_data['bet_round_id']
            tg_id = query.from_user.id
            
            async for session in get_db_session():
                user = (await session.exec(select(User).where(User.telegram_id == tg_id))).first()
                
                if not user or user.playground_balance < amount:
                    await query.edit_message_text("❌ Bet failed: Insufficient Playground balance.")
                    return

                next_draw = await get_next_draw_time()
                if (next_draw - datetime.utcnow()).total_seconds() <= 5:
                    await query.edit_message_text("❌ Bet failed: Betting window closed.")
                    return

                # Deduct & Log
                user.playground_balance -= amount
                new_bet = Bet(user_id=tg_id, round_id=round_id, amount=amount, selected_numbers=picks)
                bet_tx = Transaction(user_id=tg_id, amount=amount, type="BET", status="COMPLETED", request_details=f'{{"round_id": {round_id}, "picks_count": {len(picks)}}}')

                session.add(user); session.add(new_bet); session.add(bet_tx)
                await session.commit(); await session.refresh(user)
                
                # Clear user state
                context.user_data.pop('state', None); context.user_data.pop('final_picks', None); context.user_data.pop('bet_round_id', None)
                
                await query.edit_message_text(f"✅ *Bet Placed Successfully!* (Round {round_id})", parse_mode=ParseMode.MARKDOWN)
                break 

        except Exception as e:
            logger.error(f"Bet failed: {e}", exc_info=True)
            await query.edit_message_text("An error occurred while placing your bet.")


# --- FastAPI Application & Webhook Setup ---

app = FastAPI(title="Keno Telegram Bot API", version="1.0.0")
ptb_application = ApplicationBuilder().token(BOT_TOKEN).build()

# Add Handlers to PTB Application
ptb_application.add_handler(CommandHandler("start", start_command))
ptb_application.add_handler(CommandHandler("profile", profile_command))
ptb_application.add_handler(CommandHandler("deposit", deposit_command))
ptb_application.add_handler(CommandHandler("withdraw", withdraw_command))
ptb_application.add_handler(CommandHandler("transfer", transfer_command))
ptb_application.add_handler(CommandHandler("play", play_command))
ptb_application.add_handler(CommandHandler("admin", admin_command)) # Added admin command
ptb_application.add_handler(CallbackQueryHandler(button_handler))
# CRITICAL FIX: Correct MessageHandler registration
ptb_application.add_handler(MessageHandler(filters.TEXT & ~(filters.COMMAND), handle_picks_and_bet))
ptb_application.add_error_handler(error_handler) # CRITICAL FIX: Added error handler

@app.on_event("startup")
async def startup_event():
    """Initializes DB, Redis, and starts the game loop when FastAPI starts."""
    global redis_client, game_task
    
    # 1. Database Initialization (CRITICAL FIX: Added try/except)
    if not DATABASE_URL:
        logger.error("FATAL: DATABASE_URL not set.")
        sys.exit(1)
        
    try:
        init_db(DATABASE_URL)
        logger.info("Database engine initialization successful.")
    except Exception as e:
        logger.error(f"FATAL: Database engine initialization failed: {e}", exc_info=True)
        # Halt startup if DB setup fails
        sys.exit(1) 
    
    # 2. Redis Initialization (Connect to Railway Redis)
    if not REDIS_URL:
        logger.warning("REDIS_URL not set. Game state persistence is disabled.")
    else:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        logger.info("Redis client connected.")

    # 3. PTB Application Initialization (CRITICAL FIX: Required before starting tasks/webhooks)
    await ptb_application.initialize()
    
    # 4. Webhook Setup
    if not FASTAPI_PUBLIC_URL:
        logger.error("FATAL: FASTAPI_PUBLIC_URL not set.")
        sys.exit(1)

    webhook_url = f"{FASTAPI_PUBLIC_URL}/webhook"
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={"url": webhook_url}
            )
            response.raise_for_status()
            logger.info(f"Telegram Webhook set successfully to: {webhook_url}")
    except Exception as e:
        logger.error(f"Failed to set Telegram Webhook: {e}")

    # 5. Start the background Keno Game Loop
    game_task = asyncio.create_task(run_keno_game(ptb_application.job_queue))
    logger.info("Keno Game Loop background task scheduled.")


@app.on_event("shutdown")
async def shutdown_event():
    """Stops the game loop on application shutdown."""
    if game_task:
        game_task.cancel()
        logger.info("Keno Game Loop background task cancelled.")
    
    if redis_client:
        await redis_client.close()
        logger.info("Redis client closed.")


@app.get("/", response_class=HTMLResponse)
async def home():
    """Simple health check endpoint."""
    return f"""
    <html>
        <head>
            <title>Keno Bot Status</title>
        </head>
        <body>
            <h1>Keno Telegram Bot (ፐ ኬኖ) is RUNNING</h1>
        </body>
    </html>
    """

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """The main endpoint that receives updates from Telegram."""
    try:
        update_json = await request.json()
        update = Update.de_json(update_json, ptb_application.bot)
        await ptb_application.process_update(update)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Error processing webhook update: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
