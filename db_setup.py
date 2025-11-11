from datetime import datetime
from typing import List, Optional

from sqlmodel import Field, SQLModel, create_engine, Session
from pydantic import model_validator
import json

# --- FIX: Changed import path from sqlmodel.ext.asyncio.session ---
# This is the correct, robust path for AsyncEngine in modern SQLAlchemy/SQLModel.
from sqlalchemy.ext.asyncio import AsyncEngine 

# Global variable to hold the engine instance
engine: Optional[AsyncEngine] = None

# --- Models ---

class User(SQLModel, table=True):
    """Stores user wallets and admin status."""
    __tablename__ = 'users'

    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int = Field(index=True, unique=True)
    username: Optional[str] = None
    vault_balance: float = Field(default=0.0)
    playground_balance: float = Field(default=0.0)
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)


class Transaction(SQLModel, table=True):
    """Detailed audit log for every financial movement."""
    __tablename__ = 'transactions'

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True) 
    amount: float
    type: str 
    status: str = Field(default="PENDING")
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)
    request_details: str = Field(default_factory=lambda: "{}")


class KenoRound(SQLModel, table=True):
    """Stores the history of Keno game rounds."""
    __tablename__ = 'keno_rounds'

    id: Optional[int] = Field(default=None, primary_key=True)
    round_id: int = Field(index=True, unique=True)
    draw_time: datetime
    winning_numbers_json: str = Field(default_factory=lambda: "[]")

    @property
    def winning_numbers(self) -> List[int]:
        """Converts the stored JSON string back to a list of integers."""
        return json.loads(self.winning_numbers_json)

    @winning_numbers.setter
    def winning_numbers(self, numbers: List[int]):
        """Converts the list of integers to a JSON string for storage."""
        self.winning_numbers_json = json.dumps(numbers)


class Bet(SQLModel, table=True):
    """Records individual user bets."""
    __tablename__ = 'bets'

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    round_id: int = Field(index=True)
    amount: float
    selected_numbers_json: str = Field(default_factory=lambda: "[]")
    
    matched_count: int = Field(default=0)
    payout_multiplier: float = Field(default=0.0)
    payout_amount: float = Field(default=0.0)
    is_settled: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow, nullable=False)

    @property
    def selected_numbers(self) -> List[int]:
        """Converts the stored JSON string back to a list of integers."""
        return json.loads(self.selected_numbers_json)

    @selected_numbers.setter
    def selected_numbers(self, numbers: List[int]):
        """Converts the list of integers to a JSON string for storage."""
        self.selected_numbers_json = json.dumps(numbers)


# --- Initialization Functions ---

def init_db(database_url: str):
    """Initializes the async database engine."""
    global engine
    # NOTE: The URL should start with 'postgresql+asyncpg' for this setup to work.
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
            await conn.run_sync(SQLModel.metadata.create_all)
            print("Database tables created successfully.")
    else:
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
    else:
        print("Please set the DATABASE_URL environment variable.")
