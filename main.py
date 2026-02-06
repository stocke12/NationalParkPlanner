import pandas as pd
import os
from dotenv import load_dotenv
from google import genai
from sqlalchemy import text
from database import get_connection

# Load secrets and initialize AI
load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

def run_app():
    engine = get_connection()
    if not engine:
        print("Could not connect to database. Check your .env file.")
        return

    print('Welcome to the National Park Trip Assistant!')

    # --- 1. LOGIN / ACCOUNT CREATION ---
    user_authenticated = False
    with engine.connect() as conn:
        while not user_authenticated:
            choice = input('\nDo you already have an account? Y/N ').lower()
            
            if 'y' in choice:
                username = input('Enter your username: ')
                # SQLAlchemy uses :name for parameters
                query = text("SELECT * FROM users WHERE username = :u")
                df = pd.read_sql(query, conn, params={"u": username})
                
                if not df.empty:
                    fname = df.iloc[0]['firstname']
                    likes = df.iloc[0]['likes']
                    print(f"User Profile Found: Welcome back, {fname}!")
                    user_authenticated = True
                else:
                    print("User not found.")
            else:
                # Create Account Logic
                username = input('Please enter a new username: ')
                fname = input('First name: ')
                lname = input('Last name: ')
                email = input('Email: ')
                likes = input('Likes (hobbies, interests): ')
                
                insert_user = text("""
                    INSERT INTO users (username, firstname, lastname, email, likes) 
                    VALUES (:u, :f, :l, :e, :likes)
                """)
                conn.execute(insert_user, {
                    "u": username, "f": fname, "l": lname, "e": email, "likes": likes
                })
                conn.commit()
                print("Account created successfully!")
                user_authenticated = True

        # --- 2. PARK SELECTION (Disambiguation Logic) ---
        park_input = input(f'\nHello {fname}, which National Park would you like to visit? ')
        while True:
            park_query = text("SELECT name, id, code FROM parks WHERE name ILIKE :p")
            df_park = pd.read_sql(park_query, conn, params={"p": f"%{park_input}%"})
            
            if len(df_park) == 1:
                park_name = df_park.iloc[0]['name']
                park_id = int(df_park.iloc[0]['id']) # Force to Python int
                break
            elif len(df_park) > 1:
                print("\nI found multiple matches. Which one did you mean?")
                for i, name in enumerate(df_park['name']):
                    print(f"{i + 1}. {name}")
                choice = input("Enter the number of your choice: ")
                idx = int(choice) - 1
                park_name = df_park.iloc[idx]['name']
                park_id = int(df_park.iloc[idx]['id'])
                break
            else:
                park_input = input(f"'{park_input}' not recognized. Please try again: ")

        # --- 3. TRIP DETAILS ---
        dates = input(f'When are you planning to visit {park_name}? ')
        nights = input('How many nights will you be spending? ')

        # --- 4. FETCH ALERTS FROM DATABASE ---
        alert_query = text("SELECT description FROM alerts WHERE park_id = :pid AND isactive = True")
        df_alerts = pd.read_sql(alert_query, conn, params={"pid": park_id})
        alert_list = df_alerts['description'].tolist()

    # --- 5. AI GENERATION (Outside the connection block) ---
    prompt = f"""
    You are a master national park planner. Plan a trip based on:
    - Park: {park_name}
    - Time: {dates}
    - Nights: {nights}
    - User Interests: {likes}
    - Active Alerts: {alert_list}
    
    Provide a day-by-day itinerary and safety tips.
    """

    print(f"\n--- Consulting experts for your trip to {park_name}... ---")
    response = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt)
    
    print("\nYOUR MASTER PLAN:\n", "="*20)
    print(response.text)

if __name__ == "__main__":
    run_app()