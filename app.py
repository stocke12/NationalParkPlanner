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
st.set_page_config(page_title="National Park Planner", page_icon="🌲", layout="centered")

if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_info" not in st.session_state:
    st.session_state.user_info = None

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
    if alerts_list:
        pdf.ln(10)
        pdf.set_font("Helvetica", 'B', 14)
        pdf.cell(0, 10, "Current Safety Alerts:", ln=True)
        pdf.set_font("Helvetica", size=10)
        for a in alerts_list:
            pdf.multi_cell(0, 7, f"- {a['title']}: {a['description']}")
            pdf.ln(2)
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

    # --- FRIENDS ---
    with friend_tab:
        st.header("Social Hub")
        f_search = st.text_input("Add Friend by Username").strip().lower()
        if st.button("Send Request"):
            with engine.connect() as conn:
                f_res = conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": f_search}).fetchone()
                if f_res:
                    try:
                        conn.execute(text("INSERT INTO friendships (user_id, friend_id, status) VALUES (:u, :f, 'pending')"),
                                     {"u": current_uid, "f": f_res[0]})
                        conn.commit()
                        st.success(f"Request sent to {f_search}!")
                    except: st.warning("Request already exists.")
                else: st.error("User not found.")

        st.subheader("Pending Requests")
        with engine.connect() as conn:
            pending = conn.execute(text("SELECT f.id, u.username FROM friendships f JOIN users u ON f.user_id = u.id WHERE f.friend_id = :uid AND f.status = 'pending'"), {"uid": current_uid}).fetchall()
            for req in pending:
                c1, c2 = st.columns([2, 1])
                c1.write(f"🤝 **{req[1]}** sent a request.")
                if c2.button("Accept", key=f"acc_{req[0]}"):
                    conn.execute(text("UPDATE friendships SET status = 'accepted' WHERE id = :rid"), {"rid": req[0]})
                    conn.commit()
                    st.rerun()

        st.subheader("Your Friends")
        with engine.connect() as conn:
            friends_res = conn.execute(text("""
                SELECT u.username, u.id FROM users u
                JOIN friendships f ON (u.id = f.friend_id OR u.id = f.user_id)
                WHERE ((f.user_id = :uid OR f.friend_id = :uid) AND f.status = 'accepted')
                AND u.id != :uid
            """), {"uid": current_uid}).fetchall()
            accepted_friend_names = [fr[0] for fr in friends_res]
            for name in accepted_friend_names: st.write(f"✅ {name}")

    # --- PLANNING ---
    with plan_tab:
        try:
            with engine.connect() as conn:
                df_parks = pd.read_sql(text("SELECT name, id, image_url FROM parks ORDER BY name"), conn)
                park_names = df_parks['name'].tolist()
        except: park_names = []

        if not park_names: st.warning("No parks found.")
        else:
            p_sel = st.selectbox("Select Park", options=park_names)
            
            # --- UPDATED DATE PICKER ---
            st.write("Select Trip Dates:")
            date_range = st.date_input(
                "Pick a start and end date",
                value=(date.today(), date.today()),
                min_value=date.today(),
                format="MM/DD/YYYY"
            )
            
            # Extract start and end from range
            start_dt = date_range[0] if len(date_range) > 0 else date.today()
            end_dt = date_range[1] if len(date_range) > 1 else start_dt
            nights_calc = (end_dt - start_dt).days
            
            if nights_calc < 1:
                st.caption("Please select a range of at least one night.")
            else:
                st.caption(f"Trip Duration: {nights_calc} nights")
            
            invited = st.multiselect("Invite Friends?", options=accepted_friend_names)

            if st.button("Generate & Save Trip"):
                if len(date_range) < 2:
                    st.error("Please select both a start and end date on the calendar.")
                else:
                    p_info = df_parks[df_parks['name'] == p_sel].iloc[0]
                    p_id = int(p_info['id'])

                    with engine.connect() as conn:
                        al_res = conn.execute(text("SELECT title, description FROM alerts WHERE park_id = :p AND isactive=True"), {"p": p_id}).fetchall()
                        active_alerts = [{"title": r[0], "description": r[1]} for r in al_res]

                    prompt = f"""
                    You are Ranger Gemini. Plan a trip to {p_sel}.
                    Dates: {start_dt} to {end_dt} ({nights_calc} nights).
                    Style: {st.session_state.user_info[5]}.
                    Alerts: {active_alerts}.
                    Professional day-by-day itinerary.
                    """
                    
                    with st.spinner("Ranger Gemini is planning..."):
                        resp_text = client.models.generate_content(model="gemini-3-flash-preview", contents=prompt).text
                        
                        try:
                            with engine.connect() as conn:
                                # 1. Create Trip (Saving real DATE objects now)
                                tid_res = conn.execute(text("""
                                    INSERT INTO trips (user_id, owner_id, trip_name, status, start_date, end_date) 
                                    VALUES (:u, :o, :n, 'planned', :sd, :ed) RETURNING id
                                """), {
                                    "u": current_uid, 
                                    "o": current_uid, 
                                    "n": f"{p_sel} Adventure",
                                    "sd": start_dt,
                                    "ed": end_dt
                                })
                                tid = tid_res.fetchone()[0]

                                # 2. Participants
                                conn.execute(text("INSERT INTO trip_participants (trip_id, user_id, role, invitation_status, invited_by) VALUES (:t, :u, 'owner', 'accepted', :u)"), {"t": tid, "u": current_uid})
                                for f_user in invited:
                                    fid = conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": f_user}).fetchone()[0]
                                    conn.execute(text("INSERT INTO trip_participants (trip_id, user_id, role, invitation_status, invited_by) VALUES (:t, :u, 'collaborator', 'pending', :by)"), {"t": tid, "u": fid, "by": current_uid})
                                
                                # 3. Save Park Link
                                conn.execute(text("INSERT INTO trip_parks (trip_id, park_id, notes) VALUES (:t, :p, :n)"), {"t": tid, "p": p_id, "n": resp_text})
                                conn.commit()
                                
                            st.divider()
                            if p_info['image_url']: 
                                st.image(p_info['image_url'], use_container_width=True)
                            
                            st.header(f"Your Itinerary for {p_sel}")
                            st.markdown(resp_text)
                            
                            st.divider()
                            if active_alerts:
                                with st.expander(f"⚠️ Important Safety Alerts", expanded=True):
                                    for a in active_alerts:
                                        st.subheader(a['title'])
                                        st.write(a['description'])
                                
                            pdf_bytes = create_pdf(resp_text, p_sel, st.session_state.user_info[2], active_alerts)
                            st.download_button("📥 Download PDF", pdf_bytes, f"{p_sel.replace(' ', '_')}.pdf", "application/pdf")
                        
                        except Exception as e:
                            st.error(f"Save Failed: {e}")

    # --- MY TRIPS ---
    with my_trips_tab:
        st.header("Your Saved Adventures")
        with engine.connect() as conn:
            my_trips = conn.execute(text("""
                SELECT 
                    t.trip_name, 
                    t.status, 
                    t.start_date, 
                    t.end_date,
                    tpk.notes AS itinerary  -- Pulling the saved AI text
                FROM trips t
                JOIN trip_participants tp ON t.id = tp.trip_id
                LEFT JOIN trip_parks tpk ON t.id = tpk.trip_id  -- Join to get the notes
                WHERE tp.user_id = :u 
                ORDER BY t.start_date DESC
            """), {"u": current_uid}).fetchall()
            
            if not my_trips:
                st.info("No trips yet!")
            for t in my_trips:
                date_str = f"{t[2]} to {t[3]}" if t[2] else "Dates not set"
                with st.expander(f"📍 {t[0]} ({date_str})"):
                    st.write(f"**Status:** {t[1]}")
                    st.divider()
                    # Display the itinerary if it exists
                    if t[4]:
                        st.markdown(t[4])
                    else:
                        st.info("No itinerary details found for this adventure.")
