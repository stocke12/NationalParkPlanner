import streamlit as st
import os
import pandas as pd
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

# Initialize session states - The "Persistent DB" of your current session
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_info" not in st.session_state:
    st.session_state.user_info = None
if "curated_itinerary" not in st.session_state:
    st.session_state.curated_itinerary = []
if "temp_activities" not in st.session_state:
    st.session_state.temp_activities = []
if "master_itinerary" not in st.session_state:
    st.session_state.master_itinerary = ""

engine = get_connection()

# --- HELPERS ---
def create_pdf(itinerary_text, park_name, user_name):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, f"Adventure: {park_name}", ln=True, align='C')
    pdf.ln(10)
    pdf.set_font("Helvetica", size=11)
    # Basic encoding fix for FPDF
    clean_text = itinerary_text.replace('\u2013', '-').replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"')
    pdf.multi_cell(0, 7, clean_text.encode('latin-1', 'ignore').decode('latin-1'))
    return bytes(pdf.output())

# --- AUTH ---
if not st.session_state.logged_in:
    st.title("🌲 National Park Planner 🐻")
    t1, t2 = st.tabs(["Login", "Create Account"])
    with t1:
        u = st.text_input("Username").strip().lower()
        if st.button("Login"):
            with engine.connect() as conn:
                res = conn.execute(text("SELECT id, username, firstname, lastname, likes FROM users WHERE username = :u"), {"u": u}).fetchone()
                if res:
                    st.session_state.user_info = res
                    st.session_state.logged_in = True
                    st.rerun()
                else: st.error("User not found.")
    with t2:
        nu = st.text_input("New Username").strip().lower()
        fn, ln, em = st.text_input("First Name"), st.text_input("Last Name"), st.text_input("Email")
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
        st.write(f"Welcome back, **{st.session_state.user_info[2]}**")
        if st.button("Log Out"):
            st.session_state.logged_in = False
            st.rerun()

    plan_tab, friend_tab, my_trips_tab = st.tabs(["🗺️ Plan Trip", "👥 Friends", "🎒 My Trips"])

    # --- FRIENDS ---
    with friend_tab:
        st.header("Social Hub")
        f_search = st.text_input("Search User to Add").strip().lower()
        if st.button("Send Request"):
            with engine.connect() as conn:
                f_res = conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": f_search}).fetchone()
                if f_res:
                    try:
                        conn.execute(text("INSERT INTO friendships (user_id, friend_id, status) VALUES (:u, :f, 'pending')"),
                                     {"u": current_uid, "f": f_res[0]})
                        conn.commit()
                        st.success("Request sent!")
                    except: st.warning("Request already exists.")
                else: st.error("User not found.")

        st.subheader("Pending Requests")
        with engine.connect() as conn:
            pending = conn.execute(text("SELECT f.id, u.username FROM friendships f JOIN users u ON f.user_id = u.id WHERE f.friend_id = :uid AND f.status = 'pending'"), {"uid": current_uid}).fetchall()
            for req in pending:
                if st.button(f"Accept {req[1]}", key=f"acc_{req[0]}"):
                    conn.execute(text("UPDATE friendships SET status = 'accepted' WHERE id = :rid"), {"rid": req[0]})
                    conn.commit()
                    st.rerun()

    # --- PLANNING ---
    with plan_tab:
        with engine.connect() as conn:
            # 1. Get Friend Options for Multiselect
            friends_res = conn.execute(text("""
                SELECT u.username FROM users u
                JOIN friendships f ON (u.id = f.friend_id OR u.id = f.user_id)
                WHERE ((f.user_id = :uid OR f.friend_id = :uid) AND f.status = 'accepted') AND u.id != :uid
            """), {"uid": current_uid}).fetchall()
            friend_names = [fr[0] for fr in friends_res]

            # 2. Get Parks
            df_parks = pd.read_sql(text("SELECT name, id FROM parks ORDER BY name"), conn)

        p_sel = st.selectbox("Select Park", options=df_parks['name'])
        date_range = st.date_input("Dates", value=(date.today(), date.today()))
        # KEY: 'invited_friends' ensures this stays selected after AI generation
        st.multiselect("Invite Friends?", options=friend_names, key="invited_friends")

        if st.button("Generate Plan"):
            if len(date_range) < 2: st.error("Select a range.")
            else:
                nights = (date_range[1] - date_range[0]).days
                prompt = f"""
                Suggest 10 individual activities for {p_sel} (Style: {st.session_state.user_info[5]}).
                Format: Name | Type | Description
                
                ---MASTER_ITINERARY---
                Provide a day-by-day itinerary for {nights} nights.
                """
                with st.spinner("Scouting..."):
                    resp = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt).text
                    parts = resp.split('---MASTER_ITINERARY---')
                    st.session_state.temp_activities = [l for l in parts[0].strip().split('\n') if "|" in l]
                    st.session_state.master_itinerary = parts[1].strip() if len(parts) > 1 else ""

        if st.session_state.temp_activities:
            st.divider()
            c1, c2 = st.columns(2)
            with c1:
                st.subheader("💡 Activities")
                for i, act in enumerate(st.session_state.temp_activities):
                    name, a_type, desc = act.split('|')
                    with st.container(border=True):
                        st.markdown(f"**{name}** ({a_type.strip()})")
                        if st.button("Add ➕", key=f"add_act_{i}"):
                            st.session_state.curated_itinerary.append(name)
                            st.toast(f"Added {name}")
            with c2:
                st.subheader("📅 Full Itinerary")
                st.markdown(st.session_state.master_itinerary)
                
                if st.button("💾 Save Everything"):
                    # FINAL TRANSACTION
                    with engine.connect() as conn:
                        tid = conn.execute(text("INSERT INTO trips (user_id, owner_id, trip_name, start_date, end_date) VALUES (:u, :u, :n, :s, :e) RETURNING id"),
                                           {"u": current_uid, "n": f"{p_sel} Trip", "s": date_range[0], "e": date_range[1]}).fetchone()[0]
                        # Save participants (including you)
                        conn.execute(text("INSERT INTO trip_participants (trip_id, user_id, role, invitation_status) VALUES (:t, :u, 'owner', 'accepted')"), {"t": tid, "u": current_uid})
                        for f_name in st.session_state.invited_friends:
                            fid = conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": f_name}).fetchone()[0]
                            conn.execute(text("INSERT INTO trip_participants (trip_id, user_id, role, invitation_status) VALUES (:t, :u, 'guest', 'pending')"), {"t": tid, "u": fid})
                        
                        # Save Park & Itinerary
                        p_id = int(df_parks[df_parks['name'] == p_sel]['id'].iloc[0])
                        final_notes = f"ITINERARY:\n{st.session_state.master_itinerary}\n\nSELECTIONS:\n" + "\n".join(st.session_state.curated_itinerary)
                        conn.execute(text("INSERT INTO trip_parks (trip_id, park_id, notes) VALUES (:t, :p, :n)"), {"t": tid, "p": p_id, "n": final_notes})
                        conn.commit()
                        st.success("Adventure locked in!")

    # --- MY TRIPS ---
    with my_trips_tab:
        with engine.connect() as conn:
            trips = conn.execute(text("SELECT t.trip_name, tpk.notes, t.id FROM trips t JOIN trip_parks tpk ON t.id = tpk.trip_id WHERE t.user_id = :u"), {"u": current_uid}).fetchall()
            for t in trips:
                with st.expander(f"📍 {t[0]}"):
                    st.markdown(t[1])
                    pdf_b = create_pdf(t[1], t[0], st.session_state.user_info[2])
                    st.download_button("📥 Download PDF", pdf_b, f"Trip_{t[2]}.pdf", key=f"dl_{t[2]}")
