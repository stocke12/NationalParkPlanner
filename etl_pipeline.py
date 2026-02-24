import requests
import pandas as pd
import os
import pytz
from datetime import datetime, timedelta
from sqlalchemy import text
from database import get_connection

# --- CONFIGURATION ---
NPS_API_KEY = "dZfBlMQvmHe03hV0Mt4O1EQqGZfEvIyr0EwQDaTh"
HEADERS = {"X-Api-Key": NPS_API_KEY}


def setup_database(engine):
    """Ensures all tables exist before the ETL runs."""
    print("Initializing Database Tables...")
    with engine.connect() as conn:

        # ── CORE ──────────────────────────────────────────────────────────────

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

        # ── TRIPS ─────────────────────────────────────────────────────────────

        # is_template: marks trip as a reusable template
        # recap_text: AI-generated trip recap stored after trip ends
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trips (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                owner_id INTEGER REFERENCES users(id),
                trip_name TEXT,
                start_date DATE,
                end_date DATE,
                is_template BOOLEAN DEFAULT FALSE,
                recap_text TEXT
            );
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trip_participants (
                id SERIAL PRIMARY KEY,
                trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
                user_id INTEGER REFERENCES users(id),
                role TEXT DEFAULT 'viewer',
                invitation_status TEXT DEFAULT 'pending',
                invited_by INTEGER REFERENCES users(id),
                responded_at TIMESTAMP
            );
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trip_parks (
                id SERIAL PRIMARY KEY,
                trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
                park_id INTEGER REFERENCES parks(id),
                notes TEXT
            );
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trip_activities (
                id SERIAL PRIMARY KEY,
                trip_id INTEGER REFERENCES trips(id) ON DELETE CASCADE,
                day_number INTEGER,
                activity_name TEXT,
                activity_type TEXT,
                sort_order INTEGER DEFAULT 0
            );
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trip_packing_items (
                id SERIAL PRIMARY KEY,
                trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                category VARCHAR(100),
                item_name VARCHAR(255) NOT NULL,
                is_checked BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

        # photo_url: optional image attached to a journal note
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS trip_day_notes (
                id SERIAL PRIMARY KEY,
                trip_id INTEGER NOT NULL REFERENCES trips(id) ON DELETE CASCADE,
                day_number INTEGER NOT NULL,
                author_id INTEGER REFERENCES users(id),
                note_text TEXT NOT NULL,
                photo_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

        # ── SOCIAL ────────────────────────────────────────────────────────────

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS friendships (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id),
                friend_id INTEGER REFERENCES users(id),
                status TEXT DEFAULT 'pending'
            );
        """))

        # ── PARK EXPLORER ─────────────────────────────────────────────────────

        # Enriched park data synced from NPS API (description, fees, hours, etc.)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS park_details (
                id SERIAL PRIMARY KEY,
                park_id INTEGER UNIQUE REFERENCES parks(id) ON DELETE CASCADE,
                description TEXT,
                weather_info TEXT,
                directions_info TEXT,
                entrance_fee_cost TEXT,
                entrance_fee_description TEXT,
                visitor_center_hours TEXT,
                activities TEXT,
                topics TEXT,
                latitude NUMERIC(9,6),
                longitude NUMERIC(9,6),
                last_synced TIMESTAMP
            );
        """))

        # Parks users want to visit someday
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS park_wishlists (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                park_id INTEGER NOT NULL REFERENCES parks(id) ON DELETE CASCADE,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, park_id)
            );
        """))

        # ── STATS & GAMIFICATION ──────────────────────────────────────────────

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_badges (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                badge_key VARCHAR(100) NOT NULL,
                badge_name TEXT NOT NULL,
                badge_description TEXT,
                earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, badge_key)
            );
        """))

        conn.commit()
    print("Database check complete.")


def fetch_and_sync_parks(engine):
    """Fetches all National Parks and syncs core park rows."""
    print("Syncing Parks...")
    endpoint = "https://developer.nps.gov/api/v1/parks?limit=1000"
    response = requests.get(endpoint, headers=HEADERS)
    data = response.json()

    parksls = []
    for park in data['data']:
        designation = park['designation'].lower()
        if (
            "national park" in designation
            or "national and state park" in designation
            or park['name'] == "National Park of American Samoa"
        ):
            image_url = None
            if park.get('images'):
                image_url = park['images'][0].get('url')

            parksls.append({
                'Park Name': park['name'],
                'State': park['states'],
                'Park Code': park['parkCode'],
                'Id': park['id'],
                'Image': image_url,
            })

    df = pd.DataFrame(parksls)
    df.replace(
        to_replace=['ID,MT,WY', 'CA,NV', 'NC,TN'],
        value=['WY', 'CA', 'TN'],
        inplace=True
    )

    with engine.connect() as conn:
        for _, row in df.iterrows():
            try:
                conn.execute(text("""
                    INSERT INTO parks (name, npid, state, code, image_url)
                    VALUES (:name, :npid, :state, :code, :img)
                    ON CONFLICT (npid) DO UPDATE SET
                        name      = EXCLUDED.name,
                        state     = EXCLUDED.state,
                        code      = EXCLUDED.code,
                        image_url = EXCLUDED.image_url;
                """), {
                    "name": row['Park Name'],
                    "npid": row['Id'],
                    "state": row['State'],
                    "code": row['Park Code'],
                    "img": row['Image'],
                })
            except Exception as e:
                print(f"Error inserting park {row['Park Name']}: {e}")
        conn.commit()
    print(f"Successfully synced {len(df)} parks.")


def fetch_and_sync_park_details(engine):
    """
    Enriches each park with description, weather, entrance fees, visitor
    center hours, activities, topics, and coordinates from the NPS API.
    Runs after fetch_and_sync_parks so all park rows exist.
    """
    print("Syncing Park Details...")
    import json

    with engine.connect() as conn:
        df_parks = pd.read_sql(text("SELECT id, code FROM parks"), conn)

    if df_parks.empty:
        print("No parks found — run park sync first.")
        return

    synced = 0
    for _, row in df_parks.iterrows():
        park_id = row['id']
        park_code = row['code']

        try:
            resp = requests.get(
                f"https://developer.nps.gov/api/v1/parks?parkCode={park_code}&limit=1",
                headers=HEADERS,
                timeout=10,
            )
            data = resp.json().get('data', [])
            if not data:
                continue
            p = data[0]

            # Entrance fee — first non-zero fee
            fee_cost, fee_desc = None, None
            for fee in p.get('entranceFees', []):
                if fee.get('cost') and float(fee.get('cost', 0)) > 0:
                    fee_cost = fee['cost']
                    fee_desc = fee.get('description', '')
                    break

            # Operating hours
            hours_parts = []
            for h in p.get('operatingHours', []):
                name = h.get('name', '')
                std = h.get('standardHours', {})
                if std:
                    hours_parts.append(
                        f"{name}: "
                        + ", ".join(f"{k.capitalize()} {v}" for k, v in std.items())
                    )
            visitor_hours = " | ".join(hours_parts) if hours_parts else None

            # Activities & topics serialised as JSON strings
            activities = json.dumps([a['name'] for a in p.get('activities', [])])
            topics     = json.dumps([t['name'] for t in p.get('topics', [])])

            # Coordinates
            lat, lng = None, None
            coords = p.get('latLong', '')
            if coords:
                try:
                    parts = coords.replace('lat:', '').replace('long:', '').split(',')
                    lat = float(parts[0].strip())
                    lng = float(parts[1].strip())
                except Exception:
                    pass

            with engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO park_details (
                        park_id, description, weather_info, directions_info,
                        entrance_fee_cost, entrance_fee_description,
                        visitor_center_hours, activities, topics,
                        latitude, longitude, last_synced
                    ) VALUES (
                        :pid, :desc, :weather, :directions,
                        :fee_cost, :fee_desc,
                        :hours, :activities, :topics,
                        :lat, :lng, :now
                    )
                    ON CONFLICT (park_id) DO UPDATE SET
                        description              = EXCLUDED.description,
                        weather_info             = EXCLUDED.weather_info,
                        directions_info          = EXCLUDED.directions_info,
                        entrance_fee_cost        = EXCLUDED.entrance_fee_cost,
                        entrance_fee_description = EXCLUDED.entrance_fee_description,
                        visitor_center_hours     = EXCLUDED.visitor_center_hours,
                        activities               = EXCLUDED.activities,
                        topics                   = EXCLUDED.topics,
                        latitude                 = EXCLUDED.latitude,
                        longitude                = EXCLUDED.longitude,
                        last_synced              = EXCLUDED.last_synced;
                """), {
                    "pid":         park_id,
                    "desc":        p.get('description'),
                    "weather":     p.get('weatherInfo'),
                    "directions":  p.get('directionsInfo'),
                    "fee_cost":    fee_cost,
                    "fee_desc":    fee_desc,
                    "hours":       visitor_hours,
                    "activities":  activities,
                    "topics":      topics,
                    "lat":         lat,
                    "lng":         lng,
                    "now":         datetime.utcnow(),
                })
                conn.commit()
            synced += 1

        except Exception as e:
            print(f"Error syncing details for {park_code}: {e}")

    print(f"Successfully synced details for {synced} parks.")


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
        park_id   = row['id']
        park_code = row['code']

        endpoint = f"https://developer.nps.gov/api/v1/alerts?parkCode={park_code}"
        response = requests.get(endpoint, headers=HEADERS)
        data = response.json()

        for alert in data['data']:
            alerts_data.append({
                'nps_id':      alert.get('id'),
                'Title':       alert.get('title'),
                'Category':    alert.get('category'),
                'Description': alert.get('description'),
                'ParkId':      park_id,
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
                conn.execute(text("""
                    INSERT INTO alerts (nps_id, title, category, description, park_id, lastseen, isactive)
                    VALUES (:nps_id, :title, :category, :description, :park_id, :lastseen, True)
                    ON CONFLICT (nps_id)
                    DO UPDATE SET
                        title       = EXCLUDED.title,
                        category    = EXCLUDED.category,
                        description = EXCLUDED.description,
                        lastseen    = :lastseen,
                        isactive    = True;
                """), {
                    "nps_id":      row['nps_id'],
                    "title":       row['Title'],
                    "category":    row['Category'],
                    "description": row['Description'],
                    "park_id":     int(row['ParkId']),
                    "lastseen":    now,
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
        print(f"✅ Robot connected to: {str(db_engine.url).split('@')[-1]}")

        setup_database(db_engine)
        fetch_and_sync_parks(db_engine)
        fetch_and_sync_park_details(db_engine)   # runs after parks are seeded
        fetch_and_sync_alerts(db_engine)
        print("ETL Pipeline completed successfully.")
