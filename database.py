from sqlalchemy import create_engine
import os
from dotenv import load_dotenv

load_dotenv()

def get_connection():
    # 1. Try to get the full connection string (used by GitHub/Streamlit)
    conn_str = os.getenv("DATABASE_URL")
    
    if conn_str:
        # If it starts with postgres://, SQLAlchemy needs it to be postgresql://
        if conn_str.startswith("postgres://"):
            conn_str = conn_str.replace("postgres://", "postgresql://", 1)
        return create_engine(conn_str)
    
    # 2. Fallback to individual variables (your local setup)
    user = os.getenv("DB_USER")
    pw = os.getenv("DB_PASSWORD")
    db = os.getenv("DB_NAME")
    host = os.getenv("DB_HOST")
    
    return create_engine(f'postgresql+psycopg2://{user}:{pw}@{host}:5432/{db}')