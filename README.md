# Employee Attendance & Analytics API

A FastAPI and MongoDB application for employee management, attendance tracking, attendance corrections, and analytics.

## Features

- Employee creation and paginated listing
- Attendance punch-in and punch-out
- Attendance regularization with change history
- Monthly employee and department analytics
- Late-arrival leaderboard and department trends
- MongoDB indexes and query-plan explain endpoints

## Requirements

- Python 3.10+
- MongoDB 6.0+

## Setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "employee_attendance"
uvicorn app.main:app --port 8001
```

Open `http://127.0.0.1:8001/docs` to explore the API.

The application reads `MONGO_URI` and `MONGO_DB` from environment variables.

## Documentation

- `REVIEW.md` — defects identified in the starter and fixes
- `DECISIONS.md` — implementation decisions and trade-offs
