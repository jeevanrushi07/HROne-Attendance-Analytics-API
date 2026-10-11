# Employee Attendance & Analytics API

A REST API for employee management, attendance tracking, attendance regularization, and workforce analytics. Built with Python, FastAPI, and MongoDB.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer
- pip

## Setup

Clone the repository and navigate to its root directory.

Create and activate a virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Create a local `.env` file in the project root:

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Replace the URI with your MongoDB Atlas connection string if using Atlas. Never commit `.env` or expose database credentials.

## Run the API

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

Interactive API documentation:

- Swagger UI: http://127.0.0.1:8000/docs
- Health check: http://127.0.0.1:8000/health

## API Endpoints

### Employee management
- `POST /employees` — Create an employee.
- `GET /employees` — List employees with optional department filtering and pagination.

### Attendance
- `POST /attendance/punch-in` — Record an employee's punch-in.
- `POST /attendance/punch-out` — Record a punch-out and calculate derived attendance fields.
- `GET /attendance` — List attendance records with filters and pagination.
- `PATCH /attendance/{emp_code}/{date}` — Regularize an attendance record with an audit history.

### Analytics
- `GET /analytics/employees/{emp_code}/monthly` — Monthly employee attendance statistics.
- `GET /analytics/departments/summary` — Department-level attendance summary.
- `GET /analytics/leaderboard/late` — Rank employees by late minutes.
- `GET /analytics/departments/{department}/trend` — Daily department attendance trend and moving average.

### Operations
- `GET /health` — Check MongoDB connectivity.
- `GET /admin/explain/{endpoint}` — Inspect query execution statistics for supported operations.

## Example Requests

Create an employee:

```json
{
  "emp_code": "EMP1001",
  "name": "Rahul Kumar",
  "email": "rahul@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-15"
}
```

Punch in:

```json
{
  "emp_code": "EMP1001",
  "status": "PRESENT"
}
```

For monthly analytics, supply a month in `YYYY-MM` format. Attendance timestamps accepted by the API use integer epoch milliseconds where applicable.

## Implementation Notes

- MongoDB indexes support employee lookup, attendance uniqueness, filtering, and sorting.
- Attendance records use `(emp_code, date)` as their natural key.
- Analytics use MongoDB aggregation pipelines.
- Timestamps are returned as epoch milliseconds.
- Attendance calculations use the Asia/Kolkata timezone.
- Regularization records changes in an audit history.

## Testing and Limitations

Verify all endpoints against the assignment's OpenAPI contract and data model. Include tests for invalid input, duplicate requests, concurrent operations, overnight shifts, attendance corrections, ranking ties, empty trend dates, and MongoDB execution statistics.

Manual Swagger testing has been performed during development. Full automated integration testing against the hiring team's hidden dataset has not been verified.

See `REVIEW.md` for code review findings and `DECISIONS.md` for design rationale.