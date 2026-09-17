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

- **Frontend:** React
- **Backend:** FastAPI (Python)
- **Database:** PostgreSQL
- **External data:** National Park Service (NPS) API
- **AI:** LLM-generated itinerary planning

## Architecture notes

This project originally started as a Streamlit prototype and was rebuilt into a production-style architecture: a React frontend talking to a FastAPI backend, backed by PostgreSQL instead of flat/session state. The rebuild was driven by the need for real user accounts, persistent relational data (friends, trips, gear lists), and a UI flexible enough to support a social feature set — none of which Streamlit was designed to handle well at that scale.

## What's next

- [ ] Add trip cost estimation
- [ ] Expand the passport map with visit dates and photos
- [ ] Add park-specific gear recommendations based on season/terrain

## Author

Stockton Erickson — [LinkedIn](https://www.linkedin.com/in/stockton-erickson/)
