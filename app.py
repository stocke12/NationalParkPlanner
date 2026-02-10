import streamlit as st
import os
import pandas as pd
import time
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
    st.error("❌ DATABASE_URL is missing or incorrect. Please check your .env or Streamlit Secrets.")
    st.stop()

# --- DATABASE HELPERS ---
def log_error_to_db(username, error_msg, trace):
    """Saves app errors to the database for debugging."""
    try:
        with engine.connect() as conn:
            query = text("""
                INSERT INTO error_logs (username, error_message, stack_trace)
                VALUES (:u, :e, :s)
            """)
            conn.execute(query, {"u": username, "e": error_msg, "s": trace})
            conn.commit()
    except Exception as db_e:
        print(f"Failed to log error to DB: {db_e}")

# --- PDF GENERATION HELPER ---
def create_pdf(itinerary_text, park_name, user_name, alerts_list):
    replacements = {
        '’': "'", '‘': "'", '”': '"', '“': '"',
        '—': '-', '–': '-', '•': '*',
    }
    for char, replacement in replacements.items():
        itinerary_text = itinerary_text.replace(char, replacement)
    
    itinerary_text = itinerary_text.encode('latin-1', 'ignore').decode('latin-1')
    pdf = FPDF()
    pdf.add_page()
    
    # Title
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, f"National Park Adventure: {park_name}", ln=True, align='C')
    pdf.set_font("Helvetica", 'I', 12)
    pdf.cell(0, 10, f"Customized for {user_name}", ln=True, align='C')
    pdf.ln(10)
    
    # Itinerary Content
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 7, itinerary_text)
    
    # Add Alerts to PDF if they exist
    if alerts_list:
        pdf.ln(10)
        pdf.set_font("Helvetica", 'B', 14)
        pdf.cell(0, 10, "Current Safety Alerts:", ln=True)
        pdf.set_font("Helvetica", size=10)
        for alert in alerts_list:
            pdf.multi_cell(0, 7, f"- {alert['title']}: {alert['description']}")
            pdf.ln(2)

    return bytes(pdf.output())

# --- LOGIN / REGISTRATION PAGE ---
if not st.session_state.logged_in:
    st.title("🌲 National Park Planner 🐻")
    st.subheader("Plan your adventure. But first, let's get to know you.")
    
    tab1, tab2 = st.tabs(["Login", "Create Account"])
    
    with tab1:
        login_user = st.text_input("Username", key="login_user").strip().lower()
        if st.button("Login"):
            if login_user:
                try:
                    with engine.connect() as conn:
                        query = text("SELECT * FROM users WHERE username = :u")
                        res = conn.execute(query, {"u": login_user}).fetchone()
                        if res:
                            st.session_state.user_info = res
                            st.session_state.logged_in = True
                            st.rerun()
                        else:
                            st.error("User not found. Please create an account.")
                except Exception as e:
                    st.error(f"Login error: {e}")
            else:
                st.warning("Please enter a username.")

    with tab2:
        new_user = st.text_input("Choose a Username", key="new_user").strip().lower()
        new_fname = st.text_input("First Name")
        new_lname = st.text_input("Last Name")
        new_email = st.text_input("Email")
        new_likes = st.text_area("What's your travel style? (e.g. hiking, luxury, photography)")
        
        if st.button("Sign Up"):
            if new_user and new_fname and new_email:
                try:
                    with engine.connect() as conn:
                        ins_query = text("""
                            INSERT INTO users (username, firstname, lastname, email, likes) 
                            VALUES (:u, :f, :ln, :e, :l)
                        """)
                        conn.execute(ins_query, {
                            "u": new_user, "f": new_fname, "ln": new_lname, "e": new_email, "l": new_likes
                        })
                        conn.commit()
                        st.success("Account created! You can now log in.")
                except Exception as e:
                    error_trace = traceback.format_exc()
                    log_error_to_db(new_user, str(e), error_trace)
                    st.error("Username already taken or database error.")
            else:
                st.warning("Please fill in required fields (Username, First Name, Email).")

# --- MAIN APP PAGE ---
else:
    with st.sidebar:
        st.write(f"Logged in as: **{st.session_state.user_info[2]}**")
        if st.button("Log Out"):
            st.session_state.logged_in = False
            st.session_state.user_info = None
            st.rerun()

    st.title("🌲 Park Planner 🐻")
    
    try:
        with engine.connect() as conn:
            df_all_parks = pd.read_sql(text("SELECT name, code, id, image_url FROM parks ORDER BY name"), conn)
            park_list = df_all_parks['name'].tolist()
    except Exception as e:
        error_trace = traceback.format_exc()
        log_error_to_db(st.session_state.user_info[1], str(e), error_trace)
        st.error("Database error fetching parks.")
        park_list = []

    if not park_list:
        st.warning("No parks found in database. Run the ETL script to sync data.")
    else:
        st.header(f"Welcome back, {st.session_state.user_info[2]}!")
        
        col1, col2 = st.columns(2)
        with col1:
            selected_park_name = st.selectbox("Which National Park?", options=park_list)
        with col2:
            visit_dates = st.text_input("When are you going?", placeholder="e.g., Early July")
        
        stay_nights = st.number_input("How many nights?", min_value=1, value=3)

        if st.button("Generate My Custom Itinerary"):
            selected_park_info = df_all_parks[df_all_parks['name'] == selected_park_name].iloc[0]
            park_id = int(selected_park_info['id'])

            # Fetch active alerts for the selected park
            with engine.connect() as conn:
                alert_query = text("SELECT title, description FROM alerts WHERE park_id = :pid AND isactive = True")
                res = conn.execute(alert_query, {"pid": park_id}).fetchall()
                active_alerts = [{"title": row[0], "description": row[1]} for row in res]

            alerts_text_for_ai = "\n".join([f"- {a['title']}: {a['description']}" for a in active_alerts]) if active_alerts else "No active alerts."

            prompt = f"""
            System: You are an expert National Park guide.
            User Profile: {st.session_state.user_info[2]} likes {st.session_state.user_info[5]}.
            Trip: {selected_park_name} for {stay_nights} nights in {visit_dates}.
            Current Safety/Alerts: {alerts_text_for_ai}
            
            Task: Provide a detailed day-by-day itinerary tailored to the user's travel style. Mention specific alerts if they impact the activities.
            """

            with st.spinner(f"Consulting our Park Ranger AI for {selected_park_name}..."):
                try:
                    response = client.models.generate_content(
                        model="gemini-3-flash-preview",
                        contents=prompt
                    )
                    response_text = response.text
                    
                    st.divider()
                    
                    # Display Park Image
                    if selected_park_info['image_url']:
                        st.image(selected_park_info['image_url'], use_container_width=True)

                    st.header(f"Your Itinerary for {selected_park_name}")
                    st.markdown(response_text)
                    
                    # --- ALERT SECTION AT THE END ---
                    st.divider()
                    if active_alerts:
                        with st.expander(f"⚠️ Important Safety Alerts for {selected_park_name}", expanded=True):
                            for alert in active_alerts:
                                st.subheader(alert['title'])
                                st.write(alert['description'])
                    else:
                        st.success("✅ No active alerts reported for this park! Enjoy your trip.")

                    # Generate and allow PDF Download
                    pdf_bytes = create_pdf(response_text, selected_park_name, st.session_state.user_info[2], active_alerts)
                    st.download_button(
                        label="📥 Download PDF Itinerary",
                        data=pdf_bytes,
                        file_name=f"{selected_park_name.replace(' ', '_')}.pdf",
                        mime="application/pdf"
                    )
                except Exception as e:
                    error_trace = traceback.format_exc()
                    username = st.session_state.user_info[1] if st.session_state.user_info else "Unknown"
                    log_error_to_db(username, str(e), error_trace)
                    st.error("Uh oh... Gemini is taking a hike. Please try again in a moment!")