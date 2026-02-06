import streamlit as st
import os
import pandas as pd
import time
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

# --- PDF GENERATION HELPER (With Sanitization for Latin-1) ---
def create_pdf(itinerary_text, park_name, user_name):
    # Step 1: Clean the text to avoid encoding errors
    # Replace common fancy Unicode characters with standard equivalents
    replacements = {
        '’': "'", '‘': "'",
        '”': '"', '“': '"',
        '—': '-', '–': '-',
        '•': '*', # Replace fancy bullets with asterisks
    }
    
    for char, replacement in replacements.items():
        itinerary_text = itinerary_text.replace(char, replacement)
    
    # Final safety net: Encode to latin-1 and ignore anything that can't be converted
    itinerary_text = itinerary_text.encode('latin-1', 'ignore').decode('latin-1')

    pdf = FPDF()
    pdf.add_page()
    
    # Title
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, f"National Park Adventure: {park_name}", ln=True, align='C')
    pdf.set_font("Helvetica", 'I', 12)
    pdf.cell(0, 10, f"Customized for {user_name}", ln=True, align='C')
    pdf.ln(10)
    
    # Itinerary Body
    pdf.set_font("Helvetica", size=11)
    # multi_cell handles text wrapping and new lines
    pdf.multi_cell(0, 7, itinerary_text)
    
    # Return bytes
    return bytes(pdf.output())

# --- DATABASE FETCH: Get Parks ---
engine = get_connection()
try:
    with engine.connect() as conn:
        df_all_parks = pd.read_sql(text("SELECT name, code, id, image_url FROM parks ORDER BY name"), conn)
        park_list = df_all_parks['name'].tolist()
except Exception as e:
    st.error("Could not connect to database. Make sure your ETL pipeline has run!")
    park_list = []

# 3. SIDEBAR: User Login & Registration
with st.sidebar:
    st.header("User Access")
    is_existing_user = st.checkbox("I already have an account", value=True)
    username_input = st.text_input("Username").strip()
    
    user_data = None

    if username_input:
        with engine.connect() as conn:
            if is_existing_user:
                query = text("SELECT * FROM users WHERE username = :u")
                df_user = pd.read_sql(query, conn, params={"u": username_input})
                if not df_user.empty:
                    user_data = df_user.iloc[0]
                    st.success(f"Welcome, {user_data['firstname']}!")
                else:
                    st.error("User not found.")
            else:
                st.info("New Account Details:")
                new_fname = st.text_input("First Name")
                new_lname = st.text_input("Last Name")
                new_email = st.text_input("Email")
                new_likes = st.text_area("Tell us about your travel style")
                
                if st.button("Create Account"):
                    try:
                        ins_query = text("""
                            INSERT INTO users (username, firstname, lastname, email, likes) 
                            VALUES (:u, :f, :ln, :e, :l)
                        """)
                        conn.execute(ins_query, {
                            "u": username_input, "f": new_fname, "ln": new_lname, "e": new_email, "l": new_likes
                        })
                        conn.commit()
                        st.success("Account created! Check the box above to log in.")
                    except Exception as e:
                        st.error(f"Error: {e}")

# 4. MAIN UI: Trip Planner
st.title("🌲 National Park Assistant")
st.write("Plan your next adventure with real-time park alerts and AI intelligence.")

if not park_list:
    st.warning("No parks found. Please run your ETL pipeline script first!")
else:
    st.header("Trip Details")
    col1, col2 = st.columns(2)
    
    with col1:
        selected_park_name = st.selectbox("Which National Park?", options=park_list)
    with col2:
        visit_dates = st.text_input("When are you going?", placeholder="e.g., Early July")
    
    stay_nights = st.number_input("How many nights?", min_value=1, value=3)

    if st.button("Generate My Custom Itinerary"):
        if user_data is None:
            st.warning("Please sign in or create an account in the sidebar first.")
        else:
            selected_park_info = df_all_parks[df_all_parks['name'] == selected_park_name].iloc[0]
            park_id = int(selected_park_info['id'])

            with engine.connect() as conn:
                alert_query = text("SELECT title, description FROM alerts WHERE park_id = :pid AND isactive = True")
                df_alerts = pd.read_sql(alert_query, conn, params={"pid": park_id})
                alerts_str = df_alerts.to_string(index=False) if not df_alerts.empty else "No active alerts."

            prompt = f"""
            System: You are an expert National Park guide.
            User Profile: {user_data['firstname']} likes {user_data['likes']}.
            Trip: {selected_park_name} for {stay_nights} nights in {visit_dates}.
            Safety/Alerts to consider: {alerts_str}
            
            Task: Provide a detailed day-by-day itinerary taking into consideration the user's likes.
            """

            with st.spinner(f"Consulting the experts for {selected_park_name}..."):
                response_text = ""
                success = False
                for attempt in range(3):
                    try:
                        response = client.models.generate_content(
                            model="gemini-3-flash-preview",
                            contents=prompt
                        )
                        response_text = response.text
                        success = True
                        break
                    except Exception as e:
                        if "503" in str(e):
                            time.sleep(2)
                            continue
                        else:
                            st.error(f"AI Error: {e}")
                            break
                
                if success:
                    st.divider()
                    
                    # Display Photo
                    if selected_park_info['image_url']:
                        st.image(selected_park_info['image_url'], use_container_width=True)
                    
                    st.header(f"Your Itinerary for {selected_park_name}")
                    st.markdown(response_text)
                    
                    # --- DOWNLOAD SECTION ---
                    st.subheader("Take it with you!")
                    # This now uses the sanitized text version
                    pdf_bytes = create_pdf(response_text, selected_park_name, user_data['firstname'])
                    
                    st.download_button(
                        label="📥 Download Itinerary as PDF",
                        data=pdf_bytes,
                        file_name=f"{selected_park_name.replace(' ', '_')}_Itinerary.pdf",
                        mime="application/pdf"
                    )
                else:
                    st.error("AI is overloaded. Try again in a second!")