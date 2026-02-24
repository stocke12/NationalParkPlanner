import requests
import pandas as pd
import os
import pytz  # You may need to add this to your .yml pip install line
from datetime import datetime, timedelta
from sqlalchemy import text
from database import get_connection

# --- CONFIGURATION ---
NPS_API_KEY = "dZfBlMQvmHe03hV0Mt4O1EQqGZfEvIyr0EwQDaTh"
HEADERS = {"X-Api-Key": NPS_API_KEY}

def setup_database(engine):
    """Ensures the tables exist before the ETL runs."""
    print("Initializing Database Tables...")
    with engine.connect() as conn:
        # Create Parks Table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS parks (
                id SERIAL PRIMARY KEY,
                name TEXT,
                npid TEXT UNIQUE,
                state TEXT,
                code TEXT UNIQUE,
                image_url TEXT
            );
        """))
        
        # Create Alerts Table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS alerts (
                id SERIAL PRIMARY KEY,
                nps_id TEXT UNIQUE,
                title TEXT,
                category TEXT,
                description TEXT,
                park_id INTEGER REFERENCES parks(id),
                lastseen TIMESTAMP,
                isactive BOOLEAN
            );
        """))

        # Create Users Table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                firstname TEXT,
                lastname TEXT,
                email TEXT,
                likes TEXT
            );
        """))
        conn.commit()
    print("Database check complete.")

def fetch_and_sync_parks(engine):
    """Fetches all National Parks and syncs them."""
    print("Syncing Parks...")
    endpoint = "https://developer.nps.gov/api/v1/parks?limit=1000"
    response = requests.get(endpoint, headers=HEADERS)
    data = response.json()

    parksls = []
    for park in data['data']:
        designation = park['designation'].lower()
        if "national park" in designation or "national and state park" in designation or park['name'] == "National Park of American Samoa":
            parksls.append({
                'Park Name': park['name'],
                'State': park['states'],
                'Park Code': park['parkCode'],
                'Id': park['id']
            })

    df = pd.DataFrame(parksls)
    df.replace(to_replace=['ID,MT,WY', 'CA,NV', 'NC,TN'], value=['WY', 'CA', 'TN'], inplace=True)

    with engine.connect() as conn:
        for _, row in df.iterrows():
            try:
                query = text("""
                    INSERT INTO parks (name, npid, state, code) 
                    VALUES (:name, :npid, :state, :code)
                    ON CONFLICT (npid) DO UPDATE SET
                        name = EXCLUDED.name,
                        state = EXCLUDED.state,
                        code = EXCLUDED.code;
                """)
                conn.execute(query, {
                    "name": row['Park Name'],
                    "npid": row['Id'],
                    "state": row['State'],
                    "code": row['Park Code']
                })
            except Exception as e:
                print(f"Error inserting park {row['Park Name']}: {e}")
        conn.commit()
    print(f"Successfully synced {len(df)} parks.")

def fetch_and_sync_alerts(engine):
    """Fetches alerts for all parks currently in the DB and syncs them."""
    print("Syncing Alerts...")
    
    with engine.connect() as conn:
        df_parks = pd.read_sql(text("SELECT id, code FROM parks"), conn)
    
    if df_parks.empty:
        print("No parks found in database. Run park sync first.")
        return

    alerts_data = []
    for _, row in df_parks.iterrows():
        park_id = row['id']
        park_code = row['code']
        
        endpoint = f"https://developer.nps.gov/api/v1/alerts?parkCode={park_code}"
        response = requests.get(endpoint, headers=HEADERS)
        data = response.json()

        for alert in data['data']:
            alerts_data.append({
                'nps_id': alert.get('id'),
                'Title': alert.get('title'),
                'Category': alert.get('category'),
                'Description': alert.get('description'),
                'ParkId': park_id
            })

    if not alerts_data:
        print("No active alerts found at NPS.")
        return

    df_alerts = pd.DataFrame(alerts_data)
    
    mountain_tz = pytz.timezone('America/Denver')
    now = datetime.now(mountain_tz)
    print(f"Current Sync Time (MST): {now.strftime('%Y-%m-%d %H:%M:%S')}")

    with engine.connect() as conn:
        for _, row in df_alerts.iterrows():
            try:
                insert_query = text("""
                    INSERT INTO alerts (nps_id, title, category, description, park_id, lastseen, isactive)
                    VALUES (:nps_id, :title, :category, :description, :park_id, :lastseen, True)
                    ON CONFLICT (nps_id)
                    DO UPDATE SET
                        title = EXCLUDED.title,
                        category = EXCLUDED.category,
                        description = EXCLUDED.description,
                        lastseen = :lastseen,
                        isactive = True;
                """)
                conn.execute(insert_query, {
                    "nps_id": row['nps_id'],
                    "title": row['Title'],
                    "category": row['Category'],
                    "description": row['Description'],
                    "park_id": int(row['ParkId']),
                    "lastseen": now
                })
            except Exception as e:
                print(f"Error syncing alert {row['nps_id']}: {e}")

        buffer_time = now - timedelta(minutes=10)
        conn.execute(text("""
            UPDATE alerts 
            SET isactive = False 
            WHERE lastseen < :cutoff OR lastseen IS NULL
        """), {"cutoff": buffer_time})
        
        conn.commit()
    print(f"Successfully synced {len(df_alerts)} alerts.")

if __name__ == "__main__":
    db_engine = get_connection()
    if db_engine:
        # DEBUG: Verify the database name we are hitting
        print(f"✅ Robot connected to: {str(db_engine.url).split('@')[-1]}") 
        
        setup_database(db_engine)
        fetch_and_sync_parks(db_engine)
        fetch_and_sync_alerts(db_engine)
        print("ETL Pipeline completed successfully.")
