from sqlalchemy import create_engine
import os
from dotenv import load_dotenv

load_dotenv()

def get_connection(): # Keep the name the same so other files don't break
    user = os.getenv("DB_USER")
    pw = os.getenv("DB_PASSWORD")
    db = os.getenv("DB_NAME")
    host = os.getenv("DB_HOST")
    
    # Returns the SQLAlchemy engine
    return create_engine(f'postgresql+psycopg2://{user}:{pw}@{host}:5432/{db}')