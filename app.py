import streamlit as st
import os
import re
import json
import bcrypt
import pandas as pd
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from google import genai
from sqlalchemy import text
from database import get_connection
from fpdf import FPDF

# ─────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    st.error("Missing GEMINI_API_KEY.")
    st.stop()

client = genai.Client(api_key=GEMINI_API_KEY)

st.set_page_config(page_title="National Park Planner", page_icon="🌲", layout="wide")

for key, val in {
    "logged_in": False,
    "user_info": None,
    "temp_activities": [],
    "master_itinerary": "",
    "day_activities": {},
    "nights": 0,
    "trip_start": None,
    "trip_end": None,
    "activity_day_defaults": {},
    "park_distances": [],
    "conflict_warnings": {},
    "active_parks_saved": [],
    # auth flow state
    "auth_screen": "login",  # login | set_password | force_change | reset | signup
    "pending_uid": None,     # user id mid-flow
}.items():
    if key not in st.session_state:
        st.session_state[key] = val

@st.cache_resource
def get_engine():
    return get_connection()

engine = get_engine()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def trip_status(start_d, end_d):
    """Return (emoji, label, color) for a trip based on today's date."""
    today = date.today()
    s = start_d if isinstance(start_d, date) else date.fromisoformat(str(start_d)) if start_d else None
    e = end_d   if isinstance(end_d,   date) else date.fromisoformat(str(end_d))   if end_d   else None
    if not s or not e:
        return ("📅", "Upcoming", "gray")
    if today < s:
        days_away = (s - today).days
        label = f"Upcoming · {days_away}d away" if days_away <= 30 else "Upcoming"
        return ("📅", label, "blue")
    if s <= today <= e:
        return ("🟢", "Happening Now!", "green")
    return ("✅", "Completed", "gray")

def create_pdf(itinerary_text, park_name, user_name):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", 'B', 16)
    pdf.cell(0, 10, f"Adventure: {park_name}", ln=True, align='C')
    pdf.ln(10)
    pdf.set_font("Helvetica", size=11)
    clean_text = (itinerary_text
                  .replace('\u2013', '-').replace('\u2019', "'")
                  .replace('\u201c', '"').replace('\u201d', '"'))
    pdf.multi_cell(0, 7, clean_text.encode('latin-1', 'ignore').decode('latin-1'))
    return bytes(pdf.output())

def get_pending_trip_invites(conn, uid):
    return conn.execute(text("""
        SELECT tp.id AS participant_id, t.id AS trip_id, t.trip_name,
               t.start_date, t.end_date,
               u_owner.firstname || ' ' || u_owner.lastname AS invited_by_name,
               STRING_AGG(p.name, ', ' ORDER BY p.name) AS park_names
        FROM trip_participants tp
        JOIN trips t ON tp.trip_id = t.id
        JOIN users u_owner ON t.owner_id = u_owner.id
        LEFT JOIN trip_parks tpk ON t.id = tpk.trip_id
        LEFT JOIN parks p ON tpk.park_id = p.id
        WHERE tp.user_id = :uid AND tp.invitation_status = 'pending' AND tp.role != 'owner'
        GROUP BY tp.id, t.id, t.trip_name, t.start_date, t.end_date, u_owner.firstname, u_owner.lastname
    """), {"uid": uid}).fetchall()

def get_pending_count(uid):
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT
                (SELECT COUNT(*) FROM friendships WHERE friend_id=:uid AND status='pending') +
                (SELECT COUNT(*) FROM trip_participants WHERE user_id=:uid AND invitation_status='pending' AND role!='owner')
            AS total
        """), {"uid": uid}).scalar()
    return result or 0

def can_edit(role):
    return role in ('owner', 'collaborator')

def date_range_days(start, end):
    if not start or not end:
        return []
    days = []
    current = start if isinstance(start, date) else date.fromisoformat(str(start))
    end_d = end if isinstance(end, date) else date.fromisoformat(str(end))
    day = 1
    while current <= end_d:
        days.append((day, current))
        current += timedelta(days=1)
        day += 1
    return days

def get_trip_parks(conn, trip_id):
    return conn.execute(text("""
        SELECT tpk.id AS trip_park_id, tpk.park_id, p.name AS park_name,
               p.image_url, tpk.notes
        FROM trip_parks tpk
        JOIN parks p ON tpk.park_id = p.id
        WHERE tpk.trip_id = :tid
        ORDER BY p.name
    """), {"tid": trip_id}).fetchall()

def parse_activity_day_defaults(master_itinerary, num_days):
    day_map = {}
    if not master_itinerary:
        return day_map
    current_day = 1
    for line in master_itinerary.split('\n'):
        line_stripped = line.strip()
        day_match = re.search(r'\bday\s*(\d+)\b', line_stripped, re.IGNORECASE)
        if day_match:
            detected_day = int(day_match.group(1))
            if 1 <= detected_day <= num_days:
                current_day = detected_day
        if line_stripped and not day_match:
            day_map[line_stripped.lower()] = current_day
    return day_map

def guess_day_for_activity(activity_name, day_map, default_day=1):
    name_lower = activity_name.lower()
    for line_text, day_num in day_map.items():
        if name_lower in line_text or line_text in name_lower:
            return day_num
    name_words = set(name_lower.split())
    best_overlap = 0
    best_day = default_day
    for line_text, day_num in day_map.items():
        line_words = set(line_text.split())
        overlap = len(name_words & line_words)
        if overlap > best_overlap and overlap >= 2:
            best_overlap = overlap
            best_day = day_num
    return best_day

# ─────────────────────────────────────────────
# CONFLICT DETECTION
# ─────────────────────────────────────────────

STRENUOUS_KEYWORDS = {"hike", "hiking", "climb", "climbing", "backpack", "backpacking",
                      "trail", "summit", "scramble", "trek", "trekking", "rafting", "kayak"}
NIGHT_KEYWORDS = {"stargazing", "night", "sunset", "campfire", "evening", "dusk", "bonfire"}
EARLY_KEYWORDS = {"sunrise", "dawn", "morning", "early", "ranger walk"}
WATER_KEYWORDS = {"swim", "swimming", "snorkel", "diving", "kayak", "rafting", "canoe"}

def classify_activity(name, atype):
    text_lower = (name + " " + atype).lower()
    tags = set()
    if any(k in text_lower for k in STRENUOUS_KEYWORDS):
        tags.add("strenuous")
    if any(k in text_lower for k in NIGHT_KEYWORDS):
        tags.add("night")
    if any(k in text_lower for k in EARLY_KEYWORDS):
        tags.add("early")
    if any(k in text_lower for k in WATER_KEYWORDS):
        tags.add("water")
    return tags

def compute_conflict_warnings(day_activities):
    warnings = {}
    for day_num, acts in day_activities.items():
        day_warnings = []
        strenuous = [a for a in acts if "strenuous" in classify_activity(a["name"], a.get("type", ""))]
        night_acts = [a for a in acts if "night" in classify_activity(a["name"], a.get("type", ""))]
        early_acts = [a for a in acts if "early" in classify_activity(a["name"], a.get("type", ""))]
        if len(strenuous) >= 3:
            names = ", ".join(a["name"] for a in strenuous)
            day_warnings.append(f"🥵 **Heavy day!** 3+ strenuous activities: {names}")
        elif len(strenuous) == 2:
            names = " & ".join(a["name"] for a in strenuous)
            day_warnings.append(f"⚠️ **Back-to-back effort:** {names} — consider spacing these out")
        if night_acts and early_acts:
            n = night_acts[0]["name"]
            e = early_acts[0]["name"]
            day_warnings.append(f"😴 **Sleep conflict:** '{e}' (early start) and '{n}' (late night) on the same day")
        if day_warnings:
            warnings[day_num] = day_warnings
    return warnings

# ─────────────────────────────────────────────
# AI HELPERS
# ─────────────────────────────────────────────

def fetch_park_distances(park_names):
    if len(park_names) < 2:
        return []
    pairs = [(park_names[i], park_names[i+1]) for i in range(len(park_names)-1)]
    pair_text = "\n".join(f"- {a} to {b}" for a, b in pairs)
    prompt = f"""For each pair of US National Parks below, provide the approximate driving distance and time.
Respond ONLY as a JSON array, no markdown, no extra text. Each element:
{{"from": "...", "to": "...", "drive_time": "e.g. 3h 20min", "distance_miles": 210, "tip": "one short travel tip"}}
Pairs:\n{pair_text}"""
    try:
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt).text
        return json.loads(re.sub(r"```json|```", "", resp).strip())
    except Exception:
        return []

def generate_packing_list(park_names, activity_types, num_days):
    parks_str = ", ".join(park_names)
    acts_str = ", ".join(set(activity_types)) if activity_types else "general sightseeing"
    prompt = f"""Generate a practical packing list for a {num_days}-day trip to {parks_str}.
Activities include: {acts_str}.
Respond ONLY as a JSON array, no markdown, no extra text. Each element:
{{"category": "e.g. Clothing", "item": "e.g. Moisture-wicking shirt x3"}}
Include 25-35 items across: Clothing, Footwear, Navigation, Safety, Camping/Shelter, Food & Water, Photography, Personal Care, Documents."""
    try:
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt).text
        return json.loads(re.sub(r"```json|```", "", resp).strip())
    except Exception:
        return []

def generate_trip_recap(trip_name, park_names, activities, notes, start_date, end_date):
    parks_str = ", ".join(park_names)
    acts_str = "\n".join(f"- Day {a['day']}: {a['name']}" for a in activities[:20])
    notes_str = "\n".join(f"- Day {n['day']}: {n['text']}" for n in notes[:15])
    num_days = (end_date - start_date).days + 1 if start_date and end_date else "?"
    prompt = f"""Write a vivid, personal trip recap narrative for a {num_days}-day national park adventure.

Trip: {trip_name}
Parks visited: {parks_str}
Activities:
{acts_str}
Journal notes from the trip:
{notes_str}

Write 3-4 paragraphs in a warm, storytelling style — like a travel journal entry. Mention specific activities and any notes. End with a memorable closing line."""
    try:
        return client.models.generate_content(model="gemini-2.5-flash", contents=prompt).text.strip()
    except Exception:
        return ""

# ─────────────────────────────────────────────
# STATS / BADGES
# ─────────────────────────────────────────────

BADGE_DEFINITIONS = [
    ("first_trip",   "🏕️ First Adventure", "Saved your first trip"),
    ("5_trips",      "🗺️ Road Warrior",    "Completed 5 trips"),
    ("10_trips",     "🌟 Park Legend",      "Completed 10 trips"),
    ("3_parks",      "🌲 Park Hopper",      "Visited 3 different parks"),
    ("10_parks",     "🦅 Eagle Scout",      "Visited 10 different parks"),
    ("5_states",     "🚗 State Tripper",    "Visited parks in 5 states"),
    ("first_friend", "🤝 Trail Buddy",      "Made your first friend"),
    ("group_trip",   "👥 Pack Leader",      "Went on a group trip"),
    ("note_taker",   "📝 Chronicler",       "Wrote 10 journal notes"),
    ("all_packer",   "🎒 Overpacker",       "Checked off a full packing list"),
]

def compute_and_award_badges(uid):
    with engine.connect() as conn:
        trip_count = conn.execute(text("""
            SELECT COUNT(*) FROM trips t JOIN trip_participants tp ON t.id=tp.trip_id
            WHERE tp.user_id=:uid AND tp.invitation_status='accepted' AND tp.role='owner'
        """), {"uid": uid}).scalar() or 0
        park_count = conn.execute(text("""
            SELECT COUNT(DISTINCT tpk.park_id) FROM trip_parks tpk
            JOIN trips t ON tpk.trip_id=t.id JOIN trip_participants tp ON t.id=tp.trip_id
            WHERE tp.user_id=:uid AND tp.invitation_status='accepted'
        """), {"uid": uid}).scalar() or 0
        state_count = conn.execute(text("""
            SELECT COUNT(DISTINCT p.state) FROM trip_parks tpk
            JOIN trips t ON tpk.trip_id=t.id JOIN trip_participants tp ON t.id=tp.trip_id
            JOIN parks p ON tpk.park_id=p.id
            WHERE tp.user_id=:uid AND tp.invitation_status='accepted'
        """), {"uid": uid}).scalar() or 0
        friend_count = conn.execute(text("""
            SELECT COUNT(*) FROM friendships WHERE (user_id=:uid OR friend_id=:uid) AND status='accepted'
        """), {"uid": uid}).scalar() or 0
        group_trip = conn.execute(text("""
            SELECT COUNT(*) FROM trips t JOIN trip_participants tp ON t.id=tp.trip_id
            WHERE t.owner_id=:uid AND (
                SELECT COUNT(*) FROM trip_participants WHERE trip_id=t.id AND invitation_status='accepted') > 1
        """), {"uid": uid}).scalar() or 0
        note_count = conn.execute(text("""
            SELECT COUNT(*) FROM trip_day_notes WHERE author_id=:uid
        """), {"uid": uid}).scalar() or 0
        full_packing = conn.execute(text("""
            SELECT COUNT(*) FROM trip_packing_items tpi JOIN trips t ON tpi.trip_id=t.id
            JOIN trip_participants tp ON t.id=tp.trip_id
            WHERE tp.user_id=:uid AND tp.role='owner'
              AND t.id IN (
                SELECT trip_id FROM trip_packing_items GROUP BY trip_id
                HAVING COUNT(*)>0 AND COUNT(*)=SUM(CASE WHEN is_checked THEN 1 ELSE 0 END))
        """), {"uid": uid}).scalar() or 0

    conditions = {
        "first_trip": trip_count>=1, "5_trips": trip_count>=5, "10_trips": trip_count>=10,
        "3_parks": park_count>=3, "10_parks": park_count>=10, "5_states": state_count>=5,
        "first_friend": friend_count>=1, "group_trip": group_trip>=1,
        "note_taker": note_count>=10, "all_packer": full_packing>=1,
    }
    newly_earned = []
    badge_map = {b[0]: b for b in BADGE_DEFINITIONS}
    with engine.connect() as conn:
        existing = {r[0] for r in conn.execute(
            text("SELECT badge_key FROM user_badges WHERE user_id=:uid"), {"uid": uid}).fetchall()}
    for key, earned in conditions.items():
        if earned and key not in existing:
            bdef = badge_map[key]
            try:
                with engine.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO user_badges (user_id, badge_key, badge_name, badge_description)
                        VALUES (:uid,:key,:name,:desc) ON CONFLICT (user_id,badge_key) DO NOTHING
                    """), {"uid": uid, "key": key, "name": bdef[1], "desc": bdef[2]})
                newly_earned.append(bdef[1])
            except Exception:
                pass
    return newly_earned

# ─────────────────────────────────────────────
# DRAG-AND-DROP ITINERARY COMPONENT
# ─────────────────────────────────────────────

def render_dnd_itinerary(day_activities, days, editable=True, conflict_warnings=None):
    if conflict_warnings is None:
        conflict_warnings = {}
    days_data = []
    for day_num, day_date in days:
        acts = day_activities.get(day_num, [])
        cw = conflict_warnings.get(day_num, [])
        days_data.append({
            "day": day_num,
            "label": f"Day {day_num} — {day_date.strftime('%a, %b %d')}",
            "activities": [{"id": a["id"], "name": a["name"], "type": a.get("type", "Activity")} for a in acts],
            "warnings": cw,
        })

    delete_btn = '<button class="delete-btn" onclick="deleteActivity(this)" title="Remove">✕</button>' if editable else ""
    draggable_attr = "draggable='true' ondragstart='handleDragStart(event, this)'" if editable else ""
    cursor_style = "cursor: grab;" if editable else ""

    def warning_html(warnings):
        if not warnings:
            return ""
        items = "".join(f'<div class="warn-item">{w}</div>' for w in warnings)
        return f'<div class="day-warnings">{items}</div>'

    def build_day_html(d):
        warn = warning_html(d["warnings"])
        has_warn_class = " has-warning" if d["warnings"] else ""
        acts_html = ""
        for a in d["activities"]:
            acts_html += (
                f'''<div class="activity-card" {draggable_attr} data-id="{a["id"]}" data-day="{d["day"]}">'''
                f'''<div><div class="act-name">{a["name"]}</div><div class="act-type">{a["type"]}</div></div>'''
                f'''{delete_btn}</div>'''
            )
        return (
            f'''<div class="day-col{has_warn_class}" id="day-{d["day"]}" '''
            f'''ondragover="event.preventDefault(); this.querySelector(\'.drop-zone\').classList.add(\'drag-over\')" '''
            f'''ondragleave="this.querySelector(\'.drop-zone\').classList.remove(\'drag-over\')" '''
            f'''ondrop="handleDrop(event, {d["day"]})">'''
            f'''<div class="day-header">{d["label"]}</div>{warn}{acts_html}<div class="drop-zone"></div></div>'''
        )

    days_html = "".join(build_day_html(d) for d in days_data)

    html = f"""
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: 'Segoe UI', sans-serif; background: transparent; }}
        .itinerary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 12px; padding: 4px;
        }}
        .day-col {{ background: #f0f4f8; border-radius: 10px; padding: 10px; min-height: 120px; }}
        .day-col.has-warning {{ background: #fff8e1; border: 1px solid #ffe082; }}
        .day-header {{ font-size: 0.8em; font-weight: 700; color: #2d6a4f; margin-bottom: 8px;
            padding-bottom: 6px; border-bottom: 2px solid #2d6a4f; text-transform: uppercase; letter-spacing: 0.5px; }}
        .day-warnings {{ margin-bottom: 8px; }}
        .warn-item {{ font-size: 0.75em; color: #7b5800; background: #fff3cd; border-left: 3px solid #ffc107;
            border-radius: 4px; padding: 4px 7px; margin-bottom: 4px; line-height: 1.3; }}
        .activity-card {{ background: white; border-radius: 6px; padding: 8px 10px; margin-bottom: 6px;
            font-size: 0.82em; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: flex;
            justify-content: space-between; align-items: center; {cursor_style}
            border-left: 3px solid #52b788; transition: box-shadow 0.15s; }}
        .activity-card.dragging {{ opacity: 0.5; cursor: grabbing; box-shadow: 0 4px 12px rgba(0,0,0,0.2); }}
        .activity-card:hover {{ box-shadow: 0 3px 8px rgba(0,0,0,0.15); }}
        .act-name {{ font-weight: 600; color: #1b4332; }}
        .act-type {{ color: #888; font-size: 0.85em; margin-top: 2px; }}
        .delete-btn {{ background: none; border: none; color: #ccc; cursor: pointer; font-size: 1em; padding: 0 2px; line-height: 1; }}
        .delete-btn:hover {{ color: #e74c3c; }}
        .drop-zone {{ min-height: 40px; border-radius: 6px; border: 2px dashed transparent;
            transition: border-color 0.2s, background 0.2s; margin-top: 4px; }}
        .drop-zone.drag-over {{ border-color: #52b788; background: #d8f3dc; }}
        #result {{ display: none; }}
    </style>
    <div class="itinerary-grid" id="itinerary">{days_html}</div>
    <textarea id="result"></textarea>
    <script>
        let dragSrc = null;
        function handleDragStart(e, el) {{ dragSrc = el; el.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; }}
        document.addEventListener('dragend', () => {{
            document.querySelectorAll('.activity-card').forEach(c => c.classList.remove('dragging'));
            document.querySelectorAll('.drop-zone').forEach(z => z.classList.remove('drag-over'));
        }});
        function handleDrop(e, newDay) {{
            e.preventDefault();
            if (!dragSrc) return;
            const dayCol = document.getElementById('day-' + newDay);
            const dropZone = dayCol.querySelector('.drop-zone');
            dropZone.classList.remove('drag-over');
            dragSrc.dataset.day = newDay;
            dayCol.insertBefore(dragSrc, dropZone);
            dragSrc = null;
            saveState();
        }}
        function deleteActivity(btn) {{ btn.closest('.activity-card').remove(); saveState(); }}
        function saveState() {{
            const state = {{}};
            document.querySelectorAll('.day-col').forEach(col => {{
                const day = col.id.replace('day-', '');
                state[day] = [];
                col.querySelectorAll('.activity-card').forEach(card => {{
                    state[day].push({{ id: card.dataset.id, name: card.querySelector('.act-name').textContent, type: card.querySelector('.act-type').textContent }});
                }});
            }});
            const ta = document.getElementById('result');
            ta.value = JSON.stringify(state);
            ta.dispatchEvent(new Event('input', {{ bubbles: true }}));
            window.parent.postMessage({{ type: 'streamlit:setComponentValue', value: JSON.stringify(state) }}, '*');
        }}
    </script>
    """
    result = st.components.v1.html(html, height=max(300, len(days) * 60 + 100), scrolling=False)
    return result


# ─────────────────────────────────────────────
# PASSWORD HELPERS
# ─────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()

def check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False

def validate_password(pw: str) -> str | None:
    """Return an error string if invalid, None if fine."""
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", pw):
        return "Password must contain at least one letter."
    if not re.search(r"[0-9]", pw):
        return "Password must contain at least one number."
    return None

def dob_to_password(dob: date) -> str:
    """Convert a date of birth to the MMDDYYYY reset password string."""
    return dob.strftime("%m%d%Y")

def is_dob_password(plain: str, dob: date) -> bool:
    """Check whether the supplied plain password matches the DOB reset value."""
    return plain == dob_to_password(dob)

# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────

if not st.session_state.logged_in:
    st.title("🌲 National Park Planner 🐻")

    screen = st.session_state.auth_screen

    # ── FORCE PASSWORD CHANGE (logged in via DOB reset or first-time setup) ─
    if screen in ("set_password", "force_change"):
        if screen == "set_password":
            st.subheader("🔒 Set your password")
            st.info("Your account was created before passwords were required. Please set a password to continue.")
        else:
            st.subheader("🔒 Please set a new password")
            st.warning("You logged in with your temporary password. Choose a new password to continue.")

        pw1 = st.text_input("New password", type="password", key="fc_pw1")
        pw2 = st.text_input("Confirm password", type="password", key="fc_pw2")
        if st.button("Set Password", use_container_width=True):
            err = validate_password(pw1)
            if err:
                st.error(err)
            elif pw1 != pw2:
                st.error("Passwords don't match.")
            else:
                with engine.begin() as conn:
                    conn.execute(
                        text("UPDATE users SET password_hash=:ph WHERE id=:uid"),
                        {"ph": hash_password(pw1), "uid": st.session_state.pending_uid}
                    )
                with engine.connect() as conn:
                    res = conn.execute(
                        text("SELECT id, username, firstname, lastname, email, likes, date_of_birth FROM users WHERE id=:uid"),
                        {"uid": st.session_state.pending_uid}
                    ).mappings().fetchone()
                st.session_state.user_info = {k: v for k, v in res.items() if k != "date_of_birth"}
                st.session_state.logged_in = True
                st.session_state.auth_screen = "login"
                st.session_state.pending_uid = None
                st.rerun()
        if st.button("Back to Login", type="tertiary"):
            st.session_state.auth_screen = "login"
            st.session_state.pending_uid = None
            st.rerun()

    # ── RESET PASSWORD (DOB lookup) ─────────────────────────────────────────
    elif screen == "reset":
        st.subheader("🔑 Reset your password")
        st.caption("Enter your username and date of birth. Your password will be reset to your DOB (MMDDYYYY) and you'll be prompted to change it on login.")
        reset_u   = st.text_input("Username").strip().lower()
        reset_dob = st.date_input("Date of Birth", min_value=date(1900, 1, 1), max_value=date.today(), value=None)
        if st.button("Reset Password", use_container_width=True):
            if not reset_u or not reset_dob:
                st.error("Please fill in both fields.")
            else:
                with engine.connect() as conn:
                    row = conn.execute(
                        text("SELECT id, date_of_birth FROM users WHERE username=:u"),
                        {"u": reset_u}
                    ).fetchone()
                # Same error for wrong username or wrong DOB — no enumeration
                if not row or not row.date_of_birth:
                    st.error("No account found with that username and date of birth.")
                else:
                    stored_dob = row.date_of_birth if isinstance(row.date_of_birth, date) else row.date_of_birth.date()
                    if stored_dob != reset_dob:
                        st.error("No account found with that username and date of birth.")
                    else:
                        # Reset password to MMDDYYYY
                        dob_pw = dob_to_password(stored_dob)
                        with engine.begin() as conn:
                            conn.execute(
                                text("UPDATE users SET password_hash=:ph WHERE id=:uid"),
                                {"ph": hash_password(dob_pw), "uid": row.id}
                            )
                        st.success(f"Password reset! Log in with your username and **{dob_pw}** — you'll be prompted to set a new password.")
                        st.session_state.auth_screen = "login"
                        st.rerun()
        if st.button("Back to Login", type="tertiary"):
            st.session_state.auth_screen = "login"
            st.rerun()

    # ── SIGN UP ─────────────────────────────────────────────────────────────
    elif screen == "signup":
        st.subheader("🌲 Create Account")
        nu  = st.text_input("Username").strip().lower()
        fn  = st.text_input("First Name")
        ln  = st.text_input("Last Name")
        em  = st.text_input("Email")
        dob = st.date_input("Date of Birth", min_value=date(1900, 1, 1), max_value=date.today(), value=None,
                             help="Used to reset your password if you forget it")
        lk  = st.text_area("Travel Style / Interests")
        pw1 = st.text_input("Password", type="password", key="su_pw1")
        pw2 = st.text_input("Confirm Password", type="password", key="su_pw2")
        if st.button("Sign Up", use_container_width=True):
            if not nu:
                st.error("Username is required.")
            elif not dob:
                st.error("Date of birth is required.")
            else:
                err = validate_password(pw1)
                if err:
                    st.error(err)
                elif pw1 != pw2:
                    st.error("Passwords don't match.")
                else:
                    try:
                        with engine.begin() as conn:
                            conn.execute(
                                text("INSERT INTO users (username, firstname, lastname, email, likes, password_hash, date_of_birth) VALUES (:u,:f,:ln,:e,:l,:ph,:dob)"),
                                {"u": nu, "f": fn, "ln": ln, "e": em, "l": lk, "ph": hash_password(pw1), "dob": dob}
                            )
                        st.success("Account created! You can now log in.")
                        st.session_state.auth_screen = "login"
                        st.rerun()
                    except Exception as e:
                        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                            st.error("That username is already taken.")
                        else:
                            st.error(f"Error: {e}")
        if st.button("Already have an account? Log in", type="tertiary"):
            st.session_state.auth_screen = "login"
            st.rerun()

    # ── LOGIN ────────────────────────────────────────────────────────────────
    else:
        st.subheader("Welcome back")
        u  = st.text_input("Username").strip().lower()
        pw = st.text_input("Password", type="password", key="login_pw")
        if st.button("Log In", use_container_width=True):
            with engine.connect() as conn:
                res = conn.execute(
                    text("SELECT id, username, firstname, lastname, email, likes, password_hash, date_of_birth FROM users WHERE username=:u"),
                    {"u": u}
                ).mappings().fetchone()

            if not res:
                st.error("Invalid username or password.")
            elif not res["password_hash"]:
                # Pre-password account — force setup without checking password
                st.session_state.pending_uid = res["id"]
                st.session_state.auth_screen = "set_password"
                st.rerun()
            elif not check_password(pw, res["password_hash"]):
                st.error("Invalid username or password.")
            else:
                # Valid password — check if they logged in with their DOB reset password
                dob = res["date_of_birth"]
                logged_in_with_dob = (
                    dob is not None and
                    is_dob_password(pw, dob if isinstance(dob, date) else dob.date() if hasattr(dob, 'date') else dob)
                )
                st.session_state.pending_uid = res["id"]
                st.session_state.user_info = {k: v for k, v in res.items() if k not in ("password_hash", "date_of_birth")}
                if logged_in_with_dob:
                    st.session_state.auth_screen = "force_change"
                    st.rerun()
                else:
                    st.session_state.logged_in = True
                    st.session_state.auth_screen = "login"
                    st.session_state.pending_uid = None
                    st.rerun()

        col1, col2 = st.columns(2)
        if col1.button("Create account", use_container_width=True):
            st.session_state.auth_screen = "signup"
            st.rerun()
        if col2.button("Forgot password?", use_container_width=True):
            st.session_state.auth_screen = "reset"
            st.rerun()

# ─────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────

else:
    current_uid = st.session_state.user_info['id']

    new_badges = compute_and_award_badges(current_uid)
    for b in new_badges:
        st.toast(f"🏅 Badge unlocked: {b}!", icon="🎉")

    with st.sidebar:
        st.write(f"Welcome back, **{st.session_state.user_info['firstname']}**")
        pending_count = get_pending_count(current_uid)
        if pending_count > 0:
            st.warning(f"🔔 **{pending_count}** pending notification(s)")

        st.divider()
        with st.expander("✏️ Edit Profile"):
            new_firstname = st.text_input("First Name", value=st.session_state.user_info.get("firstname", ""), key="profile_firstname")
            new_lastname  = st.text_input("Last Name",  value=st.session_state.user_info.get("lastname",  ""), key="profile_lastname")
            new_email     = st.text_input("Email",      value=st.session_state.user_info.get("email",     ""), key="profile_email")
            new_likes     = st.text_area("Travel Style / Interests", value=st.session_state.user_info.get("likes", ""), key="profile_likes", height=100)
            if st.button("💾 Save Profile", use_container_width=True):
                try:
                    with engine.begin() as conn:
                        conn.execute(text("""
                            UPDATE users SET firstname=:fn, lastname=:ln, email=:em, likes=:lk WHERE id=:uid
                        """), {"fn": new_firstname, "ln": new_lastname, "em": new_email, "lk": new_likes, "uid": current_uid})
                    st.session_state.user_info.update({"firstname": new_firstname, "lastname": new_lastname, "email": new_email, "likes": new_likes})
                    st.success("Profile updated!")
                except Exception as e:
                    st.error(f"Error: {e}")

        st.divider()
        with st.expander("🔒 Change Password"):
            cp_current = st.text_input("Current password", type="password", key="cp_current")
            cp_new1    = st.text_input("New password",     type="password", key="cp_new1")
            cp_new2    = st.text_input("Confirm new",      type="password", key="cp_new2")
            if st.button("Update Password", use_container_width=True):
                with engine.connect() as conn:
                    row = conn.execute(text("SELECT password_hash FROM users WHERE id=:uid"),
                                       {"uid": current_uid}).fetchone()
                if not row or not row.password_hash:
                    st.error("No password set on this account.")
                elif not check_password(cp_current, row.password_hash):
                    st.error("Current password is incorrect.")
                else:
                    err = validate_password(cp_new1)
                    if err:
                        st.error(err)
                    elif cp_new1 != cp_new2:
                        st.error("New passwords don't match.")
                    else:
                        with engine.begin() as conn:
                            conn.execute(text("UPDATE users SET password_hash=:ph WHERE id=:uid"),
                                         {"ph": hash_password(cp_new1), "uid": current_uid})
                        st.success("Password updated!")

        st.divider()
        if st.button("Log Out", use_container_width=True):
            st.session_state.logged_in = False
            st.session_state.user_info = None
            st.session_state.auth_screen = "login"
            st.rerun()

    plan_tab, explorer_tab, friend_tab, my_trips_tab, stats_tab, notif_tab = st.tabs([
        "🗺️ Plan Trip", "🔭 Park Explorer", "👥 Friends", "🎒 My Trips", "📊 My Stats", "🔔 Notifications"
    ])

    # ─────────────────────────────────────────────
    # NOTIFICATIONS TAB
    # ─────────────────────────────────────────────
    with notif_tab:
        st.header("🔔 Notifications")

        with engine.connect() as conn:
            friend_reqs = conn.execute(text("""
                SELECT f.id, u.firstname, u.lastname, u.username, f.status
                FROM friendships f JOIN users u ON f.user_id=u.id
                WHERE f.friend_id=:uid AND f.status='pending' ORDER BY f.id DESC
            """), {"uid": current_uid}).fetchall()
            trip_invites = get_pending_trip_invites(conn, current_uid)
            recent_notes = conn.execute(text("""
                SELECT tdn.id, tdn.trip_id, tdn.day_number, tdn.note_text, tdn.created_at,
                       u.firstname, u.lastname, t.trip_name
                FROM trip_day_notes tdn
                JOIN users u ON tdn.author_id=u.id
                JOIN trips t ON tdn.trip_id=t.id
                JOIN trip_participants tp ON t.id=tp.trip_id
                WHERE tp.user_id=:uid AND tdn.author_id!=:uid
                  AND tdn.created_at > NOW() - INTERVAL '7 days'
                ORDER BY tdn.created_at DESC LIMIT 20
            """), {"uid": current_uid}).fetchall()

        if friend_reqs:
            st.subheader("👤 Friend Requests")
            for req in friend_reqs:
                with st.container(border=True):
                    c1, c2, c3 = st.columns([4, 1, 1])
                    c1.write(f"**{req.firstname} {req.lastname}** (@{req.username}) wants to connect")
                    if c2.button("✅ Accept", key=f"notif_acc_friend_{req.id}"):
                        with engine.begin() as conn:
                            conn.execute(text("UPDATE friendships SET status='accepted' WHERE id=:rid"), {"rid": req.id})
                        st.rerun()
                    if c3.button("❌ Decline", key=f"notif_dec_friend_{req.id}"):
                        with engine.begin() as conn:
                            conn.execute(text("DELETE FROM friendships WHERE id=:rid"), {"rid": req.id})
                        st.rerun()
            st.divider()

        if trip_invites:
            st.subheader("🎒 Trip Invites")
            for inv in trip_invites:
                with st.container(border=True):
                    st.write(f"**{inv.trip_name}**")
                    st.caption(f"📍 {inv.park_names or 'Multiple Parks'}  •  📅 {inv.start_date} → {inv.end_date}  •  From **{inv.invited_by_name}**")
                    col1, col2 = st.columns(2)
                    if col1.button("Accept 🎒", key=f"notif_acc_trip_{inv.participant_id}"):
                        with engine.begin() as conn:
                            conn.execute(text("UPDATE trip_participants SET invitation_status='accepted', responded_at=CURRENT_TIMESTAMP WHERE id=:pid"), {"pid": inv.participant_id})
                        st.success(f"You're going to **{inv.trip_name}**! 🎉")
                        st.rerun()
                    if col2.button("Decline ❌", key=f"notif_dec_trip_{inv.participant_id}"):
                        with engine.begin() as conn:
                            conn.execute(text("UPDATE trip_participants SET invitation_status='declined', responded_at=CURRENT_TIMESTAMP WHERE id=:pid"), {"pid": inv.participant_id})
                        st.rerun()
            st.divider()

        if recent_notes:
            st.subheader("📝 Recent Journal Activity on Your Trips")
            for note in recent_notes:
                with st.container(border=True):
                    st.caption(f"**{note.firstname} {note.lastname}** wrote on **{note.trip_name}** — Day {note.day_number}")
                    st.markdown(f"> {note.note_text}")
                    ts = note.created_at.strftime('%b %d, %Y') if hasattr(note.created_at, 'strftime') else str(note.created_at)
                    st.caption(ts)
        elif not friend_reqs and not trip_invites:
            st.info("You're all caught up! No pending notifications.")

    # ─────────────────────────────────────────────
    # PARK EXPLORER TAB
    # ─────────────────────────────────────────────
    with explorer_tab:
        st.header("🔭 Park Explorer")
        st.caption("Browse all national parks, discover details, and save parks to your wishlist.")

        with engine.connect() as conn:
            all_parks_df = pd.read_sql(text("""
                SELECT p.id, p.name, p.state, p.image_url,
                       pd.description, pd.entrance_fee_cost, pd.visitor_center_hours,
                       pd.weather_info, pd.activities, pd.latitude, pd.longitude,
                       CASE WHEN pw.id IS NOT NULL THEN TRUE ELSE FALSE END AS wishlisted
                FROM parks p
                LEFT JOIN park_details pd ON p.id=pd.park_id
                LEFT JOIN park_wishlists pw ON p.id=pw.park_id AND pw.user_id=:uid
                ORDER BY p.name
            """), conn, params={"uid": current_uid})
            active_alerts_df = pd.read_sql(text("""
                SELECT park_id, COUNT(*) AS alert_count FROM alerts WHERE isactive=TRUE GROUP BY park_id
            """), conn)

        alert_map = dict(zip(active_alerts_df['park_id'], active_alerts_df['alert_count'])) if not active_alerts_df.empty else {}

        fc1, fc2, fc3 = st.columns([2, 2, 1])
        search_q = fc1.text_input("🔍 Search parks", placeholder="e.g. Yellowstone, CA...")
        states = sorted(all_parks_df['state'].dropna().unique().tolist())
        state_filter = fc2.selectbox("Filter by State", ["All States"] + states)
        wishlist_only = fc3.checkbox("❤️ Wishlist only")

        filtered = all_parks_df.copy()
        if search_q:
            filtered = filtered[filtered['name'].str.contains(search_q, case=False, na=False) |
                                 filtered['state'].str.contains(search_q, case=False, na=False)]
        if state_filter != "All States":
            filtered = filtered[filtered['state'] == state_filter]
        if wishlist_only:
            filtered = filtered[filtered['wishlisted'] == True]

        st.caption(f"Showing {len(filtered)} parks")

        for i in range(0, len(filtered), 3):
            row_parks = filtered.iloc[i:i+3]
            cols = st.columns(3)
            for col, (_, park) in zip(cols, row_parks.iterrows()):
                with col:
                    with st.container(border=True):
                        if park.get('image_url'):
                            st.image(park['image_url'], use_container_width=True)
                        alert_badge = f" 🚨 {alert_map[park['id']]} alert(s)" if park['id'] in alert_map else ""
                        wishlist_icon = "❤️" if park['wishlisted'] else "🤍"
                        st.markdown(f"**{park['name']}**{alert_badge}")
                        st.caption(f"📍 {park['state']}")
                        if park.get('entrance_fee_cost'):
                            st.caption(f"💵 Entrance: ${park['entrance_fee_cost']}")
                        with st.expander("ℹ️ Details"):
                            if park.get('description'):
                                st.write(park['description'][:400] + ("..." if len(str(park.get('description', ''))) > 400 else ""))
                            if park.get('weather_info'):
                                st.caption(f"🌤️ {park['weather_info'][:200]}")
                            if park.get('visitor_center_hours'):
                                st.caption(f"🕐 {park['visitor_center_hours'][:200]}")
                            if park.get('activities'):
                                try:
                                    acts = json.loads(park['activities'])
                                    st.caption("Activities: " + ", ".join(acts[:8]))
                                except Exception:
                                    pass
                            if park['id'] in alert_map:
                                with engine.connect() as conn:
                                    park_alerts = conn.execute(text("""
                                        SELECT title, category, description FROM alerts
                                        WHERE park_id=:pid AND isactive=TRUE LIMIT 3
                                    """), {"pid": park['id']}).fetchall()
                                st.markdown("**🚨 Active Alerts:**")
                                for a in park_alerts:
                                    st.warning(f"**{a.category}:** {a.title}")
                        if st.button(f"{wishlist_icon} {'Remove from' if park['wishlisted'] else 'Add to'} Wishlist",
                                     key=f"wl_{park['id']}", use_container_width=True):
                            if park['wishlisted']:
                                with engine.begin() as conn:
                                    conn.execute(text("DELETE FROM park_wishlists WHERE user_id=:uid AND park_id=:pid"),
                                                 {"uid": current_uid, "pid": park['id']})
                            else:
                                with engine.begin() as conn:
                                    conn.execute(text("INSERT INTO park_wishlists (user_id, park_id) VALUES (:uid,:pid) ON CONFLICT DO NOTHING"),
                                                 {"uid": current_uid, "pid": park['id']})
                            st.rerun()

    # ─────────────────────────────────────────────
    # MY STATS TAB
    # ─────────────────────────────────────────────
    with stats_tab:
        st.header("📊 My Adventure Stats")

        with engine.connect() as conn:
            trip_count = conn.execute(text("""
                SELECT COUNT(*) FROM trips t JOIN trip_participants tp ON t.id=tp.trip_id
                WHERE tp.user_id=:uid AND tp.invitation_status='accepted' AND tp.role='owner'
            """), {"uid": current_uid}).scalar() or 0
            parks_visited = conn.execute(text("""
                SELECT DISTINCT p.id, p.name, p.state FROM parks p
                JOIN trip_parks tpk ON p.id=tpk.park_id JOIN trips t ON tpk.trip_id=t.id
                JOIN trip_participants tp ON t.id=tp.trip_id
                WHERE tp.user_id=:uid AND tp.invitation_status='accepted'
            """), {"uid": current_uid}).fetchall()
            states_visited = list({p.state for p in parks_visited if p.state})
            activity_count = conn.execute(text("""
                SELECT COUNT(*) FROM trip_activities ta JOIN trips t ON ta.trip_id=t.id
                JOIN trip_participants tp ON t.id=tp.trip_id
                WHERE tp.user_id=:uid AND tp.invitation_status='accepted'
            """), {"uid": current_uid}).scalar() or 0
            note_count = conn.execute(text("SELECT COUNT(*) FROM trip_day_notes WHERE author_id=:uid"), {"uid": current_uid}).scalar() or 0
            friend_count = conn.execute(text("""
                SELECT COUNT(*) FROM friendships WHERE (user_id=:uid OR friend_id=:uid) AND status='accepted'
            """), {"uid": current_uid}).scalar() or 0
            wishlist_count = conn.execute(text("SELECT COUNT(*) FROM park_wishlists WHERE user_id=:uid"), {"uid": current_uid}).scalar() or 0
            earned_badges = conn.execute(text("""
                SELECT badge_key, badge_name, badge_description, earned_at
                FROM user_badges WHERE user_id=:uid ORDER BY earned_at DESC
            """), {"uid": current_uid}).fetchall()

        m1, m2, m3, m4, m5, m6 = st.columns(6)
        m1.metric("🏕️ Trips", trip_count)
        m2.metric("🌲 Parks", len(parks_visited))
        m3.metric("🗺️ States", len(states_visited))
        m4.metric("⚡ Activities", activity_count)
        m5.metric("📝 Notes", note_count)
        m6.metric("🤝 Friends", friend_count)

        st.divider()
        left_col, right_col = st.columns(2)

        with left_col:
            st.subheader("🌲 Parks Visited")
            if parks_visited:
                for p in sorted(parks_visited, key=lambda x: x.name):
                    st.caption(f"✅ {p.name} — {p.state}")
            else:
                st.info("No parks visited yet. Plan your first trip!")
            st.subheader("🗺️ States Explored")
            if states_visited:
                st.write(", ".join(sorted(states_visited)))
            else:
                st.info("No states yet!")
            st.subheader("❤️ Wishlist")
            st.caption(f"{wishlist_count} park(s) on your wishlist — check the Park Explorer tab!")

        with right_col:
            st.subheader("🏅 Badges")
            earned_keys = {b.badge_key for b in earned_badges}
            if earned_badges:
                st.markdown("**Earned:**")
                badge_cols = st.columns(2)
                for bi, badge in enumerate(earned_badges):
                    with badge_cols[bi % 2]:
                        with st.container(border=True):
                            st.markdown(f"**{badge.badge_name}**")
                            st.caption(badge.badge_description)
                            ts = badge.earned_at.strftime('%b %d, %Y') if hasattr(badge.earned_at, 'strftime') else str(badge.earned_at)
                            st.caption(f"Earned {ts}")
            locked = [b for b in BADGE_DEFINITIONS if b[0] not in earned_keys]
            if locked:
                st.markdown("**Locked 🔒:**")
                for b in locked:
                    st.caption(f"🔒 {b[1]} — {b[2]}")

    # ─────────────────────────────────────────────
    # FRIENDS TAB
    # ─────────────────────────────────────────────
    with friend_tab:
        st.header("Social Hub")

        f_search = st.text_input("Search by username, first name, or last name").strip()
        if f_search:
            with engine.connect() as conn:
                search_results = conn.execute(text("""
                    SELECT u.id, u.username, u.firstname, u.lastname, u.likes,
                        CASE
                            WHEN f.id IS NOT NULL AND f.status='accepted' THEN 'friends'
                            WHEN f.id IS NOT NULL AND f.status='pending' AND f.user_id=:uid THEN 'request_sent'
                            WHEN f.id IS NOT NULL AND f.status='pending' AND f.friend_id=:uid THEN 'request_received'
                            ELSE 'none'
                        END AS friendship_status
                    FROM users u
                    LEFT JOIN friendships f ON (f.user_id=:uid AND f.friend_id=u.id) OR (f.friend_id=:uid AND f.user_id=u.id)
                    WHERE u.id!=:uid
                      AND (LOWER(u.username) LIKE LOWER(:q) OR LOWER(u.firstname) LIKE LOWER(:q) OR LOWER(u.lastname) LIKE LOWER(:q))
                    ORDER BY u.firstname, u.lastname LIMIT 20
                """), {"uid": current_uid, "q": f"%{f_search}%"}).fetchall()

            if not search_results:
                st.info("No users found matching that search.")
            else:
                for res in search_results:
                    with st.container(border=True):
                        rc1, rc2 = st.columns([4, 1])
                        rc1.markdown(f"**{res.firstname} {res.lastname}** (@{res.username})")
                        if res.likes:
                            rc1.caption(f"Style: {res.likes}")
                        if res.friendship_status == "friends":
                            rc2.success("✅ Friends")
                        elif res.friendship_status == "request_sent":
                            rc2.info("⏳ Sent")
                        elif res.friendship_status == "request_received":
                            rc2.warning("📬 Accept?")
                            if rc2.button("Accept", key=f"search_accept_{res.id}"):
                                with engine.begin() as conn:
                                    conn.execute(text("UPDATE friendships SET status='accepted' WHERE user_id=:them AND friend_id=:me"), {"them": res.id, "me": current_uid})
                                st.rerun()
                        else:
                            if rc2.button("➕ Add", key=f"search_add_{res.id}"):
                                try:
                                    with engine.begin() as conn:
                                        conn.execute(text("INSERT INTO friendships (user_id, friend_id, status) VALUES (:u,:f,'pending')"), {"u": current_uid, "f": res.id})
                                    st.toast(f"Friend request sent to {res.firstname}!")
                                    st.rerun()
                                except Exception:
                                    st.warning("Request already exists.")

        st.divider()
        st.subheader("Your Adventure Crew")
        with engine.connect() as conn:
            my_friends = conn.execute(text("""
                SELECT u.id, u.username, u.firstname, u.likes, f.id AS friendship_id
                FROM users u JOIN friendships f ON (u.id=f.friend_id OR u.id=f.user_id)
                WHERE (f.user_id=:uid OR f.friend_id=:uid) AND f.status='accepted' AND u.id!=:uid
            """), {"uid": current_uid}).fetchall()

        if not my_friends:
            st.info("No friends yet. Use the search above to grow your crew!")
        else:
            for f in my_friends:
                with st.container(border=True):
                    fc1, fc2 = st.columns([4, 1])
                    fc1.write(f"**{f.firstname}** (@{f.username})")
                    fc1.caption(f"Style: {f.likes}")
                    confirm_key = f"confirm_del_friend_{f.friendship_id}"
                    if confirm_key not in st.session_state:
                        st.session_state[confirm_key] = False
                    if not st.session_state[confirm_key]:
                        if fc2.button("🗑️", key=f"del_friend_btn_{f.friendship_id}", help="Remove friend"):
                            st.session_state[confirm_key] = True
                            st.rerun()
                    else:
                        fc2.warning(f"Remove **{f.firstname}**?")
                        cf1, cf2 = fc2.columns(2)
                        if cf1.button("Yes", key=f"confirm_yes_friend_{f.friendship_id}"):
                            with engine.begin() as conn:
                                conn.execute(text("DELETE FROM friendships WHERE id=:fid"), {"fid": f.friendship_id})
                            st.session_state[confirm_key] = False
                            st.success("Friend removed.")
                            st.rerun()
                        if cf2.button("No", key=f"confirm_no_friend_{f.friendship_id}"):
                            st.session_state[confirm_key] = False
                            st.rerun()

        st.divider()
        st.subheader("Incoming Friend Requests")
        with engine.connect() as conn:
            pending = conn.execute(text("""
                SELECT f.id, u.username, u.firstname, u.lastname FROM friendships f
                JOIN users u ON f.user_id=u.id WHERE f.friend_id=:uid AND f.status='pending'
            """), {"uid": current_uid}).fetchall()

        if not pending:
            st.info("No pending friend requests.")
        for req in pending:
            c1, c2 = st.columns([0.7, 0.3])
            c1.write(f"Request from **{req.firstname} {req.lastname}** (@{req.username})")
            if c2.button("Accept ✅", key=f"acc_friend_{req.id}"):
                with engine.begin() as conn:
                    conn.execute(text("UPDATE friendships SET status='accepted' WHERE id=:rid"), {"rid": req.id})
                st.rerun()

    # ─────────────────────────────────────────────
    # PLAN TRIP TAB
    # ─────────────────────────────────────────────
    with plan_tab:
        with engine.connect() as conn:
            friends_res = conn.execute(text("""
                SELECT u.id, u.username FROM users u
                JOIN friendships f ON (u.id=f.friend_id OR u.id=f.user_id)
                WHERE ((f.user_id=:uid OR f.friend_id=:uid) AND f.status='accepted') AND u.id!=:uid
            """), {"uid": current_uid}).fetchall()
            friend_options = {fr[1]: fr[0] for fr in friends_res}
            df_parks = pd.read_sql(text("SELECT name, id FROM parks ORDER BY name"), conn)
            templates = conn.execute(text("""
                SELECT t.id, t.trip_name, t.start_date, t.end_date,
                       STRING_AGG(p.name, ', ' ORDER BY p.name) AS park_names
                FROM trips t JOIN trip_participants tp ON t.id=tp.trip_id
                LEFT JOIN trip_parks tpk ON t.id=tpk.trip_id LEFT JOIN parks p ON tpk.park_id=p.id
                WHERE tp.user_id=:uid AND t.is_template=TRUE AND tp.role='owner'
                GROUP BY t.id, t.trip_name, t.start_date, t.end_date ORDER BY t.trip_name
            """), {"uid": current_uid}).fetchall()

        if templates:
            st.markdown("**📋 Start from a Template**")
            tmpl_options = {f"{t.trip_name} ({t.park_names})": t for t in templates}
            chosen_tmpl = st.selectbox("Choose template", ["— New trip —"] + list(tmpl_options.keys()), key="template_select")
            if chosen_tmpl != "— New trip —" and st.button("📋 Load Template"):
                tmpl = tmpl_options[chosen_tmpl]
                tmpl_parks = [p.strip() for p in (tmpl.park_names or "").split(",") if p.strip()]
                st.session_state["selected_parks"] = tmpl_parks
                st.toast(f"Loaded template: {tmpl.trip_name}")
                st.rerun()
            st.divider()

        st.markdown("**Select Parks** _(choose one or more)_")
        selected_parks = st.multiselect(
            "Parks", options=df_parks['name'].tolist(),
            placeholder="Search and add parks...",
            label_visibility="collapsed",
            key="selected_parks"
        )

        if not selected_parks:
            st.info("Select at least one park to get started.")

        date_range = st.date_input("Dates", value=(date.today(), date.today()))
        if len(date_range) == 2 and date_range[1] < date_range[0]:
            st.error("End date must be on or after start date.")
            date_range = (date_range[0], date_range[0])

        st.markdown("**Invite Friends**")
        invite_roles = {}
        if friend_options:
            for fname in friend_options:
                col1, col2 = st.columns([0.6, 0.4])
                if col1.checkbox(fname, key=f"invite_check_{fname}"):
                    invite_roles[fname] = col2.selectbox("Role", ["collaborator", "viewer"], key=f"invite_role_{fname}")
        else:
            st.caption("Add friends to invite them on trips.")

        if st.button("🔍 Generate Plan", disabled=not selected_parks):
            if len(date_range) < 2:
                st.error("Please select a date range.")
            elif not selected_parks:
                st.error("Please select at least one park.")
            else:
                nights = (date_range[1] - date_range[0]).days
                st.session_state.nights = nights
                st.session_state.trip_start = date_range[0]
                st.session_state.trip_end = date_range[1]
                st.session_state.day_activities = {i + 1: [] for i in range(nights + 1)}
                st.session_state.activity_day_defaults = {}
                st.session_state.park_distances = []
                st.session_state.conflict_warnings = {}
                # FIX: save a stable copy of parks at generate time
                st.session_state.active_parks_saved = list(selected_parks)

                parks_label = ", ".join(selected_parks)
                travel_styles = [f"{st.session_state.user_info['firstname']}: {st.session_state.user_info['likes']}"]
                if invite_roles:
                    with engine.connect() as conn:
                        for fname in invite_roles.keys():
                            friend_likes = conn.execute(text("SELECT firstname, likes FROM users WHERE username=:u"), {"u": fname}).fetchone()
                            if friend_likes and friend_likes.likes:
                                travel_styles.append(f"{friend_likes.firstname}: {friend_likes.likes}")

                group_note = (f"This is a group trip. Balance activities for everyone's styles:\n" +
                              "\n".join(f"  - {s}" for s in travel_styles)) if len(travel_styles) > 1 else f"Travel Style: {st.session_state.user_info['likes']}"
                parks_context = f"the park: {selected_parks[0]}" if len(selected_parks) == 1 else f"these parks: {parks_label}"
                itinerary_context = (f"a {nights}-night trip at {selected_parks[0]}" if len(selected_parks) == 1
                                     else f"a {nights}-night multi-park trip visiting {parks_label}. Distribute days across parks logically.")

                prompt = f"""
Suggest 12 individual activities spread across {parks_context}.
{group_note}
Format each as: Name | Type | Park | Brief description
Only return the list, one activity per line.

---MASTER_ITINERARY---
Provide a full day-by-day itinerary for {itinerary_context}.
{group_note}
Label each day clearly as "Day 1", "Day 2", etc. and list the specific activities under each day.
"""
                with st.spinner("Scouting the trail..."):
                    resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt).text
                    parts = resp.split('---MASTER_ITINERARY---')
                    st.session_state.temp_activities = [l for l in parts[0].strip().split('\n') if "|" in l]
                    st.session_state.master_itinerary = parts[1].strip() if len(parts) > 1 else ""

                    num_days = nights + 1
                    day_map = parse_activity_day_defaults(st.session_state.master_itinerary, num_days)
                    defaults = {}
                    for i, act in enumerate(st.session_state.temp_activities):
                        act_name = act.split('|')[0].strip()
                        suggested = guess_day_for_activity(act_name, day_map, default_day=1)
                        defaults[i] = suggested
                        defaults[str(i)] = suggested
                        widget_key = f"target_day_{i}"
                        if widget_key in st.session_state:
                            del st.session_state[widget_key]
                    st.session_state.activity_day_defaults = defaults

                if len(selected_parks) > 1:
                    with st.spinner("Calculating park distances..."):
                        st.session_state.park_distances = fetch_park_distances(selected_parks)

        # Park distance banner
        if st.session_state.park_distances:
            st.divider()
            st.subheader("🚗 Park-to-Park Drive Times")
            dist_cols = st.columns(len(st.session_state.park_distances))
            for idx, leg in enumerate(st.session_state.park_distances):
                with dist_cols[idx]:
                    with st.container(border=True):
                        st.markdown(f"**{leg.get('from','?')} → {leg.get('to','?')}**")
                        st.metric("Drive Time", leg.get("drive_time", "—"))
                        st.caption(f"~{leg.get('distance_miles','?')} miles")
                        if leg.get("tip"):
                            st.caption(f"💡 {leg['tip']}")

        # ── ACTIVITY BOARD ──
        # FIX: use stable saved parks; board always renders if we have activities + dates
        active_parks = st.session_state.active_parks_saved or st.session_state.get("selected_parks") or selected_parks
        board_start = st.session_state.trip_start
        board_end = st.session_state.trip_end

        if st.session_state.temp_activities and board_start and board_end:
            st.divider()
            days = date_range_days(board_start, board_end)
            day_options = [d[0] for d in days]
            st.session_state.conflict_warnings = compute_conflict_warnings(st.session_state.day_activities)

            left, right = st.columns([1, 2])

            with left:
                st.subheader("💡 Suggested Activities")
                with engine.connect() as conn:
                    for park_name in active_parks:
                        park_img = conn.execute(text("SELECT image_url FROM parks WHERE name=:n"), {"n": park_name}).scalar()
                        if park_img:
                            st.image(park_img, caption=park_name, use_container_width=True)

                # Build sorted activity list with stable day defaults
                sorted_activities = []
                for i, act in enumerate(st.session_state.temp_activities):
                    parts = act.split('|')
                    name   = parts[0].strip()
                    a_type = parts[1].strip() if len(parts) > 1 else "Activity"
                    a_park = parts[2].strip() if len(parts) > 2 else ""
                    # FIX: robust default lookup — try int key first, then str key, then 1
                    raw = st.session_state.activity_day_defaults.get(i, st.session_state.activity_day_defaults.get(str(i), 1))
                    suggested_day = raw if raw in day_options else (day_options[0] if day_options else 1)
                    sorted_activities.append((i, name, a_type, a_park, suggested_day))
                sorted_activities.sort(key=lambda x: x[4])

                if st.button("✅ Apply All to Trip", use_container_width=True):
                    added = 0
                    for i, name, a_type, a_park, suggested_day in sorted_activities:
                        target = st.session_state.get(f"target_day_{i}", suggested_day)
                        if target not in st.session_state.day_activities:
                            st.session_state.day_activities[target] = []
                        if name not in [a["name"] for a in st.session_state.day_activities[target]]:
                            st.session_state.day_activities[target].append({"id": f"act_{i}_{target}", "name": name, "type": a_type})
                            added += 1
                    st.toast(f"Added {added} activities to your itinerary! 🎒")
                    st.rerun()

                st.caption("Or add individually:")
                current_day_label = None
                for i, name, a_type, a_park, suggested_day in sorted_activities:
                    if suggested_day != current_day_label:
                        current_day_label = suggested_day
                        day_date = next((d[1] for d in days if d[0] == suggested_day), None)
                        day_label = f"Day {suggested_day}" + (f" — {day_date.strftime('%a, %b %d')}" if day_date else "")
                        st.markdown(f"**📅 {day_label}**")
                    # FIX: clamp index so it never crashes
                    default_index = day_options.index(suggested_day) if suggested_day in day_options else 0
                    with st.container(border=True):
                        ac1, ac2, ac3 = st.columns([2, 2, 1])
                        ac1.markdown(f"**{name}**")
                        ac1.caption(f"{a_type}" + (f" · {a_park}" if a_park else ""))
                        target_day = ac2.selectbox(
                            "Day", options=day_options,
                            format_func=lambda d: f"Day {d}",
                            index=default_index,
                            key=f"target_day_{i}"
                        )
                        if ac3.button("➕", key=f"add_{i}"):
                            if target_day not in st.session_state.day_activities:
                                st.session_state.day_activities[target_day] = []
                            if name not in [a["name"] for a in st.session_state.day_activities[target_day]]:
                                st.session_state.day_activities[target_day].append({"id": f"act_{i}_{target_day}", "name": name, "type": a_type})
                                st.toast(f"Added to Day {target_day}!")
                                st.rerun()

            with right:
                st.subheader("📅 Day-by-Day Itinerary")
                st.caption("Drag activities between days • Click ✕ to remove")

                if st.session_state.conflict_warnings:
                    total = sum(len(v) for v in st.session_state.conflict_warnings.values())
                    with st.expander(f"⚠️ {total} scheduling conflict(s) detected — click to review", expanded=False):
                        for day_num, warns in sorted(st.session_state.conflict_warnings.items()):
                            st.markdown(f"**Day {day_num}**")
                            for w in warns:
                                st.markdown(f"&nbsp;&nbsp;{w}")

                render_dnd_itinerary(st.session_state.day_activities, days, editable=True,
                                     conflict_warnings=st.session_state.conflict_warnings)

                st.divider()
                # FIX: master itinerary always rendered here inside the board block where it belongs
                if st.session_state.master_itinerary:
                    st.subheader("📖 AI Master Itinerary")
                    st.markdown(st.session_state.master_itinerary)
                    st.divider()

                bcol1, bcol2 = st.columns(2)

                if bcol1.button("💾 Save Trip", use_container_width=True):
                    if not active_parks:
                        st.error("No parks selected.")
                    else:
                        try:
                            parks_label = ", ".join(active_parks)
                            trip_name = f"{parks_label} Trip" if len(active_parks) == 1 else f"Multi-Park Trip: {parks_label}"
                            with engine.begin() as conn:
                                tid = conn.execute(text("""
                                    INSERT INTO trips (user_id, owner_id, trip_name, start_date, end_date)
                                    VALUES (:u,:u,:n,:s,:e) RETURNING id
                                """), {"u": current_uid, "n": trip_name, "s": board_start, "e": board_end}).scalar()
                                conn.execute(text("INSERT INTO trip_participants (trip_id, user_id, role, invitation_status, invited_by) VALUES (:t,:u,'owner','accepted',:u)"),
                                             {"t": tid, "u": current_uid})
                                for f_name, f_role in invite_roles.items():
                                    fid = friend_options.get(f_name)
                                    if fid:
                                        conn.execute(text("INSERT INTO trip_participants (trip_id, user_id, role, invitation_status, invited_by) VALUES (:t,:u,:role,'pending',:inviter)"),
                                                     {"t": tid, "u": fid, "role": f_role, "inviter": current_uid})
                                for park_name in active_parks:
                                    park_row = df_parks[df_parks['name'] == park_name]
                                    if not park_row.empty:
                                        p_id = int(park_row['id'].iloc[0])
                                        notes_text = f"MASTER ITINERARY:\n{st.session_state.master_itinerary}" if park_name == active_parks[0] else ""
                                        conn.execute(text("INSERT INTO trip_parks (trip_id, park_id, notes) VALUES (:t,:p,:n)"),
                                                     {"t": tid, "p": p_id, "n": notes_text})
                                for day_num, activities in st.session_state.day_activities.items():
                                    for order, act in enumerate(activities):
                                        conn.execute(text("INSERT INTO trip_activities (trip_id, day_number, activity_name, activity_type, sort_order) VALUES (:tid,:day,:name,:atype,:order)"),
                                                     {"tid": tid, "day": day_num, "name": act["name"], "atype": act.get("type", ""), "order": order})

                            all_types = [act.get("type", "") for acts in st.session_state.day_activities.values() for act in acts]
                            num_days_val = (board_end - board_start).days + 1
                            with st.spinner("Packing your bags..."):
                                packing_items = generate_packing_list(active_parks, all_types, num_days_val)
                            if packing_items:
                                with engine.begin() as conn:
                                    for item in packing_items:
                                        conn.execute(text("INSERT INTO trip_packing_items (trip_id, category, item_name) VALUES (:tid,:cat,:item)"),
                                                     {"tid": tid, "cat": item.get("category", "General"), "item": item.get("item", "")})

                            st.success("Adventure locked in! 🎉")
                            st.balloons()
                            for k in ["day_activities", "activity_day_defaults", "conflict_warnings"]:
                                st.session_state[k] = {}
                            for k in ["temp_activities", "park_distances", "active_parks_saved"]:
                                st.session_state[k] = []
                            for k in ["trip_start", "trip_end"]:
                                st.session_state[k] = None
                            st.session_state.master_itinerary = ""
                            if "selected_parks" in st.session_state:
                                del st.session_state["selected_parks"]

                        except Exception as e:
                            st.error(f"Database Error: {e}")

                if bcol2.button("📋 Save as Template", use_container_width=True):
                    if not active_parks:
                        st.error("No parks selected.")
                    else:
                        try:
                            parks_label = ", ".join(active_parks)
                            tmpl_name = f"Template: {parks_label}"
                            with engine.begin() as conn:
                                tmpl_tid = conn.execute(text("""
                                    INSERT INTO trips (user_id, owner_id, trip_name, start_date, end_date, is_template)
                                    VALUES (:u,:u,:n,:s,:e,TRUE) RETURNING id
                                """), {"u": current_uid, "n": tmpl_name, "s": board_start, "e": board_end}).scalar()
                                conn.execute(text("INSERT INTO trip_participants (trip_id, user_id, role, invitation_status, invited_by) VALUES (:t,:u,'owner','accepted',:u)"),
                                             {"t": tmpl_tid, "u": current_uid})
                                for park_name in active_parks:
                                    park_row = df_parks[df_parks['name'] == park_name]
                                    if not park_row.empty:
                                        p_id = int(park_row['id'].iloc[0])
                                        conn.execute(text("INSERT INTO trip_parks (trip_id, park_id, notes) VALUES (:t,:p,'')"),
                                                     {"t": tmpl_tid, "p": p_id})
                                for day_num, activities in st.session_state.day_activities.items():
                                    for order, act in enumerate(activities):
                                        conn.execute(text("INSERT INTO trip_activities (trip_id, day_number, activity_name, activity_type, sort_order) VALUES (:tid,:day,:name,:atype,:order)"),
                                                     {"tid": tmpl_tid, "day": day_num, "name": act["name"], "atype": act.get("type", ""), "order": order})
                            st.success(f"Template saved as '{tmpl_name}'! ✅")
                        except Exception as e:
                            st.error(f"Error saving template: {e}")

    # ─────────────────────────────────────────────
    # MY TRIPS TAB
    # ─────────────────────────────────────────────
    with my_trips_tab:
        st.header("Your Adventures")

        with engine.connect() as conn:
            trips = conn.execute(text("""
                SELECT DISTINCT t.id, t.trip_name, t.start_date, t.end_date,
                       STRING_AGG(DISTINCT p.name, ', ' ORDER BY p.name) AS park_names,
                       STRING_AGG(DISTINCT p.image_url, '|' ORDER BY p.image_url) AS park_images,
                       u_owner.firstname || ' ' || u_owner.lastname AS owner_name,
                       tp.role, t.recap_text
                FROM trips t
                JOIN trip_participants tp ON t.id=tp.trip_id
                JOIN users u_owner ON t.owner_id=u_owner.id
                LEFT JOIN trip_parks tpk ON t.id=tpk.trip_id
                LEFT JOIN parks p ON tpk.park_id=p.id
                WHERE tp.user_id=:uid AND tp.invitation_status='accepted'
                  AND (t.is_template IS NULL OR t.is_template=FALSE)
                GROUP BY t.id, t.trip_name, t.start_date, t.end_date, u_owner.firstname, u_owner.lastname, tp.role, t.recap_text
                ORDER BY t.start_date DESC
            """), {"uid": current_uid}).fetchall()
            all_parks = pd.read_sql(text("SELECT name, id FROM parks ORDER BY name"), conn)

        if not trips:
            st.info("No trips yet! Head to Plan Trip to start your first adventure 🏕️")
        else:
            for t in trips:
                role_badge = "👑 Owner" if t.role == "owner" else "✏️ Collaborator" if t.role == "collaborator" else "👁️ Viewer"
                # FIX: full trip status with Happening Now / Upcoming / Completed
                status_emoji, status_label, _ = trip_status(t.start_date, t.end_date)
                trip_end_d = t.end_date if isinstance(t.end_date, date) else (date.fromisoformat(str(t.end_date)) if t.end_date else None)
                label = f"{status_emoji} {t.trip_name}  —  {role_badge}  ·  {status_label}"
                editable = can_edit(t.role)

                with st.expander(label):
                    if t.park_images:
                        imgs = [img for img in t.park_images.split('|') if img]
                        if imgs:
                            img_cols = st.columns(min(len(imgs), 3))
                            park_name_list = t.park_names.split(', ') if t.park_names else []
                            for idx, img_url in enumerate(imgs[:3]):
                                cap = park_name_list[idx] if idx < len(park_name_list) else ""
                                img_cols[idx].image(img_url, caption=cap, use_container_width=True)

                    edit_key = f"editing_{t.id}"
                    confirm_del_key = f"confirm_del_trip_{t.id}"
                    for k, v in [(edit_key, False), (confirm_del_key, False)]:
                        if k not in st.session_state:
                            st.session_state[k] = v

                    col_info, col_btns = st.columns([3, 1])
                    with col_info:
                        st.caption(f"📅 {t.start_date} → {t.end_date}  •  🏔️ {t.park_names or 'No Parks'}")
                        if t.role != "owner":
                            st.caption(f"Planned by **{t.owner_name}**")
                    with col_btns:
                        if editable:
                            toggle_label = "Cancel" if st.session_state[edit_key] else "✏️ Edit"
                            if st.button(toggle_label, key=f"toggle_edit_{t.id}"):
                                st.session_state[edit_key] = not st.session_state[edit_key]
                                st.rerun()
                        with engine.connect() as conn_pdf:
                            trip_parks_rows = get_trip_parks(conn_pdf, t.id)
                        all_notes_txt = "\n\n".join(f"=== {tp.park_name} ===\n{tp.notes}" for tp in trip_parks_rows if tp.notes)
                        if all_notes_txt:
                            pdf_b = create_pdf(all_notes_txt, t.trip_name, st.session_state.user_info['firstname'])
                            st.download_button("📥 PDF", pdf_b, f"Trip_{t.id}.pdf", key=f"dl_{t.id}")
                        if t.role == "owner":
                            if not st.session_state[confirm_del_key]:
                                if st.button("🗑️ Delete", key=f"del_trip_btn_{t.id}"):
                                    st.session_state[confirm_del_key] = True
                                    st.rerun()
                            else:
                                st.warning("Delete this trip permanently?")
                                dy, dn = st.columns(2)
                                if dy.button("Yes, delete", key=f"confirm_del_yes_{t.id}"):
                                    with engine.begin() as conn:
                                        conn.execute(text("DELETE FROM trips WHERE id=:tid"), {"tid": t.id})
                                    st.session_state[confirm_del_key] = False
                                    st.success("Trip deleted.")
                                    st.rerun()
                                if dn.button("Cancel", key=f"confirm_del_no_{t.id}"):
                                    st.session_state[confirm_del_key] = False
                                    st.rerun()

                    # ── INLINE EDIT FORM ──
                    if editable and st.session_state[edit_key]:
                        st.divider()
                        st.markdown("### ✏️ Edit Trip")
                        new_name = st.text_input("Trip Name", value=t.trip_name, key=f"name_{t.id}")
                        start = t.start_date if isinstance(t.start_date, date) else date.fromisoformat(str(t.start_date)) if t.start_date else date.today()
                        end   = t.end_date   if isinstance(t.end_date,   date) else date.fromisoformat(str(t.end_date))   if t.end_date   else date.today()
                        new_dates = st.date_input("Dates", value=(start, end), key=f"dates_{t.id}")

                        st.markdown("**Parks**")
                        with engine.connect() as conn2:
                            trip_parks_rows = get_trip_parks(conn2, t.id)
                        current_park_names = [tp.park_name for tp in trip_parks_rows]
                        new_park_selection = st.multiselect("Select Parks", options=all_parks['name'].tolist(), default=current_park_names, key=f"parks_edit_{t.id}")

                        st.markdown("**Park Notes / Itinerary**")
                        park_notes_map = {}
                        existing_notes = {tp.park_name: tp.notes for tp in trip_parks_rows}
                        for pname in new_park_selection:
                            park_notes_map[pname] = st.text_area(f"Notes for {pname}", value=existing_notes.get(pname, ""), height=150, key=f"notes_{t.id}_{pname}")

                        st.markdown("**Edit Day Activities**")
                        with engine.connect() as conn2:
                            saved_acts = conn2.execute(text("""
                                SELECT id, day_number, activity_name, activity_type, sort_order
                                FROM trip_activities WHERE trip_id=:tid ORDER BY day_number, sort_order
                            """), {"tid": t.id}).fetchall()
                        edit_days = date_range_days(start, end)
                        edit_day_acts = {d[0]: [] for d in edit_days}
                        for a in saved_acts:
                            if a.day_number in edit_day_acts:
                                edit_day_acts[a.day_number].append({"id": str(a.id), "name": a.activity_name, "type": a.activity_type or ""})
                        render_dnd_itinerary(edit_day_acts, edit_days, editable=True)

                        st.markdown("**Add a new activity:**")
                        na1, na2, na3, na4 = st.columns([3, 2, 2, 1])
                        new_act_name = na1.text_input("Activity name", key=f"new_act_name_{t.id}")
                        new_act_type = na2.text_input("Type", key=f"new_act_type_{t.id}")
                        new_act_day  = na3.selectbox("Day", [d[0] for d in edit_days], key=f"new_act_day_{t.id}")
                        if na4.button("Add", key=f"add_new_act_{t.id}") and new_act_name:
                            with engine.connect() as conn2:
                                max_order = conn2.execute(text("SELECT COALESCE(MAX(sort_order),0) FROM trip_activities WHERE trip_id=:tid AND day_number=:day"),
                                                          {"tid": t.id, "day": new_act_day}).scalar()
                                conn2.execute(text("INSERT INTO trip_activities (trip_id, day_number, activity_name, activity_type, sort_order) VALUES (:tid,:day,:name,:atype,:order)"),
                                              {"tid": t.id, "day": new_act_day, "name": new_act_name, "atype": new_act_type, "order": max_order+1})
                                conn2.commit()
                            st.rerun()

                        if saved_acts:
                            st.markdown("**Move or delete a saved activity:**")
                            for sa in saved_acts:
                                sa1, sa2, sa3, sa4 = st.columns([3, 2, 1, 1])
                                sa1.write(f"Day {sa.day_number} — {sa.activity_name}")
                                move_day = sa2.selectbox("Move to", [d[0] for d in edit_days], key=f"mv_day_{sa.id}", index=sa.day_number - 1)
                                if sa3.button("Move", key=f"mv_btn_{sa.id}"):
                                    with engine.begin() as conn2:
                                        conn2.execute(text("UPDATE trip_activities SET day_number=:day WHERE id=:aid"), {"day": move_day, "aid": sa.id})
                                    st.rerun()
                                if sa4.button("🗑️", key=f"del_act_{sa.id}"):
                                    with engine.begin() as conn2:
                                        conn2.execute(text("DELETE FROM trip_activities WHERE id=:aid"), {"aid": sa.id})
                                    st.rerun()

                        if t.role == "owner":
                            st.markdown("**Manage Participant Permissions**")
                            with engine.connect() as conn2:
                                participants = conn2.execute(text("""
                                    SELECT tp.id, u.username, u.firstname, tp.role, tp.invitation_status
                                    FROM trip_participants tp JOIN users u ON tp.user_id=u.id
                                    WHERE tp.trip_id=:tid AND tp.role!='owner'
                                """), {"tid": t.id}).fetchall()
                            for p in participants:
                                pc1, pc2, pc3 = st.columns([2, 2, 1])
                                pc1.write(f"**{p.firstname}** (@{p.username})")
                                pc1.caption(f"Status: {p.invitation_status}")
                                new_role = pc2.selectbox("Role", ["collaborator", "viewer"], index=0 if p.role=="collaborator" else 1, key=f"role_{t.id}_{p.id}")
                                if pc3.button("Update", key=f"update_role_{t.id}_{p.id}"):
                                    with engine.begin() as conn2:
                                        conn2.execute(text("UPDATE trip_participants SET role=:r WHERE id=:pid"), {"r": new_role, "pid": p.id})
                                    st.rerun()

                        if st.button("💾 Save Changes", key=f"save_{t.id}"):
                            if not new_park_selection:
                                st.error("Please select at least one park.")
                            else:
                                try:
                                    with engine.begin() as conn2:
                                        conn2.execute(text("UPDATE trips SET trip_name=:name, start_date=:s, end_date=:e WHERE id=:tid"),
                                                      {"name": new_name, "s": new_dates[0] if len(new_dates)>1 else start, "e": new_dates[1] if len(new_dates)>1 else end, "tid": t.id})
                                        removed_parks = [tp for tp in trip_parks_rows if tp.park_name not in new_park_selection]
                                        for rp in removed_parks:
                                            conn2.execute(text("DELETE FROM trip_parks WHERE id=:tpkid"), {"tpkid": rp.trip_park_id})
                                        existing_park_names = {tp.park_name: tp.trip_park_id for tp in trip_parks_rows}
                                        for pname in new_park_selection:
                                            prow = all_parks[all_parks['name']==pname]
                                            if prow.empty:
                                                continue
                                            pid = int(prow['id'].iloc[0])
                                            notes = park_notes_map.get(pname, "")
                                            if pname in existing_park_names:
                                                conn2.execute(text("UPDATE trip_parks SET park_id=:p, notes=:n WHERE id=:tpkid"),
                                                              {"p": pid, "n": notes, "tpkid": existing_park_names[pname]})
                                            else:
                                                conn2.execute(text("INSERT INTO trip_parks (trip_id, park_id, notes) VALUES (:t,:p,:n)"),
                                                              {"t": t.id, "p": pid, "n": notes})
                                    st.success("Trip updated! ✅")
                                    st.session_state[edit_key] = False
                                    st.rerun()
                                except Exception as e:
                                    st.error(f"Error saving: {e}")

                    # ── READ-ONLY VIEW ──
                    else:
                        with engine.connect() as conn2:
                            saved_acts = conn2.execute(text("""
                                SELECT day_number, activity_name, activity_type
                                FROM trip_activities WHERE trip_id=:tid ORDER BY day_number, sort_order
                            """), {"tid": t.id}).fetchall()
                        with engine.connect() as conn2:
                            all_notes_rows = conn2.execute(text("""
                                SELECT tdn.id, tdn.day_number, tdn.note_text, tdn.created_at,
                                       u.firstname, u.lastname, tdn.author_id
                                FROM trip_day_notes tdn JOIN users u ON tdn.author_id=u.id
                                WHERE tdn.trip_id=:tid ORDER BY tdn.day_number, tdn.created_at
                            """), {"tid": t.id}).fetchall()
                        notes_by_day = {}
                        for n in all_notes_rows:
                            notes_by_day.setdefault(n.day_number, []).append(n)
                        day_act_map = {}
                        for a in saved_acts:
                            day_act_map.setdefault(a.day_number, []).append(a)

                        # Active NPS alerts
                        with engine.connect() as conn2:
                            trip_park_ids = [tp.park_id for tp in get_trip_parks(conn2, t.id)]
                        if trip_park_ids:
                            with engine.connect() as conn2:
                                trip_alerts = conn2.execute(text("""
                                    SELECT a.title, a.category, a.description, p.name AS park_name
                                    FROM alerts a JOIN parks p ON a.park_id=p.id
                                    WHERE a.park_id=ANY(:pids) AND a.isactive=TRUE
                                    ORDER BY p.name, a.category
                                """), {"pids": trip_park_ids}).fetchall()
                            if trip_alerts:
                                st.divider()
                                with st.expander(f"🚨 {len(trip_alerts)} active NPS alert(s) for this trip", expanded=False):
                                    for al in trip_alerts:
                                        st.warning(f"**{al.park_name} — {al.category}:** {al.title}")
                                        if al.description:
                                            st.caption(al.description[:300])

                        # Day-by-day
                        view_days = date_range_days(t.start_date, t.end_date)
                        st.divider()
                        st.markdown("**📅 Day-by-Day:**")
                        for day_num, day_date in view_days:
                            acts = day_act_map.get(day_num, [])
                            day_notes = notes_by_day.get(day_num, [])
                            with st.container(border=True):
                                st.markdown(f"**Day {day_num} — {day_date.strftime('%a, %b %d')}**")
                                if acts:
                                    for a in acts:
                                        st.caption(f"  • {a.activity_name} _{a.activity_type}_")
                                else:
                                    st.caption("  _(no activities planned)_")
                                if day_notes:
                                    st.markdown("📝 **Notes:**")
                                    for note in day_notes:
                                        is_mine = note.author_id == current_uid
                                        note_col1, note_col2 = st.columns([8, 1])
                                        note_col1.markdown(
                                            f"> {note.note_text}  \n"
                                            f"<small>— {note.firstname} {note.lastname}, "
                                            f"{note.created_at.strftime('%b %d') if hasattr(note.created_at,'strftime') else note.created_at}</small>",
                                            unsafe_allow_html=True
                                        )
                                        if is_mine:
                                            if note_col2.button("🗑️", key=f"del_note_{note.id}"):
                                                with engine.begin() as conn2:
                                                    conn2.execute(text("DELETE FROM trip_day_notes WHERE id=:nid"), {"nid": note.id})
                                                st.rerun()
                                new_note = st.text_input(
                                    f"Add a note for Day {day_num}",
                                    placeholder="How did it go? Any tips for future visitors?",
                                    key=f"note_input_{t.id}_{day_num}",
                                    label_visibility="collapsed"
                                )
                                if st.button("💬 Add Note", key=f"add_note_{t.id}_{day_num}"):
                                    if new_note.strip():
                                        with engine.begin() as conn2:
                                            conn2.execute(text("INSERT INTO trip_day_notes (trip_id, day_number, author_id, note_text) VALUES (:tid,:day,:uid,:note)"),
                                                          {"tid": t.id, "day": day_num, "uid": current_uid, "note": new_note.strip()})
                                        st.rerun()

                        # Packing list
                        with engine.connect() as conn2:
                            packing_rows = conn2.execute(text("""
                                SELECT id, category, item_name, is_checked
                                FROM trip_packing_items WHERE trip_id=:tid ORDER BY category, item_name
                            """), {"tid": t.id}).fetchall()

                        st.divider()
                        if packing_rows:
                            with st.expander("🎒 Packing List", expanded=False):
                                checked_count = sum(1 for p in packing_rows if p.is_checked)
                                total_count = len(packing_rows)
                                st.progress(checked_count / total_count if total_count else 0,
                                            text=f"{checked_count}/{total_count} items packed")
                                st.caption("Check items off as you pack!")
                                categories = {}
                                for row in packing_rows:
                                    categories.setdefault(row.category, []).append(row)
                                pack_cols = st.columns(2)
                                for ci, (category, items) in enumerate(sorted(categories.items())):
                                    with pack_cols[ci % 2]:
                                        st.markdown(f"**{category}**")
                                        for item in items:
                                            checked = st.checkbox(item.item_name, value=item.is_checked, key=f"pack_{item.id}")
                                            if checked != item.is_checked:
                                                with engine.begin() as conn2:
                                                    conn2.execute(text("UPDATE trip_packing_items SET is_checked=:c WHERE id=:iid"), {"c": checked, "iid": item.id})
                        else:
                            # FIX: fallback generator for trips saved before packing list feature
                            with st.expander("🎒 Packing List", expanded=False):
                                st.caption("No packing list yet for this trip.")
                                with engine.connect() as conn2:
                                    pl_park_names = [tp.park_name for tp in get_trip_parks(conn2, t.id)]
                                    pl_act_types = [a.activity_type for a in conn2.execute(text(
                                        "SELECT activity_type FROM trip_activities WHERE trip_id=:tid"), {"tid": t.id}).fetchall()]
                                num_days_pl = (trip_end_d - (t.start_date if isinstance(t.start_date, date) else date.fromisoformat(str(t.start_date)))).days + 1 if trip_end_d else 1
                                if st.button("✨ Generate Packing List", key=f"gen_pack_{t.id}"):
                                    with st.spinner("Packing your bags..."):
                                        new_items = generate_packing_list(pl_park_names, pl_act_types, num_days_pl)
                                    if new_items:
                                        with engine.begin() as conn2:
                                            for item in new_items:
                                                conn2.execute(text("INSERT INTO trip_packing_items (trip_id, category, item_name) VALUES (:tid,:cat,:item)"),
                                                              {"tid": t.id, "cat": item.get("category", "General"), "item": item.get("item", "")})
                                        st.rerun()
                                    else:
                                        st.error("Couldn't generate packing list. Try again.")

                        # AI Trip Recap (past trips only)
                        if trip_end_d and trip_end_d < date.today():
                            st.divider()
                            st.markdown("**✍️ Trip Recap**")
                            if t.recap_text:
                                st.markdown(t.recap_text)
                                if editable and st.button("🔄 Regenerate Recap", key=f"regen_recap_{t.id}"):
                                    st.session_state[f"gen_recap_{t.id}"] = True
                                    st.rerun()
                            else:
                                st.caption("This trip is in the past — generate a recap narrative!")
                            gen_key = f"gen_recap_{t.id}"
                            if st.session_state.get(gen_key) or not t.recap_text:
                                if st.button("🤖 Generate AI Recap", key=f"do_recap_{t.id}"):
                                    with st.spinner("Writing your story..."):
                                        recap_acts  = [{"day": a.day_number, "name": a.activity_name} for a in saved_acts]
                                        recap_notes = [{"day": n.day_number, "text": n.note_text} for n in all_notes_rows]
                                        park_names_list = [p.strip() for p in (t.park_names or "").split(",") if p.strip()]
                                        start_d = t.start_date if isinstance(t.start_date, date) else date.fromisoformat(str(t.start_date)) if t.start_date else None
                                        end_d_r = t.end_date   if isinstance(t.end_date,   date) else date.fromisoformat(str(t.end_date))   if t.end_date   else None
                                        recap = generate_trip_recap(t.trip_name, park_names_list, recap_acts, recap_notes, start_d, end_d_r)
                                    if recap:
                                        with engine.begin() as conn2:
                                            conn2.execute(text("UPDATE trips SET recap_text=:r WHERE id=:tid"), {"r": recap, "tid": t.id})
                                        st.session_state[gen_key] = False
                                        st.rerun()
                                    else:
                                        st.error("Couldn't generate recap. Try again.")

                        # Park notes / master itinerary stored in DB
                        with engine.connect() as conn2:
                            trip_parks_rows_view = get_trip_parks(conn2, t.id)
                        if trip_parks_rows_view:
                            st.divider()
                            for tp in trip_parks_rows_view:
                                if tp.notes:
                                    st.markdown(f"**📍 {tp.park_name}**")
                                    st.markdown(tp.notes)

                    # Trip crew — always visible
                    with engine.connect() as conn2:
                        participants = conn2.execute(text("""
                            SELECT u.firstname, u.lastname, u.username, tp.role, tp.invitation_status
                            FROM trip_participants tp JOIN users u ON tp.user_id=u.id
                            WHERE tp.trip_id=:tid ORDER BY tp.role
                        """), {"tid": t.id}).fetchall()
                    if participants:
                        st.divider()
                        st.markdown("**Trip Crew:**")
                        for p in participants:
                            status_icon = "✅" if p.invitation_status=="accepted" else "⏳" if p.invitation_status=="pending" else "❌"
                            role_icon   = "👑" if p.role=="owner" else "✏️" if p.role=="collaborator" else "👁️"
                            st.caption(f"{status_icon} {p.firstname} {p.lastname} (@{p.username}) — {role_icon} {p.role}")
