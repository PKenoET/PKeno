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
from telegram import Update, Message
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from sqlmodel import select, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession
# from redis import asyncio as aioredis  <-- REMOVED REDIS IMPORT
from dotenv import load_dotenv

# Local imports
from db_setup import init_db, create_db_and_tables, engine, User, Transaction, KenoRound, Bet

# --- Configuration & Initialization ---

load_dotenv()

# Environment Variables (CRITICAL for deployment)
BOT_TOKEN = os.getenv("BOT_TOKEN")
FASTAPI_PUBLIC_URL = os.getenv("FASTAPI_PUBLIC_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
# REDIS_URL = os.getenv("REDIS_URL") <-- REDIS URL IS NO LONGER NEEDED

# Game Constants
GAME_INTERVAL_SECONDS = 60 # Time between draws
MIN_BET_AMOUNT = 5.0
KENO_MAX_NUMBERS = 80
KENO_DRAW_COUNT = 20
KENO_MAX_PICKS = 10
ADMIN_ID = 557555000 # Placeholder: Replace with your actual Telegram User ID

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Global State (In-memory Fallback for Round Tracking) ---
# NOTE: In a single-instance deployment (like the Render free tier), this is fine.
# If you scale to multiple instances, this state will not be shared.
class GameState:
    current_round_id: int = 1
    next_draw_time: datetime = datetime.utcnow() + timedelta(seconds=GAME_INTERVAL_SECONDS)

GAME_STATE = GameState()
game_task: Optional[asyncio.Task] = None
game_loop_lock = asyncio.Lock()

# --- Utility Functions ---

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
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info(f"New user created: {tg_id}")

        if tg_id == ADMIN_ID:
             user.is_admin = True
             session.add(user)
             await session.commit()
             await session.refresh(user)
             logger.warning(f"Admin user assigned: {tg_id}")
    
    return user

async def initialize_game_state(session: AsyncSession):
    """Initializes GAME_STATE from the database on startup."""
    global GAME_STATE
    # Find the latest round ID
    latest_round_statement = select(KenoRound).order_by(KenoRound.round_id.desc()).limit(1)
    latest_round = (await session.exec(latest_round_statement)).first()
    
    if latest_round:
        GAME_STATE.current_round_id = latest_round.round_id
    else:
        GAME_STATE.current_round_id = 1
        
    # Set the next draw time based on the last known round or default interval
    GAME_STATE.next_draw_time = datetime.utcnow() + timedelta(seconds=GAME_INTERVAL_SECONDS)
    logger.info(f"Game State Initialized. Current Round: {GAME_STATE.current_round_id}")


async def get_current_round_id() -> int:
    """Fetches the current active round ID from in-memory state."""
    return GAME_STATE.current_round_id

async def get_next_draw_time() -> datetime:
    """Fetches the next scheduled draw time from in-memory state."""
    return GAME_STATE.next_draw_time

# --- Core Game Logic ---

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
        
        # --- Placeholder Payout Logic ---
        payout_multiplier = 0.0
        if matched_count >= 5:
            # Example: 5 matches wins 2x, 10 matches wins 100x
            payout_multiplier = matched_count * 2.0 
        
        payout_amount = bet.amount * payout_multiplier
        bet.payout_multiplier = payout_multiplier
        bet.payout_amount = payout_amount
        bet.is_settled = True
        
        session.add(bet)
        
        # Create WIN Transaction and update user balance
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
            session.add(user) # Save updated user balance

        await session.commit()
        await session.refresh(user)
        
        # Send notification to user (use try-except for sending messages)
        try:
            if payout_amount > 0:
                await context.bot.send_message(
                    chat_id=bet.user_id,
                    text=f"🥳 *Round {round_id} Result:* Your bet of {bet.amount:.2f} ETB matched *{matched_count}* numbers!\n\n"
                         f"You won: *{payout_amount:.2f} ETB*! Your new Playground balance is {user.playground_balance:.2f} ETB.",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await context.bot.send_message(
                    chat_id=bet.user_id,
                    text=f"😔 *Round {round_id} Result:* Your bet of {bet.amount:.2f} ETB matched *{matched_count}* numbers. Better luck next time!",
                    parse_mode=ParseMode.MARKDOWN
                )
        except Exception as msg_e:
            logger.error(f"Failed to send settlement message to user {bet.user_id}: {msg_e}")


async def start_new_round(session: AsyncSession, context: ContextTypes.DEFAULT_TYPE):
    """Updates in-memory state for the next round and informs users."""
    global GAME_STATE
    
    current_round_id = GAME_STATE.current_round_id
    new_round_id = current_round_id + 1
    next_draw_time = datetime.utcnow() + timedelta(seconds=GAME_INTERVAL_SECONDS)
    
    # Update in-memory state
    GAME_STATE.current_round_id = new_round_id
    GAME_STATE.next_draw_time = next_draw_time
    
    # Send broadcast message
    draw_time_str = next_draw_time.strftime("%I:%M:%S %p")
    message = (
        f"🔔 *New Keno Round Started!* (Round ID: {new_round_id})\n\n"
        f"Place your bets now! The draw will happen at *{draw_time_str}* (UTC). "
        f"Use /play to pick your numbers."
    )
    
    logger.info(f"New round {new_round_id} started. Draw at {draw_time_str}")
    

async def run_keno_game(context: ContextTypes.DEFAULT_TYPE):
    """The main continuous game loop, running in a background task."""
    logger.info("Keno Game Loop started.")
    
    # Initialization step
    async for session in get_db_session():
        await create_db_and_tables() 
        await initialize_game_state(session)
        break

    while True:
        try:
            now = datetime.utcnow()
            next_draw_time = GAME_STATE.next_draw_time
            
            if next_draw_time <= now:
                
                async with game_loop_lock:
                    current_round_id = GAME_STATE.current_round_id
                    logger.info(f"--- DRAW TIME HIT for Round {current_round_id} ---")

                    async for session in get_db_session():
                        # A. Execute Draw
                        winning_numbers = await execute_keno_draw(session, current_round_id)
                        
                        # B. Settle Bets for the finished round
                        await settle_all_bets(session, current_round_id, winning_numbers, context)
                        
                        # C. Start the next round (updates in-memory state)
                        await start_new_round(session, context)
                        
                        break

            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Error in game loop: {e}", exc_info=True)
            await asyncio.sleep(10)

# --- Telegram Bot Handlers (No changes needed below this point) ---
# ... (All other handlers like start_command, profile_command, deposit_command, etc., remain the same) ...

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
    
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Please use the format: `/deposit <AMOUNT>`. E.g., `/deposit 500`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a positive number.")
        return

    async for session in get_db_session():
        new_tx = Transaction(
            user_id=tg_id,
            amount=amount,
            type="DEPOSIT",
            status="PENDING",
            request_details=f'{{"method": "TBD-Mpesa/Bank", "user_note": ""}}'
        )
        session.add(new_tx)
        await session.commit()
        await session.refresh(new_tx)

        await update.message.reply_text(
            f"✅ *Deposit Request Submitted!* (TxID: {new_tx.id})\n\n"
            f"A deposit of *{amount:.2f} ETB* is pending administrative approval. Once approved, the funds will appear in your Vault.",
            parse_mode=ParseMode.MARKDOWN
        )

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🚨 *NEW PENDING DEPOSIT* (TxID: {new_tx.id})\nUser ID: `{tg_id}` requested: {amount:.2f} ETB. "
                 f"Use `/admin approve_deposit {new_tx.id}` to approve.",
            parse_mode=ParseMode.MARKDOWN
        )
        break

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initiates a withdrawal request from the Vault."""
    tg_id = update.effective_user.id
    
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Please use the format: `/withdraw <AMOUNT>`. E.g., `/withdraw 100`", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a positive number.")
        return

    async for session in get_db_session():
        user = (await session.exec(select(User).where(User.telegram_id == tg_id))).first()
        if not user or user.vault_balance < amount:
            await update.message.reply_text("Insufficient funds in your Vault. Use /profile to check your balance.")
            return

        user.vault_balance -= amount
        
        new_tx = Transaction(
            user_id=tg_id,
            amount=amount,
            type="WITHDRAW",
            status="PENDING",
            request_details=f'{{"method": "TBD-Bank", "amount": {amount}}}'
        )
        
        session.add(user)
        session.add(new_tx)
        await session.commit()
        await session.refresh(new_tx)

        await update.message.reply_text(
            f"✅ *Withdrawal Request Submitted!* (TxID: {new_tx.id})\n\n"
            f"*{amount:.2f} ETB* has been reserved from your Vault and is pending administrative processing. Your remaining Vault balance is {user.vault_balance:.2f} ETB.",
            parse_mode=ParseMode.MARKDOWN
        )

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🚨 *NEW PENDING WITHDRAWAL* (TxID: {new_tx.id})\nUser ID: `{tg_id}` requested: {amount:.2f} ETB. "
                 f"Process payment and then use `/admin complete_withdrawal {new_tx.id}`.",
            parse_mode=ParseMode.MARKDOWN
        )
        break

async def transfer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles fund transfers between Vault and Playground."""
    tg_id = update.effective_user.id
    
    if len(context.args) != 2:
        await update.message.reply_text(
            "Please use the format: `/transfer <AMOUNT> <vault|play>`. "
            "Example: `/transfer 100 vault` (moves 100 from Vault to Playground)",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        amount = float(context.args[0])
        source = context.args[1].lower()
        if amount <= 0 or source not in ['vault', 'play']:
            raise ValueError()
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
                if user.vault_balance < amount:
                    await update.message.reply_text("Insufficient funds in your Vault.")
                    return
                user.vault_balance -= amount
                user.playground_balance += amount
                tx_type_out, tx_type_in = "TRANSFER_OUT", "TRANSFER_IN"
                
            elif source == 'play':
                if user.playground_balance < amount:
                    await update.message.reply_text("Insufficient funds in your Playground balance.")
                    return
                user.playground_balance -= amount
                user.vault_balance += amount
                tx_type_out, tx_type_in = "TRANSFER_OUT", "TRANSFER_IN"

            session.add(user)
            
            tx_out = Transaction(user_id=tg_id, amount=amount, type=tx_type_out, status="COMPLETED", request_details=f'{{"from": "{source}"}}')
            tx_in = Transaction(user_id=tg_id, amount=amount, type=tx_type_in, status="COMPLETED", request_details=f'{{"to": "{ "play" if source == "vault" else "vault"}"}}')
            session.add(tx_out)
            session.add(tx_in)
            
            await session.commit()
            await session.refresh(user)
            
            await update.message.reply_text(
                f"✅ *Transfer Complete!* Moved *{amount:.2f} ETB* from {source.capitalize()} to {'Playground' if source == 'vault' else 'Vault'}.\n\n"
                f"Vault: {user.vault_balance:.2f} ETB\nPlayground: {user.playground_balance:.2f} ETB",
                parse_mode=ParseMode.MARKDOWN
            )

        except Exception as e:
            logger.error(f"Transfer failed for user {tg_id}: {e}", exc_info=True)
            await update.message.reply_text("An error occurred during the transfer. Funds were not moved.")
        
        break

async def play_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Starts the number selection process."""
    
    tg_id = update.effective_user.id
    async for session in get_db_session():
        user = (await session.exec(select(User).where(User.telegram_id == tg_id))).first()
        if not user or user.playground_balance < MIN_BET_AMOUNT:
            await update.message.reply_text(f"Your Playground balance is too low (Min Bet: {MIN_BET_AMOUNT} ETB). Deposit funds or /transfer funds from your Vault.")
            return
        
        round_id = await get_current_round_id()
        next_draw = await get_next_draw_time()
        time_left = next_draw - datetime.utcnow()
        
        if time_left.total_seconds() <= 5: 
            await update.message.reply_text("Sorry, the betting window for this round is closed. Please wait for the next round to start.")
            return

        context.user_data['state'] = 'PICKING_NUMBERS'
        context.user_data['picks'] = []
        context.user_data['bet_round_id'] = round_id

        message = (
            f"🔢 *Keno Round {round_id}: Pick your numbers.*\n"
            f"You can select up to *{KENO_MAX_PICKS}* numbers between 1 and {KENO_MAX_NUMBERS}.\n"
            f"Send a message with your selected numbers (e.g., `5 12 77 33`)."
        )

        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)
        break


async def handle_picks_and_bet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the user's number submission and final bet confirmation."""
    
    if context.user_data.get('state') != 'PICKING_NUMBERS':
        return

    try:
        raw_picks = update.message.text.split()
        picks = []
        for p in raw_picks:
            num = int(p)
            if 1 <= num <= KENO_MAX_NUMBERS and num not in picks:
                picks.append(num)
        
        picks.sort()

        if not (1 <= len(picks) <= KENO_MAX_PICKS):
            await update.message.reply_text(f"Invalid number of picks. Please select between 1 and {KENO_MAX_PICKS} unique numbers.")
            return
        
        round_id = context.user_data['bet_round_id']
        context.user_data['final_picks'] = picks
        context.user_data['state'] = 'CONFIRMING_BET'

        keyboard = [
            [InlineKeyboardButton(f"Bet {MIN_BET_AMOUNT:.2f} ETB", callback_data=f'confirm_bet:{MIN_BET_AMOUNT}'),
             InlineKeyboardButton("Cancel", callback_data='cancel_bet')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message = (
            f"📝 *Bet Confirmation* (Round {round_id})\n"
            f"Your Picks: *{', '.join(map(str, picks))}*\n"
            f"Number of Picks: *{len(picks)}*\n\n"
            f"How much do you want to bet? (Min Bet: {MIN_BET_AMOUNT:.2f} ETB)"
        )

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
        context.user_data.pop('state', None)
        context.user_data.pop('final_picks', None)
        await query.edit_message_text("❌ Bet cancelled. Use /play to start a new game.")
        return

    if data.startswith('confirm_bet:'):
        if context.user_data.get('state') != 'CONFIRMING_BET':
            await query.edit_message_text("Error: Betting window timed out or invalid state.")
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
                    await query.edit_message_text("❌ Bet failed: Betting window closed (Draw is too soon).")
                    return

                user.playground_balance -= amount
                
                new_bet = Bet(
                    user_id=tg_id,
                    round_id=round_id,
                    amount=amount,
                    selected_numbers=picks
                )
                
                bet_tx = Transaction(
                    user_id=tg_id,
                    amount=amount,
                    type="BET",
                    status="COMPLETED",
                    request_details=f'{{"round_id": {round_id}, "picks_count": {len(picks)}}}'
                )

                session.add(user)
                session.add(new_bet)
                session.add(bet_tx)
                await session.commit()
                await session.refresh(user)
                
                context.user_data.pop('state', None)
                context.user_data.pop('final_picks', None)
                
                await query.edit_message_text(
                    f"✅ *Bet Placed Successfully!* (Round {round_id})\n"
                    f"Amount: *{amount:.2f} ETB*\n"
                    f"Picks: *{', '.join(map(str, picks))}*\n\n"
                    f"Your new Playground Balance: {user.playground_balance:.2f} ETB.\n"
                    "Good luck! Results are coming soon.",
                    parse_mode=ParseMode.MARKDOWN
                )
                
                break

        except Exception as e:
            logger.error(f"Bet failed: {e}", exc_info=True)
            await query.edit_message_text("An error occurred while placing your bet. Please try again or check /profile.")


# --- FastAPI Application & Webhook Setup ---

app = FastAPI(title="Keno Telegram Bot API", version="1.0.0")
ptb_application = ApplicationBuilder().token(BOT_TOKEN).build()

ptb_application.add_handler(CommandHandler("start", start_command))
ptb_application.add_handler(CommandHandler("profile", profile_command))
ptb_application.add_handler(CommandHandler("deposit", deposit_command))
ptb_application.add_handler(CommandHandler("withdraw", withdraw_command))
ptb_application.add_handler(CommandHandler("transfer", transfer_command))
ptb_application.add_handler(CommandHandler("play", play_command))
ptb_application.add_handler(CallbackQueryHandler(button_handler))
# Use MessageHandler for all non-command text, handling the betting flow
ptb_application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_picks_and_bet))


@app.on_event("startup")
async def startup_event():
    """Initializes DB and starts the game loop when FastAPI starts."""
    global game_task
    
    if not DATABASE_URL:
        logger.error("FATAL: DATABASE_URL not set.")
        sys.exit(1)
        
    init_db(DATABASE_URL)
    logger.warning("NOTE: Redis is skipped. Game state is stored in-memory, suitable for single-instance free tier deployment.")
    
    if not FASTAPI_PUBLIC_URL:
        logger.error("FATAL: FASTAPI_PUBLIC_URL not set.")
        sys.exit(1)

    webhook_url = f"{FASTAPI_PUBLIC_URL}/webhook"
    
    import httpx
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

    # Start the background Keno Game Loop
    game_task = asyncio.create_task(run_keno_game(ptb_application.job_queue))
    logger.info("Keno Game Loop background task scheduled.")


@app.on_event("shutdown")
async def shutdown_event():
    """Stops the game loop on application shutdown."""
    if game_task:
        game_task.cancel()
        logger.info("Keno Game Loop background task cancelled.")


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
            <p>Status: Webhook is active and listening for Telegram updates.</p>
            <p>Webhook URL: <code>{FASTAPI_PUBLIC_URL}/webhook</code></p>
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