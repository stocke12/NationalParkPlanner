"""
seed_phase4.py — Seeds crowd calendar, seasonal warnings, and challenges.
Run once after migration_phase4.sql:  python seed_phase4.py
Requires DATABASE_URL in .env (same as the main app).
"""
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.environ["DATABASE_URL"])

# ─────────────────────────────────────────────
# CROWD CALENDAR
# Approximate crowd levels based on NPS public visitation data.
# Maps park name → {month: (avg_visitors_thousands, level, note)}
# ─────────────────────────────────────────────

CROWD_DATA = {
    "Yellowstone": {
        1: (30000,  "Low",       "Roads mostly closed; only North Entrance open"),
        2: (28000,  "Low",       "Winter wildlife viewing season"),
        3: (50000,  "Low",       "Early spring; many roads still closed"),
        4: (120000, "Moderate",  "Roads begin opening; unpredictable weather"),
        5: (350000, "High",      "Busy spring season; some facilities opening"),
        6: (700000, "Very High", "Peak summer begins; book lodging far in advance"),
        7: (900000, "Very High", "Busiest month; expect long waits at entry gates"),
        8: (880000, "Very High", "Still peak; slightly less crowded late month"),
        9: (600000, "High",      "Shoulder season; great wildlife viewing"),
        10:(250000, "Moderate",  "Fall colors; most facilities close mid-month"),
        11:(45000,  "Low",       "Most roads close; winter services begin"),
        12:(32000,  "Low",       "Quiet winter; snowmobile season starts"),
    },
    "Grand Canyon": {
        1: (300000, "Low",       "Cool and quiet; snow possible on rim"),
        2: (320000, "Low",       "Still off-peak; great hiking weather"),
        3: (500000, "Moderate",  "Spring break crowds; pleasant temperatures"),
        4: (600000, "High",      "Popular month; inner canyon gets warm"),
        5: (650000, "High",      "Inner canyon hot; hike early morning only"),
        6: (700000, "Very High", "South Rim very busy; inner canyon dangerously hot"),
        7: (750000, "Very High", "Hottest month; monsoon season begins"),
        8: (720000, "Very High", "Monsoon storms; flash flood risk in canyon"),
        9: (580000, "High",      "Shoulder season; storms taper off"),
        10:(550000, "High",      "Beautiful fall; very popular month"),
        11:(380000, "Moderate",  "Quieter; occasional snow on rim"),
        12:(290000, "Low",       "Holiday crowds around Christmas week"),
    },
    "Yosemite": {
        1: (120000, "Low",       "Quiet winter; Tioga Road closed"),
        2: (130000, "Low",       "Horsetail Fall firefall effect in good years"),
        3: (200000, "Moderate",  "Waterfalls peak; roads may be icy"),
        4: (350000, "High",      "Waterfalls at peak; Valley gets busy"),
        5: (450000, "High",      "Peak waterfall season; Tioga Road opens"),
        6: (550000, "Very High", "Summer crowds; timed entry reservation required"),
        7: (600000, "Very High", "Busiest month; book months in advance"),
        8: (580000, "Very High", "Still peak; smoke from wildfires possible"),
        9: (420000, "High",      "Crowds thin; great fall hiking"),
        10:(280000, "Moderate",  "Tioga Road closes; Valley beautiful"),
        11:(160000, "Low",       "Quiet; first snows possible"),
        12:(130000, "Low",       "Peaceful winter; limited facilities"),
    },
    "Zion": {
        1: (100000, "Low",       "Cool and quiet; Narrows may be closed"),
        2: (120000, "Low",       "Off-peak; pleasant canyon hiking"),
        3: (250000, "Moderate",  "Spring break; Angel's Landing fills early"),
        4: (400000, "High",      "Wildflowers; very popular month"),
        5: (480000, "Very High", "Peak season begins; shuttle required"),
        6: (500000, "Very High", "Hot in canyon; hike early"),
        7: (490000, "Very High", "Flash flood season; check forecasts"),
        8: (480000, "Very High", "Still peak; monsoon rain possible"),
        9: (400000, "High",      "Shoulder season; cooler temperatures"),
        10:(350000, "High",      "Fall colors; very popular"),
        11:(180000, "Moderate",  "Quieter; some narrows access"),
        12:(110000, "Low",       "Winter quiet; possible snow at elevation"),
    },
    "Great Smoky Mountains": {
        1: (400000, "Low",       "Quiet winter; Clingmans Dome road closed"),
        2: (380000, "Low",       "Off-peak; wildflower season begins late month"),
        3: (700000, "Moderate",  "Spring break crowds; wildflowers starting"),
        4: (1100000,"High",      "Peak wildflower season; very popular"),
        5: (1200000,"High",      "Spring wildflowers and waterfalls"),
        6: (1300000,"Very High", "Summer peak; Cades Cove very busy"),
        7: (1500000,"Very High", "Busiest month in most-visited US park"),
        8: (1400000,"Very High", "Still peak summer crowds"),
        9: (1200000,"High",      "Early fall color; still very busy"),
        10:(1600000,"Very High", "Peak fall foliage; absolute busiest month"),
        11:(700000, "Moderate",  "Foliage ends; quieter"),
        12:(500000, "Low",       "Holiday week busy; otherwise quiet"),
    },
    "Rocky Mountain": {
        1: (200000, "Low",       "Trail Ridge Road closed; snowshoeing popular"),
        2: (180000, "Low",       "Quiet winter; wildlife viewing"),
        3: (230000, "Low",       "Still winter; elk calving begins"),
        4: (280000, "Moderate",  "Spring thaw; unpredictable weather"),
        5: (400000, "Moderate",  "Trail Ridge Road opens late month"),
        6: (600000, "High",      "Summer begins; Trail Ridge Road open"),
        7: (750000, "Very High", "Peak month; arrive before 9am"),
        8: (720000, "Very High", "Busy; afternoon thunderstorms daily"),
        9: (550000, "High",      "Elk rut; stunning fall scenery"),
        10:(320000, "Moderate",  "Trail Ridge Road closes; quieter"),
        11:(210000, "Low",       "Winter approaching; fewer crowds"),
        12:(190000, "Low",       "Holiday visitors; winter recreation"),
    },
    "Acadia": {
        1: (30000,  "Low",       "Very quiet; some trails icy"),
        2: (25000,  "Low",       "Off-season; few facilities open"),
        3: (40000,  "Low",       "Still quiet; cold and brisk"),
        4: (80000,  "Moderate",  "Park road opens; migrating birds"),
        5: (200000, "Moderate",  "Park Loop Road opens fully"),
        6: (400000, "High",      "Summer season begins"),
        7: (650000, "Very High", "Peak month; Cadillac Mountain crowded at sunrise"),
        8: (680000, "Very High", "Busiest month; book lodging early"),
        9: (500000, "High",      "Shoulder season; early fall color"),
        10:(350000, "High",      "Peak fall foliage; very popular"),
        11:(80000,  "Low",       "Quiet; most facilities closed"),
        12:(35000,  "Low",       "Winter quiet; cross-country skiing"),
    },
    "Olympic": {
        1: (200000, "Low",       "Rainiest month; trails may flood"),
        2: (190000, "Low",       "Still rainy; quiet off-season"),
        3: (230000, "Low",       "Spring approaching; wildflowers on coast"),
        4: (300000, "Moderate",  "Good whale watching; waterfalls peak"),
        5: (400000, "Moderate",  "Wildflowers in meadows; Hurricane Ridge opens"),
        6: (520000, "High",      "Summer begins; Hoh Rainforest popular"),
        7: (650000, "Very High", "Peak season; driest month"),
        8: (640000, "Very High", "Still peak; mountain meadows in bloom"),
        9: (450000, "High",      "Shoulder; fewer crowds on beaches"),
        10:(300000, "Moderate",  "Fall colors; rain returns"),
        11:(200000, "Low",       "Quiet and rainy"),
        12:(190000, "Low",       "Winter; some roads close"),
    },
}

# ─────────────────────────────────────────────
# SEASONAL WARNINGS
# ─────────────────────────────────────────────

SEASONAL_WARNINGS = [
    # Yellowstone
    ("Yellowstone", 11, 4,  "Closure",  "Most interior roads closed to wheeled vehicles Nov–Apr; only North Entrance open year-round"),
    ("Yellowstone", 6,  8,  "Wildlife", "Bison calving and grizzly activity peaks Jun–Aug; maintain 100-yard distance from bears"),
    ("Yellowstone", 7,  8,  "Smoke",    "Wildfire smoke common Jul–Aug; check air quality before strenuous hikes"),

    # Grand Canyon
    ("Grand Canyon", 6, 9,  "Heat",     "Inner canyon temperatures exceed 110°F Jun–Sep; do not hike to the river and back in one day"),
    ("Grand Canyon", 7, 9,  "Flooding", "Monsoon season Jul–Sep; flash floods can occur with no warning in side canyons"),
    ("Grand Canyon", 12,2,  "Snow",     "North Rim closed mid-Oct through mid-May due to snow"),

    # Yosemite
    ("Yosemite", 12, 3,  "Closure",  "Tioga Pass Road (Hwy 120) closed typically Nov–May; check NPS road status"),
    ("Yosemite", 7,  9,  "Smoke",    "Wildfire smoke can severely impact air quality and visibility Jul–Sep"),
    ("Yosemite", 3,  5,  "Flooding", "Valley roads and trails may flood during snowmelt; check current conditions"),

    # Zion
    ("Zion", 7,  9,  "Flooding", "Flash flood season Jul–Sep; The Narrows may close with little notice"),
    ("Zion", 6,  8,  "Heat",     "Canyon temperatures regularly exceed 100°F Jun–Aug; hike before 10am"),

    # Great Smoky Mountains
    ("Great Smoky Mountains", 7, 9,  "Smoke",    "Wildfire smoke and summer haze reduce visibility Jul–Sep"),
    ("Great Smoky Mountains", 12,3,  "Closure",  "Clingmans Dome Road closed Dec–Mar due to ice and snow"),
    ("Great Smoky Mountains", 4,  6,  "Wildlife", "Black bear activity peaks during spring foraging; store food properly"),

    # Rocky Mountain
    ("Rocky Mountain", 11, 5,  "Closure",  "Trail Ridge Road (highest continuous paved road in US) closed Nov–late May"),
    ("Rocky Mountain", 7,  8,  "Lightning","Afternoon thunderstorms nearly daily Jul–Aug; be below treeline by noon"),
    ("Rocky Mountain", 9,  10, "Wildlife", "Bull elk rut Sep–Oct; maintain distance and never approach bugling elk"),

    # Glacier
    ("Glacier", 11, 6,  "Closure",  "Going-to-the-Sun Road typically closed Nov–late Jun; check NPS for opening dates"),
    ("Glacier", 7,  9,  "Wildlife", "Grizzly bear activity high Jul–Sep; carry bear spray and make noise on trails"),
    ("Glacier", 7,  8,  "Smoke",    "Wildfire smoke frequently impacts the park Jul–Aug"),

    # Death Valley
    ("Death Valley", 5,  9,  "Heat",     "Extreme heat May–Sep; temperatures regularly exceed 120°F in valley floor; avoid all strenuous activity"),
    ("Death Valley", 2,  4,  "Flooding", "Rare but intense rainstorms Feb–Apr can cause flash floods and road closures"),

    # Olympic
    ("Olympic", 11, 3,  "Flooding", "Rainforest trails and coastal roads prone to flooding Nov–Mar; check conditions"),
    ("Olympic", 1,  2,  "Closure",  "Hurricane Ridge Road closes frequently for snow and ice Dec–Mar"),

    # Acadia
    ("Acadia", 12, 3,  "Closure",  "Park Loop Road closes to vehicles Dec–Mar; accessible by foot and ski only"),
    ("Acadia", 7,  8,  "Wildlife", "Peregrine falcons nest on cliffs Jul–Aug; some climbing routes closed to protect nests"),

    # Bryce Canyon
    ("Bryce Canyon", 12, 3,  "Snow",     "Heavy snow Dec–Mar; roads may be icy but hoodoos in snow are spectacular"),
    ("Bryce Canyon", 7,  8,  "Lightning","Afternoon thunderstorms Jul–Aug; exposed rim trails dangerous during storms"),

    # Joshua Tree
    ("Joshua Tree", 6,  9,  "Heat",     "Temperatures exceed 100°F Jun–Sep; most visitors come Oct–May"),
    ("Joshua Tree", 7,  9,  "Flooding", "Monsoon rains Jul–Sep can cause flash floods in washes"),

    # Arches
    ("Arches", 6,  8,  "Heat",     "Extreme heat Jun–Aug; most trails are exposed with no shade; hike at dawn"),
    ("Arches", 7,  9,  "Flooding", "Flash floods possible Jul–Sep in Fiery Furnace and slot canyon areas"),
]

# ─────────────────────────────────────────────
# BUCKET LIST CHALLENGES
# ─────────────────────────────────────────────

CHALLENGES = [
    ("🌋 Mighty Five",          "Visit all 5 Utah Gigaplex national parks",         ["Zion", "Bryce Canyon", "Capitol Reef", "Canyonlands", "Arches"],            None, None, 1),
    ("🌊 Pacific Coast",        "Visit 5 parks along the Pacific Coast",            None,                                                                          ["CA", "OR", "WA", "AK", "HI"], 5,   2),
    ("🏔️ Crown of the Continent","Explore the Northern Rockies parks",              ["Glacier", "Grand Teton", "Yellowstone"],                                     None, None, 3),
    ("🌵 Desert Wanderer",       "Visit 4 iconic desert parks",                     ["Joshua Tree", "Death Valley", "Saguaro", "Arches"],                          None, None, 4),
    ("🌲 Old Growth",           "Visit 3 parks known for ancient trees",            ["Redwood", "Olympic", "Sequoia"],                                             None, None, 5),
    ("🦅 Grand Loop",           "Complete the classic Grand Circle road trip",      ["Grand Canyon", "Zion", "Bryce Canyon", "Capitol Reef", "Canyonlands", "Arches", "Mesa Verde"], None, None, 6),
    ("🗺️ Coast to Coast",       "Visit a park on both the East and West coasts",   None,                                                                          None, None, 7),
    ("🏕️ Trailblazer",          "Visit 10 different national parks",               None,                                                                          None, 10,  8),
    ("🌎 Park Explorer",        "Visit 25 different national parks",               None,                                                                          None, 25,  9),
    ("🏆 Century Club",         "Visit 50 different national parks",               None,                                                                          None, 50,  10),
    ("🌺 Island Hopper",        "Visit parks in Hawaii or US territories",         None,                                                                          ["HI"], 1,  11),
    ("🐻 Wildlife Watcher",     "Visit 5 parks famous for wildlife",               ["Yellowstone", "Everglades", "Denali", "Olympic", "Great Smoky Mountains"],   None, None, 12),
    ("🏜️ Southwest Explorer",   "Visit 6 parks in the American Southwest",        None,                                                                          ["AZ", "UT", "NM", "NV"], 6,      13),
    ("🌿 East Coast Trail",     "Visit 4 parks along the Appalachians",           ["Shenandoah", "Great Smoky Mountains", "Acadia", "New River Gorge"],           None, None, 14),
    ("❄️ Winter Warrior",       "Visit any 3 parks during winter (Dec–Feb)",       None,                                                                          None, 3,   15),
]


def get_park_id(conn, name):
    row = conn.execute(text("SELECT id FROM parks WHERE name ILIKE :n"), {"n": f"%{name}%"}).fetchone()
    return row.id if row else None


def seed():
    with engine.begin() as conn:
        # ── Crowd Calendar ──────────────────────────────────────────────────
        print("Seeding crowd calendar...")
        inserted = 0
        for park_name, months in CROWD_DATA.items():
            park_id = get_park_id(conn, park_name)
            if not park_id:
                print(f"  ⚠ Park not found: {park_name}")
                continue
            for month, (avg, level, notes) in months.items():
                conn.execute(text("""
                    INSERT INTO park_crowd_calendar (park_id, month, avg_visitors, crowd_level, notes)
                    VALUES (:pid, :m, :avg, :lvl, :notes)
                    ON CONFLICT (park_id, month) DO UPDATE
                      SET avg_visitors=EXCLUDED.avg_visitors,
                          crowd_level=EXCLUDED.crowd_level,
                          notes=EXCLUDED.notes
                """), {"pid": park_id, "m": month, "avg": avg, "lvl": level, "notes": notes})
                inserted += 1
        print(f"  ✓ {inserted} crowd calendar rows")

        # ── Seasonal Warnings ───────────────────────────────────────────────
        print("Seeding seasonal warnings...")
        # Clear existing seeded warnings to avoid duplication on re-run
        conn.execute(text("DELETE FROM park_seasonal_warnings"))
        inserted = 0
        for park_name, m_start, m_end, w_type, desc in SEASONAL_WARNINGS:
            park_id = get_park_id(conn, park_name)
            if not park_id:
                print(f"  ⚠ Park not found: {park_name}")
                continue
            conn.execute(text("""
                INSERT INTO park_seasonal_warnings (park_id, month_start, month_end, warning_type, description)
                VALUES (:pid, :ms, :me, :wt, :desc)
            """), {"pid": park_id, "ms": m_start, "me": m_end, "wt": w_type, "desc": desc})
            inserted += 1
        print(f"  ✓ {inserted} seasonal warnings")

        # ── Challenges ──────────────────────────────────────────────────────
        print("Seeding challenges...")
        conn.execute(text("DELETE FROM user_challenge_progress"))
        conn.execute(text("DELETE FROM challenges"))
        inserted = 0
        for name, desc, req_parks, req_states, req_count, sort in CHALLENGES:
            park_ids = None
            if req_parks:
                park_ids = []
                for pname in req_parks:
                    pid = get_park_id(conn, pname)
                    if pid:
                        park_ids.append(pid)
                    else:
                        print(f"  ⚠ Challenge park not found: {pname}")

            icon = name.split()[0]  # first emoji
            clean_name = name[len(icon):].strip()

            conn.execute(text("""
                INSERT INTO challenges (name, description, icon, required_park_ids, required_states, required_count, sort_order)
                VALUES (:name, :desc, :icon, :pids, :states, :count, :sort)
            """), {
                "name": clean_name,
                "desc": desc,
                "icon": icon,
                "pids": park_ids if park_ids else None,
                "states": req_states,
                "count": req_count,
                "sort": sort,
            })
            inserted += 1
        print(f"  ✓ {inserted} challenges")

    print("\n✅ Phase 4 seed complete.")


if __name__ == "__main__":
    seed()
