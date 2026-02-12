import streamlit as st
import os
import pandas as pd
import json
import traceback
from dotenv import load_dotenv
from google import genai
from sqlalchemy import text
from database import get_connection
from fpdf import FPDF

# 1. SETUP: Load environment and AI Client
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    st.error("Missing GEMINI_API_KEY. Please set it in your environment variables.")
    st.stop()

client = genai.Client(api_key=GEMINI_API_KEY)

# 2. CONFIG: Page styling
st.set_page_config(page_title="National Park Planner", page_icon="🌲", layout="centered")

# --- SESSION STATE INITIALIZATION ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_info" not in st.session_state:
    st.session_state.user_info = None

# --- DATABASE CONNECTION CHECK ---
engine = get_connection()
if engine is None:
    st.error("❌ DATABASE_URL is missing. Check your .env or Streamlit Secrets.")
    st.stop()

# --- DATABASE HELPERS ---
def log_error_to_db(username, error_msg, trace):
    try:
        with engine.connect() as conn:
            query = text("INSERT INTO error_logs (username, error_message, stack_trace) VALUES (:u, :e, :s)")
            conn.execute(query, {"u": username, "e": error_msg, "s": trace})
            conn.commit()
    except Exception as db_e:
        print(f"Failed to log error: {db_e}")

# --- PDF GENERATION HELPER ---
def create_pdf(itinerary_text, park_name, user_name, alerts_list):
    replacements = {'’': "'", '‘': "'", '”': '"', '“': '"', '—': '-', '–': '-', '•': '*'}
    for char, rep in replacements.items():
        itinerary_text = itinerary_text.replace(char, rep)
    
    itinerary_text = itinerary_text.encode('latin-1', 'ignore').decode('latin-1')
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, f"Adventure: {park_name}", ln=True, align='C')
    pdf.set_font("Helvetica", 'I', 12)
    pdf.cell(0, 10, f"Planned by {user_name}", ln=True, align='C')
    pdf.ln(10)
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 7, itinerary_text)
    
    if alerts_list:
        pdf.ln(10)
        pdf.set_font("Helvetica", 'B', 14)
        pdf.cell(0, 10, "Safety Alerts:", ln=True)
        pdf.set_font("Helvetica", size=10)
        for a in alerts_list:
            pdf.multi_cell(0, 7, f"- {a['title']}: {a['description']}")
    return bytes(pdf.output())

# --- LOGIN / REGISTRATION ---
if not st.session_state.logged_in:
    st.title("🌲 National Park Planner 🐻")
    t1, t2 = st.tabs(["Login", "Create Account"])
    
    with t1:
        u = st.text_input("Username").strip().lower()
        if st.button("Login"):
            with engine.connect() as conn:
                res = conn.execute(text("SELECT id, username, firstname, lastname, email, likes FROM users WHERE username = :u"), {"u": u}).fetchone()
                if res:
                    st.session_state.user_info = res
                    st.session_state.logged_in = True
                    st.rerun()
                else: st.error("User not found.")

    with t2:
        nu = st.text_input("New Username").strip().lower()
        fn = st.text_input("First Name")
        ln = st.text_input("Last Name")
        em = st.text_input("Email")
        lk = st.text_area("Travel Style")
        if st.button("Sign Up"):
            try:
                with engine.connect() as conn:
                    conn.execute(text("INSERT INTO users (username, firstname, lastname, email, likes) VALUES (:u,:f,:ln,:e,:l)"),
                                 {"u":nu, "f":fn, "ln":ln, "e":em, "l":lk})
                    conn.commit()
                    st.success("Account created!")
            except: st.error("Error creating account.")

# --- MAIN APP ---
else:
    with st.sidebar:
        st.title("Settings")
        st.write(f"User: **{st.session_state.user_info[2]}**")
        if st.button("Log Out"):
            st.session_state.logged_in = False
            st.rerun()

    # TABS FOR NAVIGATION
    plan_tab, friend_tab, my_trips_tab = st.tabs(["🗺️ Plan Trip", "👥 Friends", "🎒 My Trips"])

    # FRIENDS TAB
    with friend_tab:
        st.header("Social Hub")
        f_search = st.text_input("Find User by Username").strip().lower()
        if st.button("Add Friend"):
            with engine.connect() as conn:
                f_res = conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": f_search}).fetchone()
                if f_res:
                    conn.execute(text("INSERT INTO friendships (user_id, friend_id, status) VALUES (:u, :f, 'accepted')"),
                                 {"u": st.session_state.user_info[0], "f": f_res[0]})
                    conn.commit()
                    st.success(f"Added {f_search}!")
                else: st.error("User not found.")
        
        st.subheader("Your Friends")
        with engine.connect() as conn:
            friends = conn.execute(text("""
                SELECT username FROM users JOIN friendships ON users.id = friendships.friend_id 
                WHERE friendships.user_id = :uid"""), {"uid": st.session_state.user_info[0]}).fetchall()
            for fr in friends: st.write(f"✅ {fr[0]}")

    # PLANNING TAB
    with plan_tab:
        try:
            with engine.connect() as conn:
                df_parks = pd.read_sql(text("SELECT name, id, image_url FROM parks ORDER BY name"), conn)
                park_names = df_parks['name'].tolist()
        except: park_names = []

        if not park_names: st.warning("No parks found.")
        else:
            p_sel = st.selectbox("Select Park", options=park_names)
            dates = st.text_input("Travel Dates", "Next month")
            nights = st.number_input("Nights", 1, 14, 3)
            
            # Collaboration Selection
            friend_list = [f[0] for f in friends]
            invited = st.multiselect("Invite Friends?", options=friend_list)

            if st.button("Generate & Save Trip"):
                p_info = df_parks[df_parks['name'] == p_sel].iloc[0]
                p_id = int(p_info['id'])

                with engine.connect() as conn:
                    al_res = conn.execute(text("SELECT title, description FROM alerts WHERE park_id = :p AND isactive=True"), {"p": p_id}).fetchall()
                    active_alerts = [{"title": r[0], "description": r[1]} for r in al_res]

                prompt = f"""
                Act as a Park Ranger. User likes {st.session_state.user_info[5]}. 
                Plan {p_sel} for {nights} nights. Safety: {active_alerts}
                Return a markdown itinerary then a JSON block at the very end like this:
                DATA_START
                [{{"day": 1, "activity": "Name", "notes": "Info"}}]
                DATA_END
                """
                
                with st.spinner("Ranger AI is planning..."):
                    resp = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt).text
                    itinerary_main = resp.split("DATA_START")[0]
                    
                    # DATABASE SAVING
                    with engine.connect() as conn:
                        # 1. Create Trip
                        tid = conn.execute(text("INSERT INTO trips (user_id, trip_name) VALUES (:u, :n) RETURNING id"),
                                           {"u": st.session_state.user_info[0], "n": f"{p_sel} Adventure"}).fetchone()[0]
                        # 2. Add Participants
                        conn.execute(text("INSERT INTO trip_participants (trip_id, user_id, role) VALUES (:t, :u, 'owner')"),
                                     {"t": tid, "u": st.session_state.user_info[0]})
                        for f_user in invited:
                            fid = conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": f_user}).fetchone()[0]
                            conn.execute(text("INSERT INTO trip_participants (trip_id, user_id) VALUES (:t, :u)"), {"t": tid, "u": fid})
                        
                        # 3. Add Activity Notes to Trip_Parks
                        conn.execute(text("INSERT INTO trip_parks (trip_id, park_id, notes) VALUES (:t, :p, :n)"),
                                     {"t": tid, "p": p_id, "n": itinerary_main[:500]})
                        conn.commit()
                    
                    st.header(f"Trip to {p_sel}")
                    if p_info['image_url']: st.image(p_info['image_url'])
                    st.markdown(itinerary_main)
                    
                    with st.expander("⚠️ Safety Alerts"):
                        for a in active_alerts: st.write(f"**{a['title']}**: {a['description']}")

    # MY TRIPS TAB
    with my_trips_tab:
        st.header("Your Saved Adventures")
        with engine.connect() as conn:
            my_trips = conn.execute(text("""
                SELECT t.trip_name, t.status, t.created_at FROM trips t
                JOIN trip_participants tp ON t.id = tp.trip_id
                WHERE tp.user_id = :u"""), {"u": st.session_state.user_info[0]}).fetchall()
            for t in my_trips:
                st.info(f"📍 {t[0]} | Status: {t[1]} | Created: {t[2].strftime('%Y-%m-%d')}")