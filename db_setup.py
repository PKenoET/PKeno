from datetime import datetime
from typing import List, Optional

from sqlmodel import Field, SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncEngine
# Removed unnecessary Pydantic model_validator import

import json

# --- Database Configuration ---
# Removed the hardcoded DATABASE_URL default:
# DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost/keno_db"

# --- Models (No changes) ---
class User(SQLModel, table=True):
# ... (omitted)

class Transaction(SQLModel, table=True):
# ... (omitted)

class KenoRound(SQLModel, table=True):
# ... (omitted)

class Bet(SQLModel, table=True):
# ... (omitted)

# --- Initialization Functions ---

# Global variable to hold the engine instance
engine: Optional[AsyncEngine] = None

def init_db(database_url: str):
    """Initializes the async database engine."""
    global engine
    # Using python-dotenv is recommended for local development to manage secrets
    # The URL should start with 'postgresql+asyncpg'
    engine = AsyncEngine(create_engine(database_url, echo=False, pool_recycle=3600))
    print("Database engine initialized.")

async def create_db_and_tables():
# ... (omitted)

if __name__ == "__main__":
# ... (omitted)
