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

    return create_engine(
        url,
        pool_pre_ping=True,      # test each connection before using it — silently reconnects if SSL dropped
        pool_recycle=300,        # force-recycle connections older than 5 minutes
        pool_size=5,             # keep up to 5 connections in the pool
        max_overflow=2,          # allow 2 extra connections under burst load
        connect_args={
            "connect_timeout": 10,   # fail fast if DB is unreachable rather than hanging
            "keepalives": 1,         # enable TCP keepalives so idle connections stay alive
            "keepalives_idle": 60,   # send first keepalive after 60s of inactivity
            "keepalives_interval": 10,  # retry keepalive every 10s
            "keepalives_count": 5,   # drop connection after 5 failed keepalives
        }
    )
