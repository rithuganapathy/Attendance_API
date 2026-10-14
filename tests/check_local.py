"""
Local self-check. Seeds awkward data into MONGO_DB (DROPS employees + attendance_logs there, use a scratch DB!),
then compares the running API (http://localhost:8000) with an independent plain-Python calculation.

    MONGO_DB=attendance_check python tests/check_local.py        # start the API with the same MONGO_DB first
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import httpx
from pymongo import MongoClient

URI, DBN, API = os.getenv("MONGO_URI", "mongodb://localhost:27017"), os.getenv("MONGO_DB", "attendance_check"), "http://localhost:8000"
db = MongoClient(URI, tz_aware=True)[DBN]
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT = {"PRESENT", "WFH", "ON_DUTY"}


def rh(x, p):
    return float(Decimal(str(x)).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP))


def emp(code, dept, joined="2026-01-05", s="09:30", e="18:30"):
    return {"emp_code": code, "name": code, "email": f"{code}@x.co", "department": dept, "shift_start": s, "shift_end": e,
            "joined_on": joined, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}


def log(code, d, status="PRESENT", late=0, ot=0, hours=9.0, half=False, legacy=False):
    day = date.fromisoformat(d)
    pin = datetime(day.year, day.month, day.day, 9, 30, tzinfo=IST).astimezone(timezone.utc) if status in PRESENT else None
    doc = {"emp_code": code, "date": d, "status": status, "punch_in": pin,
           "punch_out": pin + timedelta(hours=hours) if pin else None, "work_hours": hours if pin else None,
           "late_minutes": late, "overtime_minutes": ot, "half_day": half, "history": []}
    if legacy:
        del doc["half_day"], doc["history"]
    return doc


EMPS = [emp("EMP0001", "Eng"), emp("EMP0002", "Eng"), emp("EMP0003", "Eng", joined="2026-07-15"),
        emp("EMP0004", "Ops"), emp("EMP0005", "Ops"), emp("EMP0006", "Ops", s="22:00", e="06:00"),
        emp("EMP0007", "HR", joined="2026-08-01")]
LOGS = [
    log("EMP0001", "2026-07-01"), log("EMP0001", "2026-07-02", late=35, ot=40), log("EMP0001", "2026-07-03", hours=3.5, half=True, status="WFH"),
    log("EMP0001", "2026-07-04", ot=45, hours=8.345),                                    # Saturday: counts for overtime, not present_days
    log("EMP0001", "2026-07-06", "LEAVE"), log("EMP0001", "2026-07-07", "ABSENT"), log("EMP0001", "2026-07-08", "ON_DUTY", legacy=True),
    log("EMP0003", "2026-07-15"), log("EMP0003", "2026-07-16", late=12), log("EMP0003", "2026-07-17", hours=4.49, half=True),
    log("EMP0004", "2026-07-01", late=20), log("EMP0004", "2026-07-02", late=30), log("EMP0005", "2026-07-01", late=50),
    log("EMP0006", "2026-07-01", late=25), log("EMP0006", "2026-07-02", late=25),
    log("EMP0099", "2026-07-01", late=500),                                               # unknown employee: ignored
]


def ref_stats(rows):
    s = dict(present=0.0, late_count=0, late=0, ot=0, leave=0, on_duty=0, hours=[])
    for r in rows:
        wk = date.fromisoformat(r["date"]).weekday() < 5
        if r["status"] in PRESENT:
            if wk:
                s["present"] += 0.5 if r.get("half_day") else 1
            if r.get("work_hours") is not None:
                s["hours"].append(Decimal(str(r["work_hours"])))
        s["late_count"] += (r.get("late_minutes") or 0) > 0
        s["late"] += r.get("late_minutes") or 0
        s["ot"] += r.get("overtime_minutes") or 0
        s["leave"] += r["status"] == "LEAVE"
        s["on_duty"] += r["status"] == "ON_DUTY"
    return s


def month_range(m):
    y, mo = int(m[:4]), int(m[5:])
    first = date(y, mo, 1)
    last = (date(y + (mo == 12), mo % 12 + 1, 1)) - timedelta(days=1)
    return first, last


def wdays(a, b):
    return sum(1 for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5)


def expected_monthly(code, m):
    first, last = month_range(m)
    e = db.employees.find_one({"emp_code": code})
    rows = list(db.attendance_logs.find({"emp_code": code, "date": {"$gte": str(first), "$lte": str(last)}}))
    s = ref_stats(rows)
    wd = wdays(max(first, date.fromisoformat(e["joined_on"])), last)
    return {"emp_code": code, "month": m, "working_days": wd, "present_days": rh(s["present"], 2), "leave_days": s["leave"],
            "late_count": s["late_count"], "total_late_minutes": s["late"], "total_overtime_minutes": s["ot"],
            "attendance_pct": rh(Decimal(str(s["present"])) / wd * 100, 2) if wd else None}


def expected_summary(m):
    first, last = month_range(m)
    out = {}
    for e in db.employees.find({"joined_on": {"$lte": str(last)}}):
        rows = list(db.attendance_logs.find({"emp_code": e["emp_code"], "date": {"$gte": str(first), "$lte": str(last)}}))
        s, d = ref_stats(rows), out.setdefault(e["department"], dict(headcount=0, present=0.0, lc=0, late=0, leave=0, od=0, hours=[]))
        d["headcount"] += 1; d["present"] += s["present"]; d["lc"] += s["late_count"]; d["late"] += s["late"]
        d["leave"] += s["leave"]; d["od"] += s["on_duty"]; d["hours"] += s["hours"]
    return [{"department": k, "headcount": v["headcount"], "present_days": rh(v["present"], 2),
             "avg_work_hours": rh(sum(v["hours"]) / len(v["hours"]), 2) if v["hours"] else None, "late_count": v["lc"],
             "total_late_minutes": v["late"], "leave_count": v["leave"], "on_duty_count": v["od"]} for k, v in sorted(out.items())]


def expected_board(m, limit, dept=None):
    first, last = month_range(m)
    tot = {}
    for r in db.attendance_logs.find({"date": {"$gte": str(first), "$lte": str(last)}, "late_minutes": {"$gt": 0}}):
        t = tot.setdefault(r["emp_code"], [0, 0]); t[0] += r["late_minutes"]; t[1] += 1
    rows = []
    for code, (mins, n) in tot.items():
        e = db.employees.find_one({"emp_code": code})
        if e and (dept is None or e["department"] == dept):
            rows.append((-mins, code, e["name"], e["department"], mins, n))
    rows.sort()
    res, rank = [], 0
    for i, r in enumerate(rows):
        rank = i + 1 if i == 0 or r[0] != rows[i - 1][0] else rank
        if rank <= limit:
            res.append({"rank": rank, "emp_code": r[1], "name": r[2], "department": r[3], "total_late_minutes": r[4], "late_count": r[5]})
    return res


def expected_trend(dept, a, b):
    a, b = date.fromisoformat(a), date.fromisoformat(b)
    emps = list(db.employees.find({"department": dept}))
    rows, rates = [], []
    for i in range((b - a).days + 1):
        d = a + timedelta(days=i)
        recs = list(db.attendance_logs.find({"emp_code": {"$in": [e["emp_code"] for e in emps]}, "date": str(d)}))
        hc = sum(e["joined_on"] <= str(d) for e in emps)
        pc = sum((0.5 if r.get("half_day") else 1) for r in recs if r["status"] in PRESENT)
        wd = d.weekday() < 5
        rate = pc / hc if wd and hc > 0 else None
        rates.append(rate)
        win = [x for x in rates[max(0, i - 6): i + 1] if x is not None]
        rows.append({"date": str(d), "is_working_day": wd, "headcount": hc, "present_count": rh(pc, 2),
                     "late_count": sum((r.get("late_minutes") or 0) > 0 for r in recs),
                     "attendance_rate": rh(rate, 4) if rate is not None else None,
                     "moving_avg_7d": rh(sum(win) / len(win), 4) if win else None})
    return rows


def check(name, got, exp):
    print(("PASS " if got == exp else "FAIL ") + name)
    if got != exp:
        print("   got:", got, "\n   exp:", exp)


if __name__ == "__main__":
    # Safety guard: this script DROPS employees and attendance_logs. Only run it on a scratch database.
    if not any(w in DBN.lower() for w in ("check", "test", "scratch")) and os.getenv("CHECK_ALLOW_DROP") != "1":
        raise SystemExit(f"Refusing to run: MONGO_DB='{DBN}' does not look like a scratch database.\n"
                         "Use e.g. MONGO_DB=attendance_check (and start the API with the same MONGO_DB).")
    db.employees.drop(); db.attendance_logs.drop()
    db.employees.insert_many(EMPS); db.attendance_logs.insert_many(LOGS)
    c = httpx.Client(base_url=API, timeout=30)
    c.get("/health")  # health retries index creation if the server started before the collections existed
    M = "2026-07"
    for code in ("EMP0001", "EMP0002", "EMP0003", "EMP0007"):
        check(f"monthly {code}", c.get(f"/analytics/employees/{code}/monthly", params={"month": M}).json(), expected_monthly(code, M))
    check("summary", c.get("/analytics/departments/summary", params={"month": M}).json()["items"], expected_summary(M))
    for lim, dep in ((10, None), (1, None), (2, None), (10, "Ops")):
        p = {"month": M, "limit": lim, **({"department": dep} if dep else {})}
        check(f"leaderboard limit={lim} dept={dep}", c.get("/analytics/leaderboard/late", params=p).json()["items"], expected_board(M, lim, dep))
    for dep, a, b in (("Eng", "2026-07-01", "2026-07-31"), ("Ops", "2026-06-28", "2026-07-10"), ("HR", "2026-07-25", "2026-08-05")):
        check(f"trend {dep} {a}..{b}", c.get(f"/analytics/departments/{dep}/trend", params={"from": a, "to": b}).json()["items"], expected_trend(dep, a, b))
    for ep, p in (("attendance_list", {"emp_code": "EMP0001"}), ("attendance_list", {"status": "ABSENT"}), ("employee_monthly", {"emp_code": "EMP0001", "month": M}),
                  ("department_summary", {"month": M}), ("late_leaderboard", {"month": M}),
                  ("department_trend", {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"})):
        txt = c.get(f"/admin/explain/{ep}", params=p).text
        print(("PASS " if "IXSCAN" in txt and "COLLSCAN" not in txt else "FAIL ") + f"explain {ep} {p}")
