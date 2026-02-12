import streamlit as st
import os
import json
import pandas as pd
from datetime import datetime, date, timedelta
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

st.set_page_config(page_title="National Park Planner", page_icon="🌲", layout="wide")

# Session state defaults
for key, val in {
    "logged_in": False,
    "user_info": None,
    "temp_activities": [],
    "master_itinerary": "",
    "day_activities": {},   # {day_number: [{"name":..., "type":..., "id":...}]}
    "nights": 0,
}.items():
    if key not in st.session_state:
        st.session_state[key] = val

engine = get_connection()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

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
               p.name AS park_name
        FROM trip_participants tp
        JOIN trips t ON tp.trip_id = t.id
        JOIN users u_owner ON t.owner_id = u_owner.id
        LEFT JOIN trip_parks tpk ON t.id = tpk.trip_id
        LEFT JOIN parks p ON tpk.park_id = p.id
        WHERE tp.user_id = :uid AND tp.invitation_status = 'pending' AND tp.role != 'owner'
    """), {"uid": uid}).fetchall()

def get_pending_count(uid):
    with engine.connect() as conn:
        fc = conn.execute(text("SELECT COUNT(*) FROM friendships WHERE friend_id=:uid AND status='pending'"), {"uid": uid}).scalar()
        tc = conn.execute(text("SELECT COUNT(*) FROM trip_participants WHERE user_id=:uid AND invitation_status='pending' AND role!='owner'"), {"uid": uid}).scalar()
    return (fc or 0) + (tc or 0)

def can_edit(role):
    return role in ('owner', 'collaborator')

def date_range_days(start, end):
    """Returns list of (day_number, date) tuples for a trip."""
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

# ─────────────────────────────────────────────
# DRAG-AND-DROP ITINERARY COMPONENT
# ─────────────────────────────────────────────

def render_dnd_itinerary(day_activities, days, editable=True):
    """
    Renders a drag-and-drop day-by-day itinerary using a custom HTML component.
    Returns the updated day_activities dict after user interaction.
    """
    days_data = []
    for day_num, day_date in days:
        acts = day_activities.get(day_num, [])
        days_data.append({
            "day": day_num,
            "label": f"Day {day_num} — {day_date.strftime('%a, %b %d')}",
            "activities": [{"id": a["id"], "name": a["name"], "type": a.get("type", "Activity")} for a in acts]
        })

    delete_btn = '<button class="delete-btn" onclick="deleteActivity(this)" title="Remove">✕</button>' if editable else ""
    draggable_attr = "draggable='true' ondragstart='handleDragStart(event, this)'" if editable else ""

    html = f"""
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: 'Segoe UI', sans-serif; background: transparent; }}
        .itinerary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 12px;
            padding: 4px;
        }}
        .day-col {{
            background: #f0f4f8;
            border-radius: 10px;
            padding: 10px;
            min-height: 120px;
        }}
        .day-header {{
            font-size: 0.8em;
            font-weight: 700;
            color: #2d6a4f;
            margin-bottom: 8px;
            padding-bottom: 6px;
            border-bottom: 2px solid #2d6a4f;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .activity-card {{
            background: white;
            border-radius: 6px;
            padding: 8px 10px;
            margin-bottom: 6px;
            font-size: 0.82em;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
            {"cursor: grab;" if editable else ""}
            border-left: 3px solid #52b788;
            transition: box-shadow 0.15s;
        }}
        .activity-card.dragging {{
            opacity: 0.5;
            cursor: grabbing;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        }}
        .activity-card:hover {{
            box-shadow: 0 3px 8px rgba(0,0,0,0.15);
        }}
        .act-name {{ font-weight: 600; color: #1b4332; }}
        .act-type {{ color: #888; font-size: 0.85em; margin-top: 2px; }}
        .delete-btn {{
            background: none;
            border: none;
            color: #ccc;
            cursor: pointer;
            font-size: 1em;
            padding: 0 2px;
            line-height: 1;
        }}
        .delete-btn:hover {{ color: #e74c3c; }}
        .drop-zone {{
            min-height: 40px;
            border-radius: 6px;
            border: 2px dashed transparent;
            transition: border-color 0.2s, background 0.2s;
            margin-top: 4px;
        }}
        .drop-zone.drag-over {{
            border-color: #52b788;
            background: #d8f3dc;
        }}
        #result {{ display: none; }}
    </style>

    <div class="itinerary-grid" id="itinerary">
        {"".join(f'''
        <div class="day-col" id="day-{d["day"]}" 
             ondragover="event.preventDefault(); this.querySelector('.drop-zone').classList.add('drag-over')"
             ondragleave="this.querySelector('.drop-zone').classList.remove('drag-over')"
             ondrop="handleDrop(event, {d['day']})">
            <div class="day-header">{d["label"]}</div>
            {"".join(f'''
            <div class="activity-card" 
                 {draggable_attr}
                 data-id="{a["id"]}" data-day="{d["day"]}">
                <div>
                    <div class="act-name">{a["name"]}</div>
                    <div class="act-type">{a["type"]}</div>
                </div>
                {delete_btn}
            </div>
            ''' for a in d["activities"])}
            <div class="drop-zone"></div>
        </div>
        ''' for d in days_data)}
    </div>
    <textarea id="result"></textarea>

    <script>
        let dragSrc = null;

        function handleDragStart(e, el) {{
            dragSrc = el;
            el.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        }}

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

        function deleteActivity(btn) {{
            btn.closest('.activity-card').remove();
            saveState();
        }}

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
# AUTH
# ─────────────────────────────────────────────

if not st.session_state.logged_in:
    st.title("🌲 National Park Planner 🐻")
    t1, t2 = st.tabs(["Login", "Create Account"])
    with t1:
        u = st.text_input("Username").strip().lower()
        if st.button("Login"):
            with engine.connect() as conn:
                res = conn.execute(
                    text("SELECT id, username, firstname, lastname, email, likes FROM users WHERE username = :u"),
                    {"u": u}
                ).mappings().fetchone()
                if res:
                    st.session_state.user_info = dict(res)
                    st.session_state.logged_in = True
                    st.rerun()
                else:
                    st.error("User not found.")
    with t2:
        nu = st.text_input("New Username").strip().lower()
        fn, ln, em = st.text_input("First Name"), st.text_input("Last Name"), st.text_input("Email")
        lk = st.text_area("Travel Style")
        if st.button("Sign Up"):
            try:
                with engine.connect() as conn:
                    conn.execute(
                        text("INSERT INTO users (username, firstname, lastname, email, likes) VALUES (:u,:f,:ln,:e,:l)"),
                        {"u": nu, "f": fn, "ln": ln, "e": em, "l": lk}
                    )
                    conn.commit()
                    st.success("Account created!")
            except Exception as e:
                st.error(f"Error: {e}")

# ─────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────

else:
    current_uid = st.session_state.user_info['id']

    with st.sidebar:
        st.write(f"Welcome back, **{st.session_state.user_info['firstname']}**")
        pending_count = get_pending_count(current_uid)
        if pending_count > 0:
            st.warning(f"🔔 **{pending_count}** pending notification(s)")
        if st.button("Log Out"):
            st.session_state.logged_in = False
            st.session_state.user_info = None
            st.rerun()

    plan_tab, friend_tab, my_trips_tab = st.tabs(["🗺️ Plan Trip", "👥 Friends", "🎒 My Trips"])

    # ─────────────────────────────────────────────
    # FRIENDS TAB
    # ─────────────────────────────────────────────
    with friend_tab:
        st.header("Social Hub")

        f_search = st.text_input("Search User by Username").strip().lower()
        if st.button("Send Friend Request"):
            with engine.connect() as conn:
                f_res = conn.execute(text("SELECT id FROM users WHERE username = :u"), {"u": f_search}).fetchone()
                if f_res:
                    if f_res[0] == current_uid:
                        st.warning("You can't friend yourself!")
                    else:
                        try:
                            conn.execute(text("INSERT INTO friendships (user_id, friend_id, status) VALUES (:u, :f, 'pending')"),
                                         {"u": current_uid, "f": f_res[0]})
                            conn.commit()
                            st.success("Friend request sent!")
                        except Exception:
                            st.warning("Request already exists or is pending.")
                else:
                    st.error("User not found.")

        st.divider()
        st.subheader("Your Adventure Crew")
        with engine.connect() as conn:
            my_friends = conn.execute(text("""
                SELECT u.id, u.username, u.firstname, u.likes,
                       f.id AS friendship_id
                FROM users u
                JOIN friendships f ON (u.id = f.friend_id OR u.id = f.user_id)
                WHERE (f.user_id = :uid OR f.friend_id = :uid)
                  AND f.status = 'accepted' AND u.id != :uid
            """), {"uid": current_uid}).fetchall()

        if not my_friends:
            st.info("No friends yet. Use the search above to grow your crew!")
        else:
            for f in my_friends:
                with st.container(border=True):
                    fc1, fc2 = st.columns([4, 1])
                    fc1.write(f"**{f.firstname}** (@{f.username})")
                    fc1.caption(f"Style: {f.likes}")

                    # ── DELETE FRIEND with confirmation ──
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
                            with engine.connect() as conn2:
                                conn2.execute(text("DELETE FROM friendships WHERE id = :fid"), {"fid": f.friendship_id})
                                conn2.commit()
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
                SELECT f.id, u.username FROM friendships f
                JOIN users u ON f.user_id = u.id
                WHERE f.friend_id = :uid AND f.status = 'pending'
            """), {"uid": current_uid}).fetchall()

        if not pending:
            st.info("No pending friend requests.")
        for req in pending:
            c1, c2 = st.columns([0.7, 0.3])
            c1.write(f"Request from **{req[1]}**")
            if c2.button("Accept ✅", key=f"acc_friend_{req[0]}"):
                with engine.connect() as conn2:
                    conn2.execute(text("UPDATE friendships SET status='accepted' WHERE id=:rid"), {"rid": req[0]})
                    conn2.commit()
                st.rerun()

        st.divider()
        st.subheader("Incoming Trip Invites")
        with engine.connect() as conn:
            trip_invites = get_pending_trip_invites(conn, current_uid)

        if not trip_invites:
            st.info("No pending trip invites.")
        for inv in trip_invites:
            with st.container(border=True):
                st.write(f"**{inv.trip_name}**")
                st.caption(f"📍 {inv.park_name or 'Multiple Parks'}  •  📅 {inv.start_date} → {inv.end_date}  •  Invited by **{inv.invited_by_name}**")
                col1, col2 = st.columns(2)
                if col1.button("Accept 🎒", key=f"accept_trip_{inv.participant_id}"):
                    with engine.connect() as conn2:
                        conn2.execute(text("UPDATE trip_participants SET invitation_status='accepted', responded_at=CURRENT_TIMESTAMP WHERE id=:pid"), {"pid": inv.participant_id})
                        conn2.commit()
                    st.success(f"You're going to **{inv.trip_name}**! 🎉")
                    st.rerun()
                if col2.button("Decline ❌", key=f"decline_trip_{inv.participant_id}"):
                    with engine.connect() as conn2:
                        conn2.execute(text("UPDATE trip_participants SET invitation_status='declined', responded_at=CURRENT_TIMESTAMP WHERE id=:pid"), {"pid": inv.participant_id})
                        conn2.commit()
                    st.rerun()

    # ─────────────────────────────────────────────
    # PLAN TRIP TAB
    # ─────────────────────────────────────────────
    with plan_tab:
        with engine.connect() as conn:
            friends_res = conn.execute(text("""
                SELECT u.id, u.username FROM users u
                JOIN friendships f ON (u.id = f.friend_id OR u.id = f.user_id)
                WHERE ((f.user_id = :uid OR f.friend_id = :uid) AND f.status = 'accepted') AND u.id != :uid
            """), {"uid": current_uid}).fetchall()
            friend_options = {fr[1]: fr[0] for fr in friends_res}
            df_parks = pd.read_sql(text("SELECT name, id FROM parks ORDER BY name"), conn)

        p_sel = st.selectbox("Select Park", options=df_parks['name'])
        date_range = st.date_input("Dates", value=(date.today(), date.today()))

        st.markdown("**Invite Friends**")
        invite_roles = {}
        if friend_options:
            for fname in friend_options:
                col1, col2 = st.columns([0.6, 0.4])
                if col1.checkbox(fname, key=f"invite_check_{fname}"):
                    invite_roles[fname] = col2.selectbox("Role", ["collaborator", "viewer"], key=f"invite_role_{fname}")
        else:
            st.caption("Add friends to invite them on trips.")

        if st.button("🔍 Generate Plan"):
            if len(date_range) < 2:
                st.error("Please select a date range.")
            else:
                nights = (date_range[1] - date_range[0]).days
                st.session_state.nights = nights
                st.session_state.day_activities = {i + 1: [] for i in range(nights + 1)}

                # Build group travel styles for the prompt
                travel_styles = [f"{st.session_state.user_info['firstname']}: {st.session_state.user_info['likes']}"]
                if invite_roles:
                    with engine.connect() as conn:
                        for fname in invite_roles.keys():
                            friend_likes = conn.execute(
                                text("SELECT firstname, likes FROM users WHERE username = :u"), {"u": fname}
                            ).fetchone()
                            if friend_likes and friend_likes.likes:
                                travel_styles.append(f"{friend_likes.firstname}: {friend_likes.likes}")

                group_styles = "\n".join(f"  - {s}" for s in travel_styles)
                group_note = f"This is a group trip. Balance activities for everyone's styles:\n{group_styles}" if len(travel_styles) > 1 else f"Travel Style: {st.session_state.user_info['likes']}"

                prompt = f"""
                Suggest 12 individual activities for {p_sel}.
                {group_note}
                Format each as: Name | Type | Brief description
                Only return the list, one activity per line.

                ---MASTER_ITINERARY---
                Provide a full day-by-day itinerary for {nights} nights at {p_sel}.
                {group_note}
                """
                with st.spinner("Scouting the trail..."):
                    resp = client.models.generate_content(model="gemini-2.0-flash", contents=prompt).text
                    parts = resp.split('---MASTER_ITINERARY---')
                    st.session_state.temp_activities = [l for l in parts[0].strip().split('\n') if "|" in l]
                    st.session_state.master_itinerary = parts[1].strip() if len(parts) > 1 else ""

        # ── ACTIVITY PICKER + DAY-BY-DAY BOARD ──
        if st.session_state.temp_activities and len(date_range) == 2:
            st.divider()
            days = date_range_days(date_range[0], date_range[1])

            left, right = st.columns([1, 2])

            with left:
                st.subheader("💡 Suggested Activities")
                st.caption("Check activities to add them to a day")

                # Park image
                with engine.connect() as conn:
                    park_img = conn.execute(text("SELECT image_url FROM parks WHERE name = :n"), {"n": p_sel}).scalar()
                if park_img:
                    st.image(park_img, use_container_width=True)

                for i, act in enumerate(st.session_state.temp_activities):
                    parts = act.split('|')
                    name = parts[0].strip()
                    a_type = parts[1].strip() if len(parts) > 1 else "Activity"

                    with st.container(border=True):
                        ac1, ac2, ac3 = st.columns([2, 2, 1])
                        ac1.markdown(f"**{name}**")
                        ac1.caption(a_type)
                        target_day = ac2.selectbox(
                            "Day", options=[d[0] for d in days],
                            format_func=lambda d: f"Day {d}",
                            key=f"target_day_{i}"
                        )
                        if ac3.button("➕", key=f"add_{i}"):
                            activity_entry = {"id": f"act_{i}_{target_day}", "name": name, "type": a_type}
                            if target_day not in st.session_state.day_activities:
                                st.session_state.day_activities[target_day] = []
                            # Avoid duplicates
                            existing_names = [a["name"] for a in st.session_state.day_activities[target_day]]
                            if name not in existing_names:
                                st.session_state.day_activities[target_day].append(activity_entry)
                                st.toast(f"Added to Day {target_day}!")
                                st.rerun()

            with right:
                st.subheader("📅 Day-by-Day Itinerary")
                st.caption("Drag activities between days • Click ✕ to remove")

                render_dnd_itinerary(st.session_state.day_activities, days, editable=True)

                # Manual move fallback using dropdowns (works reliably in Streamlit)
                st.divider()
                st.markdown("**Move an activity between days:**")
                all_placed = []
                for day_num, acts in st.session_state.day_activities.items():
                    for act in acts:
                        all_placed.append((day_num, act))

                if all_placed:
                    mv1, mv2, mv3 = st.columns([3, 2, 1])
                    act_labels = [f"Day {d} — {a['name']}" for d, a in all_placed]
                    selected_act = mv1.selectbox("Activity", act_labels, key="move_act_sel")
                    move_to_day = mv2.selectbox("Move to Day", [d[0] for d in days], key="move_to_day")
                    if mv3.button("Move"):
                        idx = act_labels.index(selected_act)
                        src_day, act_to_move = all_placed[idx]
                        st.session_state.day_activities[src_day] = [
                            a for a in st.session_state.day_activities[src_day] if a["id"] != act_to_move["id"]
                        ]
                        if move_to_day not in st.session_state.day_activities:
                            st.session_state.day_activities[move_to_day] = []
                        st.session_state.day_activities[move_to_day].append(act_to_move)
                        st.rerun()

                st.divider()
                st.subheader("📖 AI Master Itinerary")
                st.markdown(st.session_state.master_itinerary)

                if st.button("💾 Save Trip"):
                    try:
                        with engine.begin() as conn:
                            tid_res = conn.execute(text("""
                                INSERT INTO trips (user_id, owner_id, trip_name, start_date, end_date)
                                VALUES (:u, :u, :n, :s, :e) RETURNING id
                            """), {"u": current_uid, "n": f"{p_sel} Trip", "s": date_range[0], "e": date_range[1]}).fetchone()
                            tid = tid_res[0]

                            conn.execute(text("""
                                INSERT INTO trip_participants (trip_id, user_id, role, invitation_status, invited_by)
                                VALUES (:t, :u, 'owner', 'accepted', :u)
                            """), {"t": tid, "u": current_uid})

                            for f_name, f_role in invite_roles.items():
                                fid = friend_options.get(f_name)
                                if fid:
                                    conn.execute(text("""
                                        INSERT INTO trip_participants (trip_id, user_id, role, invitation_status, invited_by)
                                        VALUES (:t, :u, :role, 'pending', :inviter)
                                    """), {"t": tid, "u": fid, "role": f_role, "inviter": current_uid})

                            p_id = int(df_parks[df_parks['name'] == p_sel]['id'].iloc[0])
                            notes_text = f"MASTER ITINERARY:\n{st.session_state.master_itinerary}"
                            conn.execute(text("INSERT INTO trip_parks (trip_id, park_id, notes) VALUES (:t, :p, :n)"),
                                         {"t": tid, "p": p_id, "n": notes_text})

                            # Save day-by-day activities
                            for day_num, activities in st.session_state.day_activities.items():
                                for order, act in enumerate(activities):
                                    conn.execute(text("""
                                        INSERT INTO trip_activities (trip_id, day_number, activity_name, activity_type, sort_order)
                                        VALUES (:tid, :day, :name, :atype, :order)
                                    """), {"tid": tid, "day": day_num, "name": act["name"], "atype": act.get("type", ""), "order": order})

                        st.success("Adventure locked in! 🎉")
                        st.balloons()
                        st.session_state.day_activities = {}
                        st.session_state.temp_activities = []

                    except Exception as e:
                        st.error(f"Database Error: {e}")

    # ─────────────────────────────────────────────
    # MY TRIPS TAB
    # ─────────────────────────────────────────────
    with my_trips_tab:
        st.header("Your Adventures")

        with engine.connect() as conn:
            trips = conn.execute(text("""
                SELECT DISTINCT t.id, t.trip_name, t.start_date, t.end_date,
                       tpk.id AS trip_park_id, tpk.notes,
                       p.name AS park_name, p.image_url AS park_image,
                       u_owner.firstname || ' ' || u_owner.lastname AS owner_name,
                       tp.role
                FROM trips t
                JOIN trip_participants tp ON t.id = tp.trip_id
                JOIN users u_owner ON t.owner_id = u_owner.id
                LEFT JOIN trip_parks tpk ON t.id = tpk.trip_id
                LEFT JOIN parks p ON tpk.park_id = p.id
                WHERE tp.user_id = :uid AND tp.invitation_status = 'accepted'
                ORDER BY t.start_date DESC
            """), {"uid": current_uid}).fetchall()
            all_parks = pd.read_sql(text("SELECT name, id FROM parks ORDER BY name"), conn)

        if not trips:
            st.info("No trips yet! Head to Plan Trip to start your first adventure 🏕️")
        else:
            for t in trips:
                role_badge = "👑 Owner" if t.role == "owner" else "✏️ Collaborator" if t.role == "collaborator" else "👁️ Viewer"
                label = f"📍 {t.trip_name}  —  {role_badge}"
                editable = can_edit(t.role)

                with st.expander(label):
                    if t.park_image:
                        st.image(t.park_image, use_container_width=True)

                    edit_key = f"editing_{t.id}"
                    confirm_del_key = f"confirm_del_trip_{t.id}"
                    if edit_key not in st.session_state:
                        st.session_state[edit_key] = False
                    if confirm_del_key not in st.session_state:
                        st.session_state[confirm_del_key] = False

                    col_info, col_btns = st.columns([3, 1])
                    with col_info:
                        st.caption(f"📅 {t.start_date} → {t.end_date}  •  🏔️ {t.park_name or 'Multiple Parks'}")
                        if t.role != "owner":
                            st.caption(f"Planned by **{t.owner_name}**")
                    with col_btns:
                        if editable:
                            toggle_label = "Cancel" if st.session_state[edit_key] else "✏️ Edit"
                            if st.button(toggle_label, key=f"toggle_edit_{t.id}"):
                                st.session_state[edit_key] = not st.session_state[edit_key]
                                st.rerun()
                        if t.notes:
                            pdf_b = create_pdf(t.notes, t.trip_name, st.session_state.user_info['firstname'])
                            st.download_button("📥 PDF", pdf_b, f"Trip_{t.id}.pdf", key=f"dl_{t.id}")

                        # ── DELETE TRIP (owner only) with confirmation ──
                        if t.role == "owner":
                            if not st.session_state[confirm_del_key]:
                                if st.button("🗑️ Delete", key=f"del_trip_btn_{t.id}"):
                                    st.session_state[confirm_del_key] = True
                                    st.rerun()
                            else:
                                st.warning("Delete this trip permanently?")
                                dy, dn = st.columns(2)
                                if dy.button("Yes, delete", key=f"confirm_del_yes_{t.id}"):
                                    with engine.connect() as conn2:
                                        conn2.execute(text("DELETE FROM trips WHERE id = :tid"), {"tid": t.id})
                                        conn2.commit()
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
                        start = t.start_date if isinstance(t.start_date, date) else (date.fromisoformat(str(t.start_date)) if t.start_date else date.today())
                        end = t.end_date if isinstance(t.end_date, date) else (date.fromisoformat(str(t.end_date)) if t.end_date else date.today())
                        new_dates = st.date_input("Dates", value=(start, end), key=f"dates_{t.id}")
                        new_park = st.selectbox(
                            "Park", options=all_parks['name'],
                            index=int(all_parks[all_parks['name'] == t.park_name].index[0]) if t.park_name in all_parks['name'].values else 0,
                            key=f"park_{t.id}"
                        )
                        new_notes = st.text_area("Itinerary Notes", value=t.notes or "", height=250, key=f"notes_{t.id}")

                        # ── Day-by-day activity editor in edit mode ──
                        st.markdown("**Edit Day Activities**")
                        with engine.connect() as conn2:
                            saved_acts = conn2.execute(text("""
                                SELECT id, day_number, activity_name, activity_type, sort_order
                                FROM trip_activities WHERE trip_id = :tid
                                ORDER BY day_number, sort_order
                            """), {"tid": t.id}).fetchall()

                        edit_days = date_range_days(start, end)
                        edit_day_acts = {d[0]: [] for d in edit_days}
                        for a in saved_acts:
                            if a.day_number in edit_day_acts:
                                edit_day_acts[a.day_number].append({"id": str(a.id), "name": a.activity_name, "type": a.activity_type or ""})

                        render_dnd_itinerary(edit_day_acts, edit_days, editable=True)

                        # Add a new activity manually
                        st.markdown("**Add a new activity:**")
                        na1, na2, na3, na4 = st.columns([3, 2, 2, 1])
                        new_act_name = na1.text_input("Activity name", key=f"new_act_name_{t.id}")
                        new_act_type = na2.text_input("Type", key=f"new_act_type_{t.id}")
                        new_act_day = na3.selectbox("Day", [d[0] for d in edit_days], key=f"new_act_day_{t.id}")
                        if na4.button("Add", key=f"add_new_act_{t.id}"):
                            if new_act_name:
                                with engine.connect() as conn2:
                                    max_order = conn2.execute(text("""
                                        SELECT COALESCE(MAX(sort_order), 0) FROM trip_activities
                                        WHERE trip_id = :tid AND day_number = :day
                                    """), {"tid": t.id, "day": new_act_day}).scalar()
                                    conn2.execute(text("""
                                        INSERT INTO trip_activities (trip_id, day_number, activity_name, activity_type, sort_order)
                                        VALUES (:tid, :day, :name, :atype, :order)
                                    """), {"tid": t.id, "day": new_act_day, "name": new_act_name, "atype": new_act_type, "order": max_order + 1})
                                    conn2.commit()
                                st.rerun()

                        # Move/delete saved activities
                        if saved_acts:
                            st.markdown("**Move or delete a saved activity:**")
                            for sa in saved_acts:
                                sa1, sa2, sa3, sa4 = st.columns([3, 2, 1, 1])
                                sa1.write(f"Day {sa.day_number} — {sa.activity_name}")
                                move_day = sa2.selectbox("Move to", [d[0] for d in edit_days], key=f"mv_day_{sa.id}", index=sa.day_number - 1)
                                if sa3.button("Move", key=f"mv_btn_{sa.id}"):
                                    with engine.connect() as conn2:
                                        conn2.execute(text("UPDATE trip_activities SET day_number=:day WHERE id=:aid"), {"day": move_day, "aid": sa.id})
                                        conn2.commit()
                                    st.rerun()
                                if sa4.button("🗑️", key=f"del_act_{sa.id}"):
                                    with engine.connect() as conn2:
                                        conn2.execute(text("DELETE FROM trip_activities WHERE id=:aid"), {"aid": sa.id})
                                        conn2.commit()
                                    st.rerun()

                        if t.role == "owner":
                            st.markdown("**Manage Participant Permissions**")
                            with engine.connect() as conn2:
                                participants = conn2.execute(text("""
                                    SELECT tp.id, u.username, u.firstname, tp.role, tp.invitation_status
                                    FROM trip_participants tp JOIN users u ON tp.user_id = u.id
                                    WHERE tp.trip_id = :tid AND tp.role != 'owner'
                                """), {"tid": t.id}).fetchall()
                            for p in participants:
                                pc1, pc2, pc3 = st.columns([2, 2, 1])
                                pc1.write(f"**{p.firstname}** (@{p.username})")
                                pc1.caption(f"Status: {p.invitation_status}")
                                new_role = pc2.selectbox("Role", ["collaborator", "viewer"],
                                                         index=0 if p.role == "collaborator" else 1,
                                                         key=f"role_{t.id}_{p.id}")
                                if pc3.button("Update", key=f"update_role_{t.id}_{p.id}"):
                                    with engine.connect() as conn2:
                                        conn2.execute(text("UPDATE trip_participants SET role=:r WHERE id=:pid"), {"r": new_role, "pid": p.id})
                                        conn2.commit()
                                    st.rerun()

                        if st.button("💾 Save Changes", key=f"save_{t.id}"):
                            try:
                                new_park_id = int(all_parks[all_parks['name'] == new_park]['id'].iloc[0])
                                with engine.begin() as conn2:
                                    conn2.execute(text("""
                                        UPDATE trips SET trip_name=:name, start_date=:s, end_date=:e WHERE id=:tid
                                    """), {
                                        "name": new_name,
                                        "s": new_dates[0] if len(new_dates) > 1 else start,
                                        "e": new_dates[1] if len(new_dates) > 1 else end,
                                        "tid": t.id
                                    })
                                    conn2.execute(text("UPDATE trip_parks SET park_id=:p, notes=:n WHERE id=:tpkid"),
                                                  {"p": new_park_id, "n": new_notes, "tpkid": t.trip_park_id})
                                st.success("Trip updated! ✅")
                                st.session_state[edit_key] = False
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error saving: {e}")

                    # ── READ-ONLY VIEW ──
                    else:
                        # Show saved day activities
                        with engine.connect() as conn2:
                            saved_acts = conn2.execute(text("""
                                SELECT day_number, activity_name, activity_type
                                FROM trip_activities WHERE trip_id = :tid
                                ORDER BY day_number, sort_order
                            """), {"tid": t.id}).fetchall()

                        if saved_acts:
                            st.divider()
                            st.markdown("**Trip Activities:**")
                            view_days = date_range_days(t.start_date, t.end_date)
                            day_map = {}
                            for a in saved_acts:
                                day_map.setdefault(a.day_number, []).append(a)
                            for day_num, day_date in view_days:
                                acts = day_map.get(day_num, [])
                                if acts:
                                    st.markdown(f"**Day {day_num} — {day_date.strftime('%a, %b %d')}**")
                                    for a in acts:
                                        st.caption(f"  • {a.activity_name} _{a.activity_type}_")

                        if t.notes:
                            st.divider()
                            st.markdown(t.notes)

                    # Trip crew (always visible)
                    with engine.connect() as conn2:
                        participants = conn2.execute(text("""
                            SELECT u.firstname, u.lastname, u.username, tp.role, tp.invitation_status
                            FROM trip_participants tp JOIN users u ON tp.user_id = u.id
                            WHERE tp.trip_id = :tid ORDER BY tp.role
                        """), {"tid": t.id}).fetchall()
                    if participants:
                        st.divider()
                        st.markdown("**Trip Crew:**")
                        for p in participants:
                            status_icon = "✅" if p.invitation_status == "accepted" else "⏳" if p.invitation_status == "pending" else "❌"
                            role_icon = "👑" if p.role == "owner" else "✏️" if p.role == "collaborator" else "👁️"
                            st.caption(f"{status_icon} {p.firstname} {p.lastname} (@{p.username}) — {role_icon} {p.role}")
