import os
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    # 1. Look for the one-and-only connection string
    url = os.getenv("DATABASE_URL")
    
    # 2. If it's not there, CRASH immediately so we know there's a problem
    if not url:
        print("❌ ERROR: DATABASE_URL environment variable is MISSING!")
        return None

    # 3. Handle the 'postgres://' vs 'postgresql://' fix for SQLAlchemy 2.0
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
        
    return create_engine(url)