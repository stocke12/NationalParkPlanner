# National Park Planner

A full-stack trip-planning application for U.S. National Parks — sync live park data, get AI-generated itineraries, track your gear, and plan trips with friends.

<img width="1885" height="806" alt="image" src="https://github.com/user-attachments/assets/b02950e6-a825-4f37-9382-fcb85c1c9b2c" />

## What it does

National Park Planner pulls live data directly from the National Park Service (NPS) API — current safety alerts, park info, and more — and layers a planning experience on top of it: AI-generated day-by-day itineraries tailored to your travel style and trip length, a social system for planning trips with friends, a visitation "passport" map to track parks you've visited, and gear tracking for trip prep.

## Features

- **Live NPS data sync** — park details and active safety alerts pulled directly from the NPS API, not static/hardcoded data
- **AI-generated itineraries** — day-by-day trip plans generated based on user travel style, active alerts, and trip duration
- **Social / collaboration system** — friend requests and shared trip planning between users
- **Passport map** — visual tracker of which national parks a user has visited
- **Gear tracking** — manage packing lists and gear for upcoming trips
- **User authentication** — account creation and login

## Tech stack

- **App:** Streamlit (Python)
- **Database:** PostgreSQL (accessed via SQLAlchemy)
- **External data:** National Park Service (NPS) API
- **AI:** Google Gemini API for itinerary generation
- **Automation:** GitHub Actions — hourly scheduled job (`etl_pipeline.py`) keeps park data and alerts in sync
- **Other:** bcrypt (auth), fpdf2 (PDF export of itineraries)

## Architecture notes

This project started as a CLI prototype (`main.py`) and was rebuilt as a Streamlit app (`app.py`) backed by PostgreSQL instead of flat/session state, so it could support real user accounts and persistent relational data (friends, trips, gear lists). Data freshness is handled outside the app itself: a GitHub Actions workflow runs `etl_pipeline.py` on an hourly cron schedule to pull the latest NPS data and alerts into the database, so the app is always reading current data rather than making live API calls on every page load.

## What's next

- [ ] Rebuild the frontend in React with a FastAPI backend
- [ ] Add trip cost estimation
- [ ] Expand the passport map with visit dates and photos
- [ ] Add park-specific gear recommendations based on season/terrain

## Author

Stockton Erickson — [LinkedIn](https://www.linkedin.com/in/stockton-erickson/)
