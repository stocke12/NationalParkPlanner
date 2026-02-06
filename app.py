import streamlit as st
import os
import pandas as pd
import time
import traceback  # New: for capturing full error details
from dotenv import load_dotenv
from google import genai
from sqlalchemy import text
from database import get_connection
from fpdf import FPDF

# 1. SETUP: Load environment and AI Client
load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# 2. CONFIG: Page styling
st.set_page_config(page_title="National Park Planner", page_icon="🌲", layout="centered")

# --- SESSION STATE INITIALIZATION ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_info" not in st.session_state:
    st.session_state.user_info = None

# --- DATABASE HELPERS ---
def log_error_to_db(username, error_msg, trace):
    """Saves app errors to the database for debugging."""
    try:
        engine = get_connection()
        with engine.connect() as conn:
            query = text("""
                INSERT INTO error_logs (username, error_message, stack_trace)
                VALUES (:u, :e, :s)
            """)
            conn.execute(query, {"u": username, "e": error_msg, "s": trace})
            conn.commit()
    except Exception as db_e:
        # Fallback to console if the database itself is failing
        print(f"Failed to log error to DB: {db_e}")

# --- PDF GENERATION HELPER ---
def create_pdf(itinerary_text, park_name, user_name):
    replacements = {
        '’': "'", '‘': "'", '”': '"', '“': '"',
        '—': '-', '–': '-', '•': '*',
    }
    for char, replacement in replacements.items():
        itinerary_text = itinerary_text.replace(char, replacement)
    
    itinerary_text = itinerary_text.encode('latin-1', 'ignore').decode('latin-1')
    pdf = FPDF()
    pdf.add_page()
    
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, f"National Park Adventure: {park_name}", ln=True, align='C')
    pdf.set_font("Helvetica", 'I', 12)
    pdf.cell(0, 10, f"Customized for {user_name}", ln=True, align='C')
    pdf.ln(10)
    
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 7, itinerary_text)
    return bytes(pdf.output())

# --- DATABASE CONNECTION ---
engine = get_connection()

# --- LOGIN / REGISTRATION PAGE ---
if not st.session_state.logged_in:
    st.title("🌲 National Park Planner 🐻")
    st.subheader("Plan your adventure. But first, let's get to know you.")
    
    tab1, tab2 = st.tabs(["Login", "Create Account"])
    
    with tab1:
        login_user = st.text_input("Username", key="login_user").strip().lower()
        if st.button("Login"):
            if login_user:
                with engine.connect() as conn:
                    query = text("SELECT * FROM users WHERE username = :u")
                    res = conn.execute(query, {"u": login_user}).fetchone()
                    if res:
                        st.session_state.user_info = res
                        st.session_state.logged_in = True
                        st.rerun()
                    else:
                        st.error("User not found. Please create an account.")
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
        st.warning("No parks found in database.")
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

            with engine.connect() as conn:
                alert_query = text("SELECT title, description FROM alerts WHERE park_id = :pid AND isactive = True")
                df_alerts = pd.read_sql(alert_query, conn, params={"pid": park_id})
                alerts_str = df_alerts.to_string(index=False) if not df_alerts.empty else "No active alerts."

            prompt = f"""
            System: You are an expert National Park guide.
            User Profile: {st.session_state.user_info[2]} likes {st.session_state.user_info[5]}.
            Trip: {selected_park_name} for {stay_nights} nights in {visit_dates}.
            Safety/Alerts: {alerts_str}
            
            Task: Provide a detailed day-by-day itinerary tailored to the user's travel style.
            """

            with st.spinner(f"Creating your adventure for {selected_park_name}..."):
                try:
                    response = client.models.generate_content(
                        model="gemini-2.0-flash",
                        contents=prompt
                    )
                    response_text = response.text
                    
                    st.divider()
                    
                    if selected_park_info['image_url']:
                        st.image(selected_park_info['image_url'], use_container_width=True)
                    else:
                        st.info("📷 Beautiful imagery for this park is being curated!")

                    st.header(f"Your Itinerary for {selected_park_name}")
                    st.markdown(response_text)
                    
                    pdf_bytes = create_pdf(response_text, selected_park_name, st.session_state.user_info[2])
                    st.download_button(
                        label="📥 Download PDF Itinerary",
                        data=pdf_bytes,
                        file_name=f"{selected_park_name.replace(' ', '_')}.pdf",
                        mime="application/pdf"
                    )
                except Exception as e:
                    # Log the full error to your database
                    error_trace = traceback.format_exc()
                    username = st.session_state.user_info[1] if st.session_state.user_info else "Unknown"
                    log_error_to_db(username, str(e), error_trace)
                    
                    st.error("Uh oh... something went wrong! Please try again later!")