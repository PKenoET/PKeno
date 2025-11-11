from datetime import datetime
from typing import List, Optional

# NOTE: Added JSON to imports
from sqlmodel import Field, SQLModel, create_engine, Session
from sqlmodel.ext.asyncio.session import AsyncEngine
from pydantic import model_validator
import json

# --- Database Configuration ---
# Global variable to hold the engine instance
engine: Optional[AsyncEngine] = None

# --- Models (omitted for brevity, assume they are correct) ---
# (User, Transaction, KenoRound, Bet classes here...)

# For brevity, re-including the definitions of the models for the final code
class User(SQLModel, table=True):
    __tablename__ = 'users'
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True, unique=True)
    username: Optional[str] = None
    vault_balance: float = Field(default=0.0)
    playground_balance: float = Field(default=0.0)
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

class Transaction(SQLModel, table=True):
    __tablename__ = 'transactions'
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    amount: float
    type: str # DEPOSIT, WITHDRAW, TRANSFER_IN_V, TRANSFER_OUT_P, BET, WIN
    status: str # PENDING, COMPLETED, FAILED
    request_details: str = Field(default="{}") # JSON string
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

class KenoRound(SQLModel, table=True):
    __tablename__ = 'keno_rounds'
    id: Optional[int] = Field(default=None, primary_key=True)
    round_id: int = Field(index=True, unique=True)
    draw_time: datetime
    winning_numbers: List[int] = Field(default=[], sa_column_kwargs={"type": json.loads}) # Stored as JSON string
    is_settled: bool = Field(default=False)
    
    @model_validator(mode="before")
    def validate_numbers(cls, values):
        # Convert List[int] to JSON string before storing
        if isinstance(values.get('winning_numbers'), list):
            values['winning_numbers'] = json.dumps(values['winning_numbers'])
        return values
    
    @property
    def winning_numbers(self) -> List[int]:
        # Getter to convert JSON string back to List[int]
        return json.loads(self.sa_data.get("winning_numbers", "[]"))

class Bet(SQLModel, table=True):
    __tablename__ = 'bets'
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    round_id: int = Field(index=True)
    amount: float
    selected_numbers: List[int] = Field(default=[], sa_column_kwargs={"type": json.loads})
    matched_count: Optional[int] = Field(default=None)
    payout_multiplier: Optional[float] = Field(default=None)
    payout_amount: Optional[float] = Field(default=None)
    is_settled: bool = Field(default=False)
    bet_time: datetime = Field(default_factory=datetime.utcnow, nullable=False)

    @model_validator(mode="before")
    def validate_numbers(cls, values):
        if isinstance(values.get('selected_numbers'), list):
            values['selected_numbers'] = json.dumps(values['selected_numbers'])
        return values
    
    @property
    def selected_numbers(self) -> List[int]:
        return json.loads(self.sa_data.get("selected_numbers", "[]"))


# --- Functions ---

def init_db(database_url: str):
    """Initializes the async database engine."""
    global engine
    # CRITICAL FIX: Add connect_args for reliable connection pooling and concurrency
    engine = AsyncEngine(
        create_engine(
            database_url, 
            echo=False, 
            pool_recycle=3600,
            # Use connect_args to prevent unexpected closed connections during long idle times
            connect_args={"server_settings": {"jit": "off"}} 
        )
    )
    print("Database engine initialized.")

async def create_db_and_tables():
    """Creates all tables defined in the SQLModel classes if they don't exist."""
    print("Attempting to create database tables...")
    if engine:
        async with engine.begin() as conn:
            # Drop tables for fresh start (optional, comment out for production)
            # await conn.run_sync(SQLModel.metadata.drop_all)
            
            # Create all tables
            await conn.run_sync(SQLModel.metadata.create_all)
            print("Database tables created successfully.")
    else:
        # This message will only appear if init_db() was never called or failed silently
        print("Error: Database engine not initialized.")

if __name__ == "__main__":
    import asyncio
    import os
    from dotenv import load_dotenv

    load_dotenv()
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        init_db(db_url)
        asyncio.run(create_db_and_tables())
