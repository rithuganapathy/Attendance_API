# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`. Needs Python 3.11+ and MongoDB 6.0+ (the analytics use `$setWindowFields`, `$rank`, `$facet`).

## Run (Windows PowerShell)

```powershell
docker run -d --name attendance-mongo -p 27017:27017 mongo:7     # or use a MongoDB Atlas URI
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # if blocked: Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
pip install -r requirements.txt
Copy-Item .env.example .env      # MONGO_URI / MONGO_DB; real environment variables win over .env
uvicorn app.main:app --port 8000
```

Indexes are created at startup. Check it is up: `Invoke-RestMethod http://localhost:8000/health`

macOS / Linux: `source .venv/bin/activate`, `cp .env.example .env`, the rest is the same.

## Self-check (optional)

`tests/check_local.py` compares the analytics with a plain-Python calculation on awkward data (ties, weekend records, a mid-month joiner, an employee with no logs, an overnight shift). It **drops and reseeds** `employees` and `attendance_logs`, so use a disposable database; it refuses to run unless `MONGO_DB` contains `check`, `test` or `scratch`.

```powershell
$env:MONGO_DB = "attendance_check"
uvicorn app.main:app --port 8000          # terminal 1
python tests/check_local.py               # terminal 2 (set $env:MONGO_DB there too)
```

## Notes

- `moving_avg_7d` averages the unrounded daily rates and rounds once at the end.
- Weekend records count towards late minutes, overtime and average work hours, but not `present_days`.
- Punch-out closes the most recent open record; a repeated punch-out returns 409, and two simultaneous punch-outs yield one 200.
