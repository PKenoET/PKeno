import os
import sys
import logging
import asyncio
import random
import httpx # Added to requirements.txt
from datetime import datetime, timedelta
from typing import List, Optional

# Third-party libraries
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from telegram import Update, Message, Bot # Explicitly import Bot
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from redis import asyncio as aioredis
from dotenv import load_dotenv

# Local imports
from db_setup import init_db, create_db_and_tables, engine, User, Transaction, KenoRound, Bet

# --- Configuration & Initialization ---

# NOTE: load_dotenv() is ONLY for local development. Deployment relies on the environment.
load_dotenv()

# Environment Variables (CRITICAL for deployment)
BOT_TOKEN = os.getenv("BOT_TOKEN")
FASTAPI_PUBLIC_URL = os.getenv("FASTAPI_PUBLIC_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
REDIS_URL = os.getenv("REDIS_URL")

# Game Constants (Rest of constants remain the same)
GAME_INTERVAL_SECONDS = 60
MIN_BET_AMOUNT = 5.0
KENO_MAX_NUMBERS = 80
KENO_DRAW_COUNT = 20
KENO_MAX_PICKS = 10
ADMIN_ID = 557555000 

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Global State & Cache ---
redis_client: Optional[aioredis.Redis] = None
game_task: Optional[asyncio.Task] = None
game_loop_lock = asyncio.Lock()

# --- Utility Functions (No major changes here) ---

async def get_db_session() -> AsyncSession:
    """Dependency to get an async database session."""
    if not engine:
        raise HTTPException(status_code=500, detail="Database engine not initialized")
    async with AsyncSession(engine) as session:
        yield session

async def get_or_create_user(session: AsyncSession, tg_id: int, username: str) -> User:
    """Fetches or creates a user in the database."""
    statement = select(User).where(User.telegram_id == tg_id)
    result = await session.exec(statement)
    user = result.first()
    
    if not user:
        user = User(telegram_id=tg_id, username=username, vault_balance=0.0, playground_balance=0.0)
        # ... user creation logic
        session.add(user)
        await session.commit()
        await session.refresh(user)

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
            return datetime.fromisoformat(draw_time_str) # Removed .decode() as decode_responses=True is set
    return datetime.utcnow() + timedelta(seconds=GAME_INTERVAL_SECONDS)

# --- Core Game Logic ---

async def execute_keno_draw(session: AsyncSession, current_round_id: int) -> List[int]:
    """Simulates the Keno draw and saves the result."""
    
    # 1. Simple Random Draw
    winning_numbers = random.sample(range(1, KENO_MAX_NUMBERS + 1), KENO_DRAW_COUNT)
    winning_numbers.sort()
    
    # 2. Save the round result
    new_round = KenoRound(
        round_id=current_round_id,
        draw_time=datetime.utcnow(),
        winning_numbers=winning_numbers
    )
    session.add(new_round)
    await session.commit()
    
    return winning_numbers

# UPDATED: Changed context: ContextTypes.DEFAULT_TYPE to bot: Bot
async def settle_all_bets(session: AsyncSession, round_id: int, winning_numbers: List[int], bot: Bot):
    """
    Calculates the results for all bets in the finished round and pays out winners.
    """
    # ... rest of the function remains the same, BUT:
    # Change all instances of `await context.bot.send_message(...)` to `await bot.send_message(...)`
    
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
        
        # --- Placeholder Payout Logic ---
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
            
            win_tx = Transaction(
                user_id=bet.user_id,
                amount=payout_amount,
                type="WIN",
                status="COMPLETED",
                request_details=f'{{"bet_id": {bet.id}, "round_id": {round_id}}}'
            )
            session.add(win_tx)
            session.add(user)

        await session.commit()
        await session.refresh(user)
        
        # Send notification to user
        if payout_amount > 0:
            await bot.send_message( # FIX: Used 'bot' instead of 'context.bot'
                chat_id=bet.user_id,
                text=f"🥳 *Round {round_id} Result:* Your bet of {bet.amount:.2f} ETB on {selected_set} matched *{matched_count}* numbers!\n\n"
                     f"You won: *{payout_amount:.2f} ETB*! Your new Playground balance is {user.playground_balance:.2f} ETB.",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await bot.send_message( # FIX: Used 'bot' instead of 'context.bot'
                chat_id=bet.user_id,
                text=f"😔 *Round {round_id} Result:* Your bet of {bet.amount:.2f} ETB on {selected_set} matched *{matched_count}* numbers. Better luck next time!",
                parse_mode=ParseMode.MARKDOWN
            )

# UPDATED: Changed context: ContextTypes.DEFAULT_TYPE to bot: Bot
async def start_new_round(session: AsyncSession, bot: Bot):
    """Sets up the next round and informs users."""
    
    current_round_id = await get_current_round_id()
    new_round_id = current_round_id + 1
    next_draw_time = datetime.utcnow() + timedelta(seconds=GAME_INTERVAL_SECONDS)
    
    await redis_client.set('current_round_id', new_round_id)
    await redis_client.set('next_draw_time', next_draw_time.isoformat())
    
    logger.info(f"New round {new_round_id} started. Draw at {next_draw_time.strftime('%I:%M:%S %p')} UTC")


# UPDATED: Changed context: ContextTypes.DEFAULT_TYPE to bot: Bot
async def run_keno_game(bot: Bot):
    """The main continuous game loop, running in a background task."""
    logger.info("Keno Game Loop started.")
    
    await create_db_and_tables() 
    
    while True:
        try:
            now = datetime.utcnow()
            next_draw_time = await get_next_draw_time()
            
            if next_draw_time <= now:
                
                async with game_loop_lock:
                    current_round_id = await get_current_round_id()
                    logger.info(f"--- DRAW TIME HIT for Round {current_round_id} ---")

                    async for session in get_db_session():
                        # A. Execute Draw
                        winning_numbers = await execute_keno_draw(session, current_round_id)
                        
                        # B. Settle Bets for the finished round
                        await settle_all_bets(session, current_round_id, winning_numbers, bot) # FIX: Passed 'bot'
                        
                        # C. Start the next round
                        await start_new_round(session, bot) # FIX: Passed 'bot'
                        
                        break

            # 2. Wait for the next check
            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Error in game loop: {e}", exc_info=True)
            await asyncio.sleep(10)

# --- Telegram Bot Handlers (Minor changes only where context is passed to background functions) ---
# NOTE: /deposit and /withdraw notifications to admin need the 'bot' object, not 'context.bot' if run outside a handler.
# I've left them as is since they run inside a handler and 'context.bot' is available.

# ... start_command, profile_command, deposit_command, withdraw_command, transfer_command, play_command, handle_picks_and_bet, button_handler remain largely the same.

# --- FastAPI Application & Webhook Setup ---

app = FastAPI(title="Keno Telegram Bot API", version="1.0.0")
ptb_application = ApplicationBuilder().token(BOT_TOKEN).build()

# Add Handlers to PTB Application (FIXED THE NESTED ADD_HANDLER CALL)
ptb_application.add_handler(CommandHandler("start", start_command))
ptb_application.add_handler(CommandHandler("profile", profile_command))
ptb_application.add_handler(CommandHandler("deposit", deposit_command))
ptb_application.add_handler(CommandHandler("withdraw", withdraw_command))
ptb_application.add_handler(CommandHandler("transfer", transfer_command))
ptb_application.add_handler(CommandHandler("play", play_command))
ptb_application.add_handler(CallbackQueryHandler(button_handler))

# FIXED: Use filters.TEXT & ~filters.COMMAND for a generic text handler
ptb_application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_picks_and_bet))


@app.on_event("startup")
async def startup_event():
    """Initializes DB, Redis, and starts the game loop when FastAPI starts."""
    global redis_client, game_task
    
    # 1. Database Initialization & ArgumentError Check (CRITICAL DEPLOYMENT FIX)
    if not DATABASE_URL:
        logger.error("FATAL: DATABASE_URL not set.")
        sys.exit(1)
    
    # Check for the literal instructional string that caused the original error
    if "Select the keno-db service" in DATABASE_URL:
        logger.error("FATAL: DATABASE_URL is set to an instructional placeholder. Please configure your actual database URL in the environment variables.")
        sys.exit(1)

    init_db(DATABASE_URL)
    
    # 2. Redis Initialization
    if not REDIS_URL:
        logger.warning("REDIS_URL not set. Game state persistence is disabled.")
    else:
        # aioredis.from_url now correctly handles all URL formats
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        logger.info("Redis client connected.")
    
    # 3. Webhook Setup
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
        logger.error(f"Failed to set Telegram Webhook. Ensure BOT_TOKEN is correct and the public URL is accessible: {e}")
        sys.exit(1) # Exit if webhook setup fails

    # 4. Start the background Keno Game Loop
    # FIX: Pass the Bot object directly, not the job queue.
    game_task = asyncio.create_task(run_keno_game(ptb_application.bot))
    logger.info("Keno Game Loop background task scheduled.")


@app.on_event("shutdown")
# ... shutdown_event, home, and telegram_webhook remain the same
# ... (omitted for brevity, as they were correct)
