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

# Initialize session states
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
    clean_text = itinerary_text.replace('\u2013', '-').replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"')
    pdf.multi_cell(0, 7, clean_text.encode('latin-1', 'ignore').decode('latin-1'))
    return bytes(pdf.output())

def get_pending_trip_invites(conn, uid):
    return conn.execute(text("""
        SELECT 
            tp.id AS participant_id,
            t.id AS trip_id,
            t.trip_name,
            t.start_date,
            t.end_date,
            u_owner.firstname || ' ' || u_owner.lastname AS invited_by_name,
            p.name AS park_name
        FROM trip_participants tp
        JOIN trips t ON tp.trip_id = t.id
        JOIN users u_owner ON t.owner_id = u_owner.id
        LEFT JOIN trip_parks tpk ON t.id = tpk.trip_id
        LEFT JOIN parks p ON tpk.park_id = p.id
        WHERE tp.user_id = :uid 
          AND tp.invitation_status = 'pending'
          AND tp.role != 'owner'
    """), {"uid": uid}).fetchall()

def get_pending_count(uid):
    with engine.connect() as conn:
        friend_count = conn.execute(text("""
            SELECT COUNT(*) FROM friendships 
            WHERE friend_id = :uid AND status = 'pending'
        """), {"uid": uid}).scalar()
        trip_count = conn.execute(text("""
            SELECT COUNT(*) FROM trip_participants 
            WHERE user_id = :uid AND invitation_status = 'pending' AND role != 'owner'
        """), {"uid": uid}).scalar()
    return (friend_count or 0) + (trip_count or 0)

def can_edit(role):
    """Returns True if the user's role allows editing."""
    return role in ('owner', 'collaborator')

# --- AUTH ---
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
                st.error(f"Error creating account: {e}")

# --- MAIN APP ---
else:
    current_uid = st.session_state.user_info['id']

    with st.sidebar:
        st.write(f"Welcome back, **{st.session_state.user_info['firstname']}**")
        pending_count = get_pending_count(current_uid)
        if pending_count > 0:
            st.warning(f"🔔 You have **{pending_count}** pending notification(s)")
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
                            conn.execute(
                                text("INSERT INTO friendships (user_id, friend_id, status) VALUES (:u, :f, 'pending')"),
                                {"u": current_uid, "f": f_res[0]}
                            )
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
                SELECT u.username, u.firstname, u.likes 
                FROM users u
                JOIN friendships f ON (u.id = f.friend_id OR u.id = f.user_id)
                WHERE (f.user_id = :uid OR f.friend_id = :uid) 
                  AND f.status = 'accepted' 
                  AND u.id != :uid
            """), {"uid": current_uid}).fetchall()

            if not my_friends:
                st.info("No friends yet. Use the search above to grow your crew!")
            else:
                for f in my_friends:
                    with st.container(border=True):
                        st.write(f"**{f[1]}** (@{f[0]})")
                        st.caption(f"Style: {f[2]}")

        st.divider()

        st.subheader("Incoming Friend Requests")
        with engine.connect() as conn:
            pending = conn.execute(text("""
                SELECT f.id, u.username 
                FROM friendships f 
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
                        conn2.execute(text("UPDATE friendships SET status = 'accepted' WHERE id = :rid"), {"rid": req[0]})
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
                    st.caption(
                        f"📍 {inv.park_name or 'Multiple Parks'}  •  "
                        f"📅 {inv.start_date} → {inv.end_date}  •  "
                        f"Invited by **{inv.invited_by_name}**"
                    )
                    col1, col2 = st.columns(2)
                    if col1.button("Accept 🎒", key=f"accept_trip_{inv.participant_id}"):
                        with engine.connect() as conn2:
                            conn2.execute(text("""
                                UPDATE trip_participants 
                                SET invitation_status = 'accepted', responded_at = CURRENT_TIMESTAMP
                                WHERE id = :pid
                            """), {"pid": inv.participant_id})
                            conn2.commit()
                        st.success(f"You're going to **{inv.trip_name}**! 🎉")
                        st.rerun()
                    if col2.button("Decline ❌", key=f"decline_trip_{inv.participant_id}"):
                        with engine.connect() as conn2:
                            conn2.execute(text("""
                                UPDATE trip_participants 
                                SET invitation_status = 'declined', responded_at = CURRENT_TIMESTAMP
                                WHERE id = :pid
                            """), {"pid": inv.participant_id})
                            conn2.commit()
                        st.info("Invite declined.")
                        st.rerun()

    # ─────────────────────────────────────────────
    # PLANNING TAB
    # ─────────────────────────────────────────────
    with plan_tab:
        with engine.connect() as conn:
            friends_res = conn.execute(text("""
                SELECT u.id, u.username FROM users u
                JOIN friendships f ON (u.id = f.friend_id OR u.id = f.user_id)
                WHERE ((f.user_id = :uid OR f.friend_id = :uid) AND f.status = 'accepted') AND u.id != :uid
            """), {"uid": current_uid}).fetchall()
            friend_options = {fr[1]: fr[0] for fr in friends_res}  # {username: id}
            df_parks = pd.read_sql(text("SELECT name, id FROM parks ORDER BY name"), conn)

        p_sel = st.selectbox("Select Park", options=df_parks['name'])
        date_range = st.date_input("Dates", value=(date.today(), date.today()))

        # ── Per-friend role selector on invite ──
        st.markdown("**Invite Friends**")
        invite_roles = {}
        if friend_options:
            for fname in friend_options:
                col1, col2 = st.columns([0.6, 0.4])
                include = col1.checkbox(fname, key=f"invite_check_{fname}")
                if include:
                    role = col2.selectbox(
                        "Role",
                        options=["collaborator", "viewer"],
                        key=f"invite_role_{fname}",
                        help="Collaborators can edit the trip. Viewers can only see it."
                    )
                    invite_roles[fname] = role
        else:
            st.caption("Add friends to invite them to trips.")

        if st.button("Generate Plan"):
            if len(date_range) < 2:
                st.error("Please select a date range.")
            else:
                nights = (date_range[1] - date_range[0]).days
                prompt = f"""
                Suggest 10 individual activities for {p_sel} (Style: {st.session_state.user_info['likes']}).
                Format: Name | Type | Description
                
                ---MASTER_ITINERARY---
                Provide a full day-by-day itinerary for {nights} nights.
                """
                with st.spinner("Scouting..."):
                    resp = client.models.generate_content(model="gemini-2.0-flash", contents=prompt).text
                    parts = resp.split('---MASTER_ITINERARY---')
                    st.session_state.temp_activities = [l for l in parts[0].strip().split('\n') if "|" in l]
                    st.session_state.master_itinerary = parts[1].strip() if len(parts) > 1 else ""

        if st.session_state.temp_activities:
            st.divider()
            c1, c2 = st.columns(2)
            with c1:
                st.subheader("💡 Activities")
                for i, act in enumerate(st.session_state.temp_activities):
                    details = act.split('|')
                    name = details[0].strip()
                    a_type = details[1].strip() if len(details) > 1 else "Misc"
                    with st.container(border=True):
                        st.markdown(f"**{name}** ({a_type})")
                        if st.button("Add ➕", key=f"add_act_{i}"):
                            st.session_state.curated_itinerary.append(name)
                            st.toast(f"Added {name}")
            with c2:
                st.subheader("📅 Full Day-by-Day Itinerary")
                with engine.connect() as conn:
                    park_img = conn.execute(
                        text("SELECT image_url FROM parks WHERE name = :n"), {"n": p_sel}
                    ).scalar()
                    if park_img:
                        st.image(park_img, use_container_width=True)
                st.markdown(st.session_state.master_itinerary)

                if st.button("💾 Save Everything"):
                    try:
                        with engine.begin() as conn:
                            tid_res = conn.execute(text("""
                                INSERT INTO trips (user_id, owner_id, trip_name, start_date, end_date) 
                                VALUES (:u, :u, :n, :s, :e) RETURNING id
                            """), {
                                "u": current_uid,
                                "n": f"{p_sel} Trip",
                                "s": date_range[0],
                                "e": date_range[1]
                            }).fetchone()
                            tid = tid_res[0]

                            conn.execute(text("""
                                INSERT INTO trip_participants (trip_id, user_id, role, invitation_status, invited_by) 
                                VALUES (:t, :u, 'owner', 'accepted', :u)
                            """), {"t": tid, "u": current_uid})

                            # Insert friends with their individual roles
                            for f_name, f_role in invite_roles.items():
                                fid = friend_options.get(f_name)
                                if fid:
                                    conn.execute(text("""
                                        INSERT INTO trip_participants 
                                            (trip_id, user_id, role, invitation_status, invited_by) 
                                        VALUES (:t, :u, :role, 'pending', :inviter)
                                    """), {"t": tid, "u": fid, "role": f_role, "inviter": current_uid})

                            p_id = int(df_parks[df_parks['name'] == p_sel]['id'].iloc[0])
                            final_notes = (
                                f"MASTER ITINERARY:\n{st.session_state.master_itinerary}\n\n"
                                f"SAVED ACTIVITIES:\n" + "\n".join(st.session_state.curated_itinerary)
                            )
                            conn.execute(text("""
                                INSERT INTO trip_parks (trip_id, park_id, notes) 
                                VALUES (:t, :p, :n)
                            """), {"t": tid, "p": p_id, "n": final_notes})

                        st.success("Adventure locked in! Invites sent to your crew 🎉")
                        st.balloons()

                    except Exception as e:
                        st.error(f"Database Error: {e}")

    # ─────────────────────────────────────────────
    # MY TRIPS TAB — inline editing with permissions
    # ─────────────────────────────────────────────
    with my_trips_tab:
        st.header("Your Adventures")

        with engine.connect() as conn:
            trips = conn.execute(text("""
                SELECT DISTINCT
                    t.id,
                    t.trip_name,
                    t.start_date,
                    t.end_date,
                    tpk.id AS trip_park_id,
                    tpk.notes,
                    p.name AS park_name,
                    p.image_url AS park_image,
                    u_owner.firstname || ' ' || u_owner.lastname AS owner_name,
                    tp.role
                FROM trips t
                JOIN trip_participants tp ON t.id = tp.trip_id
                JOIN users u_owner ON t.owner_id = u_owner.id
                LEFT JOIN trip_parks tpk ON t.id = tpk.trip_id
                LEFT JOIN parks p ON tpk.park_id = p.id
                WHERE tp.user_id = :uid 
                  AND tp.invitation_status = 'accepted'
                ORDER BY t.start_date DESC
            """), {"uid": current_uid}).fetchall()
            all_parks = pd.read_sql(text("SELECT name, id FROM parks ORDER BY name"), conn)

        if not trips:
            st.info("No trips yet! Head to the Plan Trip tab to start your first adventure 🏕️")
        else:
            for t in trips:
                role_badge = "👑 Owner" if t.role == "owner" else "✏️ Collaborator" if t.role == "collaborator" else "👁️ Viewer"
                label = f"📍 {t.trip_name}  —  {role_badge}"
                editable = can_edit(t.role)

                with st.expander(label):
                    if t.park_image:
                        st.image(t.park_image, use_container_width=True)

                    edit_key = f"editing_{t.id}"
                    if edit_key not in st.session_state:
                        st.session_state[edit_key] = False

                    col_info, col_btn = st.columns([3, 1])
                    with col_info:
                        st.caption(f"📅 {t.start_date} → {t.end_date}  •  🏔️ {t.park_name or 'Multiple Parks'}")
                        if t.role != "owner":
                            st.caption(f"Planned by **{t.owner_name}**")
                    with col_btn:
                        if editable:
                            toggle_label = "Cancel" if st.session_state[edit_key] else "✏️ Edit Trip"
                            if st.button(toggle_label, key=f"toggle_edit_{t.id}"):
                                st.session_state[edit_key] = not st.session_state[edit_key]
                                st.rerun()
                        if t.notes:
                            pdf_b = create_pdf(t.notes, t.trip_name, st.session_state.user_info['firstname'])
                            st.download_button("📥 PDF", pdf_b, f"Trip_{t.id}.pdf", key=f"dl_{t.id}")

                    # ── INLINE EDIT FORM ──
                    if editable and st.session_state[edit_key]:
                        st.divider()
                        st.markdown("### ✏️ Edit Trip")

                        new_name = st.text_input("Trip Name", value=t.trip_name, key=f"name_{t.id}")
                        new_dates = st.date_input(
                            "Dates",
                            value=(t.start_date, t.end_date),
                            key=f"dates_{t.id}"
                        )
                        new_park = st.selectbox(
                            "Park",
                            options=all_parks['name'],
                            index=int(all_parks[all_parks['name'] == t.park_name].index[0]) if t.park_name in all_parks['name'].values else 0,
                            key=f"park_{t.id}"
                        )
                        new_notes = st.text_area(
                            "Itinerary Notes",
                            value=t.notes or "",
                            height=300,
                            key=f"notes_{t.id}"
                        )

                        # ── Owner-only: manage participant roles ──
                        if t.role == "owner":
                            st.markdown("**Manage Participant Permissions**")
                            with engine.connect() as conn2:
                                participants = conn2.execute(text("""
                                    SELECT tp.id, u.username, u.firstname, tp.role, tp.invitation_status
                                    FROM trip_participants tp
                                    JOIN users u ON tp.user_id = u.id
                                    WHERE tp.trip_id = :tid AND tp.role != 'owner'
                                """), {"tid": t.id}).fetchall()

                            for p in participants:
                                pc1, pc2, pc3 = st.columns([2, 2, 1])
                                pc1.write(f"**{p.firstname}** (@{p.username})")
                                pc1.caption(f"Status: {p.invitation_status}")
                                new_role = pc2.selectbox(
                                    "Role",
                                    options=["collaborator", "viewer"],
                                    index=0 if p.role == "collaborator" else 1,
                                    key=f"role_{t.id}_{p.id}"
                                )
                                if pc3.button("Update", key=f"update_role_{t.id}_{p.id}"):
                                    with engine.connect() as conn2:
                                        conn2.execute(text("""
                                            UPDATE trip_participants SET role = :r WHERE id = :pid
                                        """), {"r": new_role, "pid": p.id})
                                        conn2.commit()
                                    st.success(f"Updated {p.firstname}'s role to {new_role}.")
                                    st.rerun()

                        if st.button("💾 Save Changes", key=f"save_{t.id}"):
                            try:
                                new_park_id = int(all_parks[all_parks['name'] == new_park]['id'].iloc[0])
                                with engine.begin() as conn2:
                                    conn2.execute(text("""
                                        UPDATE trips 
                                        SET trip_name = :name, start_date = :s, end_date = :e
                                        WHERE id = :tid
                                    """), {
                                        "name": new_name,
                                        "s": new_dates[0] if len(new_dates) > 1 else t.start_date,
                                        "e": new_dates[1] if len(new_dates) > 1 else t.end_date,
                                        "tid": t.id
                                    })
                                    conn2.execute(text("""
                                        UPDATE trip_parks 
                                        SET park_id = :p, notes = :n
                                        WHERE id = :tpkid
                                    """), {"p": new_park_id, "n": new_notes, "tpkid": t.trip_park_id})

                                st.success("Trip updated! ✅")
                                st.session_state[edit_key] = False
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error saving: {e}")

                    # ── READ-ONLY VIEW ──
                    else:
                        if t.notes:
                            st.divider()
                            st.markdown(t.notes)

                    # Trip crew (always visible)
                    with engine.connect() as conn2:
                        participants = conn2.execute(text("""
                            SELECT u.firstname, u.lastname, u.username, tp.role, tp.invitation_status
                            FROM trip_participants tp
                            JOIN users u ON tp.user_id = u.id
                            WHERE tp.trip_id = :tid
                            ORDER BY tp.role
                        """), {"tid": t.id}).fetchall()

                    if participants:
                        st.divider()
                        st.markdown("**Trip Crew:**")
                        for p in participants:
                            status_icon = "✅" if p.invitation_status == "accepted" else "⏳" if p.invitation_status == "pending" else "❌"
                            role_icon = "👑" if p.role == "owner" else "✏️" if p.role == "collaborator" else "👁️"
                            st.caption(f"{status_icon} {p.firstname} {p.lastname} (@{p.username}) — {role_icon} {p.role}")
