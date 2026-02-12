import streamlit as st
import os
import pandas as pd
import json
import traceback
from datetime import datetime, date
from dotenv import load_dotenv
from google import genai
from sqlalchemy import text
from database import get_connection
from fpdf import FPDF

# 1. SETUP
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    st.error("Missing GEMINI_API_KEY.")
    st.stop()

client = genai.Client(api_key=GEMINI_API_KEY)

# 2. CONFIG
st.set_page_config(page_title="National Park Planner", page_icon="🌲", layout="wide")

# Initialize session states
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_info" not in st.session_state:
    st.session_state.user_info = None
if "curated_itinerary" not in st.session_state:
    st.session_state.curated_itinerary = []
if "temp_activities" not in st.session_state:
    st.session_state.temp_activities = []
if "expert_advice" not in st.session_state:
    st.session_state.expert_advice = ""

engine = get_connection()
if engine is None:
    st.error("❌ DATABASE_URL is missing.")
    st.stop()

# --- HELPERS ---
def log_error_to_db(username, error_msg, trace):
    try:
        with engine.connect() as conn:
            query = text("INSERT INTO error_logs (username, error_message, stack_trace) VALUES (:u, :e, :s)")
            conn.execute(query, {"u": username, "e": error_msg, "s": trace})
            conn.commit()
    except: pass

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
    return bytes(pdf.output())

# --- AUTH ---
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
    current_uid = st.session_state.user_info[0]
    with st.sidebar:
        st.write(f"User: **{st.session_state.user_info[2]}**")
        if st.button("Log Out"):
            st.session_state.logged_in = False
            st.rerun()

    plan_tab, friend_tab, my_trips_tab = st.tabs(["🗺️ Plan Trip", "👥 Friends", "🎒 My Trips"])

    # --- PLANNING ---
    with plan_tab:
        try:
            with engine.connect() as conn:
                df_parks = pd.read_sql(text("SELECT name, id, image_url FROM parks ORDER BY name"), conn)
                park_names = df_parks['name'].tolist()
        except: park_names = []

        p_sel = st.selectbox("Select Park", options=park_names)
        date_range = st.date_input("Trip Dates", value=(date.today(), date.today()), min_value=date.today())
        
        start_dt = date_range[0] if len(date_range) > 0 else date.today()
        end_dt = date_range[1] if len(date_range) > 1 else start_dt
        nights_calc = (end_dt - start_dt).days
        invited = st.multiselect("Invite Friends?", options=[]) # Placeholder for friends logic

        if st.button("Scout Activities"):
            if len(date_range) < 2:
                st.error("Please select a range.")
            else:
                p_info = df_parks[df_parks['name'] == p_sel].iloc[0]
                p_id = int(p_info['id'])

                with engine.connect() as conn:
                    al_res = conn.execute(text("SELECT title, description FROM alerts WHERE park_id = :p AND isactive=True"), {"p": p_id}).fetchall()
                    active_alerts = [{"title": r[0], "description": r[1]} for r in al_res]

                prompt = f"""
                You are Ranger Gemini. Suggest 10 specific activities for {p_sel}. 
                Travel Style: {st.session_state.user_info[5]}.
                Active Alerts: {active_alerts}.
                
                Format each activity EXACTLY as: Name | Type | Description
                Example: Delicate Arch Hike | Hike | A 3-mile round trip to the most famous arch.
                
                After the list of 10 activities, add a line '---' then provide a brief paragraph of advice for a {nights_calc}-night trip.
                """
                
                with st.spinner("Ranger Gemini is scouting the area..."):
                    resp = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt).text
                    parts = resp.split('---')
                    raw_activities = parts[0].strip().split('\n')
                    st.session_state.temp_activities = [l for l in raw_activities if "|" in l]
                    st.session_state.expert_advice = parts[1].strip() if len(parts) > 1 else ""
                    st.session_state.curated_itinerary = []

        # --- THE WORKSPACE ---
        if st.session_state.temp_activities:
            st.divider()
            col_left, col_right = st.columns([0.6, 0.4])

            with col_left:
                st.subheader("🌲 Suggested Activities")
                for i, act_line in enumerate(st.session_state.temp_activities):
                    details = act_line.split('|')
                    name = details[0].strip()
                    a_type = details[1].strip() if len(details) > 1 else "Misc"
                    desc = details[2].strip() if len(details) > 2 else ""
                    
                    with st.container(border=True):
                        c1, c2 = st.columns([0.85, 0.15])
                        c1.markdown(f"**{name}** `{a_type}`")
                        c1.caption(desc)
                        if c2.button("➕", key=f"add_{i}"):
                            st.session_state.curated_itinerary.append(f"**{name}** ({a_type}): {desc}")
                            st.toast(f"Added {name}")

            with col_right:
                st.subheader("🎒 Your Custom Itinerary")
                if not st.session_state.curated_itinerary:
                    st.info("Your itinerary is empty. Add activities from the left!")
                
                for j, item in enumerate(st.session_state.curated_itinerary):
                    with st.container(border=True):
                        st.markdown(item)
                        if st.button("🗑️ Remove", key=f"rem_{j}"):
                            st.session_state.curated_itinerary.pop(j)
                            st.rerun()

                if st.session_state.expert_advice:
                    with st.expander("📖 Ranger Gemini's Suggested Flow"):
                        st.write(st.session_state.expert_advice)

                if st.session_state.curated_itinerary:
                    if st.button("💾 Save Adventure", use_container_width=True):
                        final_notes = "\n\n".join(st.session_state.curated_itinerary)
                        p_info = df_parks[df_parks['name'] == p_sel].iloc[0]
                        try:
                            with engine.connect() as conn:
                                tid_res = conn.execute(text("INSERT INTO trips (user_id, owner_id, trip_name, status, start_date, end_date) VALUES (:u, :o, :n, 'planned', :sd, :ed) RETURNING id"), 
                                                     {"u": current_uid, "o": current_uid, "n": f"{p_sel} Trip", "sd": start_dt, "ed": end_dt})
                                tid = tid_res.fetchone()[0]
                                conn.execute(text("INSERT INTO trip_participants (trip_id, user_id, role, invitation_status, invited_by) VALUES (:t, :u, 'owner', 'accepted', :u)"), {"t": tid, "u": current_uid})
                                conn.execute(text("INSERT INTO trip_parks (trip_id, park_id, notes) VALUES (:t, :p, :n)"), {"t": tid, "p": int(p_info['id']), "n": final_notes})
                                conn.commit()
                                st.success("Adventure Saved!")
                        except Exception as e: st.error(f"Save Failed: {e}")

    # --- FRIENDS TAB (Existing Logic) ---
    with friend_tab:
        st.header("Social Hub")
        # ... (Your existing friendship logic here) ...

    # --- MY TRIPS ---
    with my_trips_tab:
        st.header("Your Saved Adventures")
        with engine.connect() as conn:
            my_trips = conn.execute(text("""
                SELECT t.trip_name, t.status, t.start_date, t.end_date, tpk.notes, t.id
                FROM trips t
                JOIN trip_participants tp ON t.id = tp.trip_id
                LEFT JOIN trip_parks tpk ON t.id = tpk.trip_id
                WHERE tp.user_id = :u ORDER BY t.start_date DESC"""), {"u": current_uid}).fetchall()
            
            for t in my_trips:
                with st.expander(f"📍 {t[0]} ({t[2]} to {t[3]})"):
                    st.write(f"**Status:** {t[1]}")
                    st.markdown(t[4] if t[4] else "No itinerary details.")
                    if t[4]:
                        pdf_b = create_pdf(t[4], t[0], st.session_state.user_info[2], [])
                        st.download_button("📥 PDF", pdf_b, f"Trip_{t[5]}.pdf", key=f"dl_{t[5]}")
