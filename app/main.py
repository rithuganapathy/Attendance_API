"""
Employee Attendance & Analytics API (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)

Layout of this file: config -> time & rule helpers -> models -> validation helpers ->
indexes -> aggregation pipeline builders -> endpoints.
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

Date = date  # alias: a path parameter below is named "date" and would shadow the class

load_dotenv()  # override=False by default, so real environment variables take priority
log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# tz_aware=True: PyMongo hands back timezone-aware UTC datetimes, so we never mix naive and aware values.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NON_PRESENCE_STATUSES = ["ABSENT", "LEAVE"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_THRESHOLD = Decimal("4.50")  # R5
MAX_SHIFT_SECONDS = 24 * 3600
MAX_TREND_DAYS = 92

MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

app_ready = {"indexes": False}


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:  # defensive: treat naive values as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def ist_at(day: date, hhmm: str) -> datetime:
    return datetime(day.year, day.month, day.day, int(hhmm[:2]), int(hhmm[3:5]), tzinfo=IST)


def round_half_up(value, places: int) -> float:
    """Half-up rounding on the decimal representation (Python's round() and Mongo's $round are half-even)."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Business rules R1 - R5
# --------------------------------------------------------------------------- #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; early-morning punch-ins on an overnight shift belong to yesterday."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.strftime("%H:%M:%S") < shift_end + ":00":
        day -= timedelta(days=1)
    return day


def calc_late_minutes(punch_in: datetime, att_date: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    seconds = int((punch_in - ist_at(att_date, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def calc_overtime_minutes(punch_out: datetime, att_date: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift end, counted only if >= 30. Overnight shifts end on the next day."""
    end_day = att_date + timedelta(days=1) if is_overnight(shift_start, shift_end) else att_date
    minutes = int((punch_out - ist_at(end_day, shift_end)).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def calc_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up (integer seconds, so the division is exact in Decimal)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def derive_fields(emp: dict, att_date: date, status: str, punch_in: Optional[datetime], punch_out: Optional[datetime]) -> dict:
    """The four API-computed fields for a record, following the table in DATA_MODEL.md."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES or punch_in is None:
        return out
    out["late_minutes"] = calc_late_minutes(punch_in, att_date, emp["shift_start"])
    if punch_out is not None:
        out["work_hours"] = calc_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = calc_overtime_minutes(punch_out, att_date, emp["shift_start"], emp["shift_end"])
        out["half_day"] = Decimal(str(out["work_hours"])) < HALF_DAY_THRESHOLD
    return out


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]  # rejects floats, strings, bools, seconds
HHMM = Annotated[str, Field(pattern=HHMM_PATTERN)]


def _real_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("not a valid calendar date (YYYY-MM-DD)")


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str = Field(pattern=DATE_PATTERN)

    @field_validator("name", "department")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        _real_date(v)
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _consistent(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        if self.status in NON_PRESENCE_STATUSES and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("ABSENT / LEAVE clear the punch times; do not supply punch_in or punch_out with them")
        return self


# --------------------------------------------------------------------------- #
# Serialisation and validation helpers
# --------------------------------------------------------------------------- #
def unprocessable(loc: list, msg: str, value=None):
    """Raise a 422 with FastAPI's standard validation-error body."""
    raise RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


def parse_date(value: Optional[str], loc: list) -> Optional[date]:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date (YYYY-MM-DD)", value)


def month_bounds(month: str) -> tuple[date, date]:
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        unprocessable(["query", "month"], "year out of range", month)
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def count_working_days(first: date, last: date) -> int:
    """Mon-Fri days in [first, last] (R7). No data is looped over, just calendar arithmetic."""
    if last < first:
        return 0
    n = (last - first).days + 1
    weeks, extra = divmod(n, 7)
    count = weeks * 5
    for i in range(extra):
        if (first + timedelta(days=weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def employee_out(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["created_at"] = dt_to_ms(out.get("created_at"))
    return out


def _history_value(v):
    return dt_to_ms(v) if isinstance(v, datetime) else v


def attendance_out(doc: dict) -> dict:
    """Stored document -> API record. Applies the defaults for legacy documents and converts instants to ms."""
    history = []
    for h in doc.get("history") or []:
        history.append({
            "at": dt_to_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {k: {"from": _history_value(c.get("from")), "to": _history_value(c.get("to"))}
                        for k, c in (h.get("changes") or {}).items()},
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": history,
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    # employees
    db.employees.create_index([("emp_code", 1)], unique=True, name="emp_code_unique")  # identity + 409 on duplicates
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="dept_emp_code")  # filtered, sorted list
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="dept_joined_on")  # headcount (R9), trend
    db.employees.create_index([("joined_on", 1)], name="joined_on")  # department summary without a filter
    # attendance_logs
    db.attendance_logs.create_index(
    [("emp_code", 1), ("date", -1)],
    name="attendance_emp_date_list",
)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="emp_date_unique")  # one record per day; per-employee queries
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_desc_emp")  # list sort order, month-wide analytics
    app_ready["indexes"] = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except Exception:  # the app still starts; /health reports 503 until MongoDB answers
        log.exception("could not create indexes at startup; will retry from /health")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_request: Request, exc: PyMongoError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# system / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")

    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)  # the unique index decides; no check-then-insert race
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q) if q else db.employees.estimated_document_count()
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive_fields(emp, att_date, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes exactly one request win
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    out_dt = ms_to_dt(body.punched_at) if body.punched_at is not None else now_seconds()
    candidates = {"emp_code": body.emp_code, "date": {"$lte": out_dt.astimezone(IST).date().isoformat()},
                  "punch_in": {"$lte": out_dt}}  # presence records only: ABSENT / LEAVE have no punch_in
    # Select the most recent OPEN record (punch_out still null).
    rec = db.attendance_logs.find_one({**candidates, "punch_out": None}, sort=[("date", -1), ("punch_in", -1)])
    if rec is None:
        # Nothing open. A closed record means this is a repeated punch-out (409); no record at all is a 404.
        if db.attendance_logs.find_one(candidates, {"_id": 1}) is not None:
            raise HTTPException(409, "already punched out")
        raise HTTPException(404, "no punch-in found")
    punched_in = rec["punch_in"]
    if out_dt <= punched_in:
        unprocessable(["body", "punched_at"], "punched_at must be after the punch-in", body.punched_at)
    if (out_dt - punched_in).total_seconds() > MAX_SHIFT_SECONDS:
        unprocessable(["body", "punched_at"], "punched_at is more than 24 hours after the punch-in", body.punched_at)

    fields = derive_fields(emp, date.fromisoformat(rec["date"]), rec["status"], punched_in, out_dt)
    updated = db.attendance_logs.find_one_and_update(
        # Only closes the record if it is still open and unchanged. Two parallel punch-outs both select the open record,
        # but MongoDB applies this update to only one of them; the loser matches nothing and gets the 409 below.
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None, "punch_in": punched_in},
        {"$set": {"punch_out": out_dt, "work_hours": fields["work_hours"],
                  "overtime_minutes": fields["overtime_minutes"], "half_day": fields["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def attendance_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]) -> dict:
    d_from = parse_date(date_from, ["query", "date_from"])
    d_to = parse_date(date_to, ["query", "date_to"])
    if d_from and d_to and d_from > d_to:
        unprocessable(["query", "date_from"], "date_from must not be after date_to", date_from)
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = date_from
        if d_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    att_date = parse_date(date, ["path", "date"])
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status = rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    if new_status in NON_PRESENCE_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable(["body"], "ABSENT / LEAVE records cannot carry punch times", None)
        new_in = new_out = None  # these statuses clear both punch times
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            unprocessable(["body", "punch_in"], "a presence status requires a punch_in", None)
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != att_date:
            unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date", body.punch_in)
        if new_out is not None:
            gap = (new_out - new_in).total_seconds()
            if gap <= 0 or gap > MAX_SHIFT_SECONDS:
                unprocessable(["body", "punch_out"], "punch_out must be after punch_in and within 24 hours", body.punch_out)

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        unprocessable(["body"], "this request changes nothing", None)

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive_fields(emp, att_date, new_status, new_in, new_out)}
    old_defaults = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {}
    for field in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        before = rec.get(field)
        if before is None and field in ("late_minutes", "overtime_minutes", "half_day"):
            before = old_defaults[field]  # legacy documents may omit these; missing means 0 / false
        if before != new[field]:
            changes[field] = {"from": before, "to": new[field]}
    entry = {"at": datetime.now(timezone.utc), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only applies if nothing we based our decision on has moved. The history
    # length acts as a version counter (every successful PATCH appends exactly one entry), so a concurrent PATCH
    # makes this one match nothing and it gets a 409 instead of silently overwriting the other's entry.
    cond = {"emp_code": emp_code, "date": rec["date"], "status": old_status, "punch_in": old_in, "punch_out": old_out}
    if "history" in rec and rec["history"] is not None:
        cond["history"] = {"$size": len(rec["history"])}
    else:
        cond["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        cond, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return attendance_out(updated)


# --------------------------------------------------------------------------- #
# Aggregation pipeline builders
# --------------------------------------------------------------------------- #
# Shared per-record expressions. They read the STORED derived values and default missing legacy fields.
_IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
_IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]}  # 1 = Sunday
_LATE = {"$ifNull": ["$late_minutes", 0]}
_PRESENT_VALUE = {"$cond": [_IS_PRESENT, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}  # half day = 0.5
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _stats() -> dict:
    """Accumulators for one group of attendance records (a month of one employee, or one day of a department)."""
    return {
        "present_days": {"$sum": {"$cond": [_IS_WEEKDAY, _PRESENT_VALUE, 0]}},  # R7: Mon-Fri only
        "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},  # weekends included
        "total_late_minutes": {"$sum": _LATE},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        # work hours kept as integer hundredths so the later average is exact (no float drift before rounding)
        "wh_cents": {"$sum": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
        "wh_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
    }


def employee_monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},  # index emp_date_unique
        {"$group": {"_id": None, **_stats()}},
    ]


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    match: dict = {"joined_on": {"$lte": end}}  # R9: joined by month end, with or without logs
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},  # index dept_joined_on / joined_on
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                {"$group": {"_id": None, **_stats()}},
            ],
            "as": "s",
        }},
        # employees with no logs get an empty stats object, so they still count in headcount below
        {"$set": {"s": {"$ifNull": [{"$arrayElemAt": ["$s", 0]}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            **{k: {"$sum": {"$ifNull": ["$s." + k, 0]}}
               for k in ("present_days", "late_count", "total_late_minutes", "leave_count", "on_duty_count", "wh_cents", "wh_n")},
        }},
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},  # index date_desc_emp
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank is standard competition ranking: 1, 2, 2, 4. Limit is applied on the rank, never on row position.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def department_trend_pipeline(department: str, d_from: date, d_to: date) -> list:
    """
    One row per calendar day in [d_from, d_to], built entirely inside MongoDB.

    The pipeline starts from `employees` filtered by department, so no employee list is ever loaded into Python.
    $facet always emits exactly one document, even when the department has no logs in the range, and the
    day rows are then generated with $range. That is why days without records are never lost.
    """
    n_days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=timezone.utc)
    per_day = {"$arrayElemAt": [{"$filter": {"input": "$daily", "cond": {"$eq": ["$$this._id", "$$date"]}}}, 0]}
    headcount = {"$reduce": {  # R9: department employees that had joined on or before that day
        "input": "$joined", "initialValue": 0,
        "in": {"$cond": [{"$lte": ["$$this._id", "$$date"]}, {"$add": ["$$value", "$$this.n"]}, "$$value"]},
    }}
    row = {"$let": {"vars": {"stat": per_day, "hc": headcount}, "in": {
        "date": "$$date",
        "is_working_day": "$$wd",
        "headcount": "$$hc",
        "present_count": {"$ifNull": ["$$stat.present_count", 0]},
        "late_count": {"$ifNull": ["$$stat.late_count", 0]},
        # only working days with headcount > 0 get a rate; a working day with no records is 0.0
        "rate": {"$cond": [{"$and": ["$$wd", {"$gt": ["$$hc", 0]}]},
                           {"$divide": [{"$ifNull": ["$$stat.present_count", 0]}, "$$hc"]}, None]},
    }}}
    return [
        {"$match": {"department": department}},  # index dept_emp_code
        {"$facet": {
            "daily": [
                {"$lookup": {
                    "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                    "pipeline": [
                        {"$match": {"date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
                        {"$group": {"_id": "$date", "present_count": {"$sum": _PRESENT_VALUE},
                                    "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}}}},
                    ],
                    "as": "d",
                }},
                {"$unwind": "$d"},
                {"$group": {"_id": "$d._id", "present_count": {"$sum": "$d.present_count"}, "late_count": {"$sum": "$d.late_count"}}},
            ],
            "joined": [{"$group": {"_id": "$joined_on", "n": {"$sum": 1}}}],
        }},
        {"$project": {"_id": 0, "rows": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$let": {
                "vars": {"day": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}},
                "in": {"$let": {
                    "vars": {"date": {"$dateToString": {"date": "$$day", "format": "%Y-%m-%d"}},
                             "wd": {"$in": [{"$dayOfWeek": "$$day"}, [2, 3, 4, 5, 6]]}},  # 1 = Sunday
                    "in": row,
                }},
            }},
        }}}},
        {"$unwind": "$rows"},
        {"$replaceRoot": {"newRoot": "$rows"}},
        # $avg skips nulls; the window is the current row and up to 6 before it, never reaching before `from`
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
    ]


def trend_range(d_from_s: str, d_to_s: str) -> tuple[date, date]:
    d_from = parse_date(d_from_s, ["query", "from"])
    d_to = parse_date(d_to_s, ["query", "to"])
    if d_to < d_from:
        unprocessable(["query", "to"], "to must not be before from", d_to_s)
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        unprocessable(["query", "to"], f"range must not exceed {MAX_TREND_DAYS} days", d_to_s)
    return d_from, d_to


def require_department_or_404(department: str) -> None:
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:  # index dept_emp_code; loads nothing
        raise HTTPException(404, "unknown department")


# --------------------------------------------------------------------------- #
# analytics endpoints
# --------------------------------------------------------------------------- #
def _num(x, default=0):
    return default if x is None else x


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    first, last = month_bounds(month)
    emp = get_employee_or_404(emp_code)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())))
    s = rows[0] if rows else {}
    joined = Date.fromisoformat(emp["joined_on"])
    working_days = count_working_days(max(first, joined), last)  # R7: counted from joined_on
    present = _num(s.get("present_days"))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present, 2),
        "leave_days": int(_num(s.get("leave_count"))),
        "late_count": int(_num(s.get("late_count"))),
        "total_late_minutes": int(_num(s.get("total_late_minutes"))),
        "total_overtime_minutes": int(_num(s.get("total_overtime_minutes"))),
        "attendance_pct": round_half_up(Decimal(str(present)) / Decimal(working_days) * 100, 2) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(first.isoformat(), last.isoformat(), department)):
        n = int(r["wh_n"])
        avg = Decimal(str(r["wh_cents"])) / Decimal(n) / Decimal(100) if n else None
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(avg, 2) if avg is not None else None,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)))
    return {"month": month, "items": [{**i, "rank": int(i["rank"]), "total_late_minutes": int(i["total_late_minutes"]),
                                       "late_count": int(i["late_count"])} for i in items]}


def _trend_items(rows: list) -> list:
    return [{
        "date": r["date"],
        "is_working_day": bool(r["is_working_day"]),
        "headcount": int(r["headcount"]),
        "present_count": round_half_up(r["present_count"], 2),
        "late_count": int(r["late_count"]),
        "attendance_rate": round_half_up(r["rate"], 4) if r.get("rate") is not None else None,
        "moving_avg_7d": round_half_up(r["moving"], 4) if r.get("moving") is not None else None,
    } for r in rows]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from", pattern=DATE_PATTERN),
                     to: str = Query(..., pattern=DATE_PATTERN)):
    d_from, d_to = trend_range(from_, to)
    require_department_or_404(department)
    rows = list(db.employees.aggregate(department_trend_pipeline(department, d_from, d_to)))
    return {"department": department, "items": _trend_items(rows)}


# --------------------------------------------------------------------------- #
# admin: explain
# --------------------------------------------------------------------------- #
def _explain(command: dict) -> dict:
    out = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(out, json_options=json_util.RELAXED_JSON_OPTIONS))


def _require(**params):
    for name, value in params.items():
        if value is None:
            unprocessable(["query", name], "field required", None)


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_PATTERN),
    date_to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_PATTERN),
    to: Optional[str] = Query(None, pattern=DATE_PATTERN),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = attendance_query(emp_code, date_from, date_to, status)
        command = {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT), "projection": {"_id": 0},
                   "skip": (page - 1) * page_size, "limit": page_size}
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "attendance_logs", "cursor": {},
                   "pipeline": employee_monthly_pipeline(emp_code, first.isoformat(), last.isoformat())}
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        _require(month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "employees", "cursor": {},
                   "pipeline": department_summary_pipeline(first.isoformat(), last.isoformat(), department)}
        collection = "employees"
    elif endpoint == "late_leaderboard":
        _require(month=month)
        first, last = month_bounds(month)
        command = {"aggregate": "attendance_logs", "cursor": {},
                   "pipeline": late_leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)}
        collection = "attendance_logs"
    else:  # department_trend
        _require(department=department, **{"from": from_, "to": to})
        d_from, d_to = trend_range(from_, to)
        require_department_or_404(department)
        command = {"aggregate": "employees", "cursor": {},
                   "pipeline": department_trend_pipeline(department, d_from, d_to)}
        collection = "employees"
    return {"endpoint": endpoint, "collection": collection, "explain": _explain(command)}
