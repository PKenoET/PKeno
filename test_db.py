# Create this new file named test_db.py

import os
import sys
import asyncio
from dotenv import load_dotenv

# Import your setup functions
from db_setup import init_db, create_db_and_tables

# --- Environment Setup ---
# Load your environment variables, including DATABASE_URL
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("FATAL: DATABASE_URL environment variable is not set.")
    sys.exit(1)

# Ensure the URL is in the asynchronous format
db_url_with_dialect = DATABASE_URL
if db_url_with_dialect.startswith("postgresql://"):
    # This ensures it uses the correct 'postgresql+asyncpg://' dialect
    db_url_with_dialect = db_url_with_dialect.replace("postgresql://", "postgresql+asyncpg://", 1)

print(f"--- Testing Connection String: {db_url_with_dialect} ---")

async def test_connection():
    try:
        # Call the function that sets up the database connection
        init_db(db_url_with_dialect)
        
        # If init_db succeeds, try to create tables
        print("\n✅ DB Engine initialized successfully. Attempting table creation...")
        await create_db_and_tables()
        print("✅ Database connection is fully operational. Your main app should now work.")

    except Exception as e:
        print("\n❌ FATAL CONNECTION ERROR! ❌")
        print("The underlying issue is:")
        # THIS IS THE CRITICAL LINE: It prints the specific error from PostgreSQL
        print(e) 
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(test_connection())
