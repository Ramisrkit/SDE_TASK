"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from __future__ import annotations

import os
import re
import json
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util
from bson.decimal128 import Decimal128

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = (*PRESENCE,   "ABSENT", "LEAVE")
MIN_EPOCH_MS, MAX_EPOCH_MS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

# ------------------------------- Models ------------------------------------
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

def epoch_field():
    # Omitted -> None (default is not validated); an explicit null or an out-of-range value -> 422.
    return Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: StrictInt = epoch_field()
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: StrictInt = epoch_field()

class RegularizeRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] = None
    punch_in: StrictInt = epoch_field()
    punch_out: StrictInt = epoch_field()

# ------------------------------- Helpers ----------------------------------
def fail422(message: str):
    raise HTTPException(status_code=422, detail=message)

def parse_date(value: str, field: str = "date") -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        fail422(f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail422(f"{field} must be a valid date")

def parse_month(value: str) -> tuple[date, date]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail422("month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    if year < 1: fail422("month year is out of range")
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)

def parse_shift(value: str, field: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        fail422(f"{field} must be HH:MM")
    return tuple(map(int, value.split(":")))

def validate_epoch_ms(value: Any, field: str = "timestamp") -> int:
    # bool is an int subclass; floats and strings are explicitly forbidden.
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        fail422(f"{field} must be an integer epoch-millisecond value in range")
    return value

def to_datetime(epoch_ms: int) -> datetime:
    validate_epoch_ms(epoch_ms)
    return datetime(1970, 1, 1) + timedelta(seconds=epoch_ms // 1000)

def now_utc_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)

def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    # PyMongo datetimes are naive UTC; aware datetimes are normalized as well.
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return (aware - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)

def round_half_up(value: float | Decimal, places: int = 2) -> float:
    q = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))

def ist_datetime(value: datetime) -> datetime:
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value
    return aware.astimezone(IST)

def attendance_date_for(punch_in_utc: datetime, emp: dict) -> str:
    local = ist_datetime(punch_in_utc)
    start = emp["shift_start"]
    end = emp["shift_end"]
    if end <= start and local.strftime("%H:%M") < end:
        local -= timedelta(days=1)
    return local.date().isoformat()

def late_minutes(
    punch_in_utc: datetime,
    shift_start: str,
    attendance_date: Optional[str] = None
) -> int:
    local = ist_datetime(punch_in_utc)
    h, m = parse_shift(shift_start, "shift_start")
    shift_day = parse_date(attendance_date) if attendance_date else local.date()

    shift = datetime.combine(shift_day, time(h, m), tzinfo=IST)
    elapsed_seconds = (local - shift).total_seconds()

    # Late only when strictly more than 10 minutes after shift start.
    if elapsed_seconds <= 600:
        return 0

    # Count whole elapsed minutes from shift start, not after the grace period.
    return int(elapsed_seconds // 60)

def shift_end_utc(date_str: str, shift_start: str, shift_end: str) -> datetime:
    h, m = parse_shift(shift_end, "shift_end")
    local_end = datetime.combine(parse_date(date_str), time(h, m), tzinfo=IST)
    if shift_end <= shift_start:
        local_end += timedelta(days=1)
    return local_end.astimezone(UTC).replace(tzinfo=None)

def derived_values(emp: dict, date_str: str, punch_in: Optional[datetime], punch_out: Optional[datetime], status: str) -> dict:
    if status in ("ABSENT", "LEAVE"):
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if punch_in is None:
        fail422("A presence status requires a punch-in time")
    late = late_minutes(punch_in, emp["shift_start"], date_str)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    seconds = (punch_out - punch_in).total_seconds()
    if seconds <= 0 or seconds > 86400:
        fail422("Punch-out must be after punch-in and within 24 hours")
    hours = round_half_up(Decimal(str(seconds)) / Decimal("3600"), 2)
    after = int((punch_out - shift_end_utc(date_str, emp["shift_start"], emp["shift_end"])).total_seconds() // 60)
    overtime = after if after >= 30 else 0
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": overtime, "half_day": hours < 4.50}

def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")), "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0), "half_day": bool(doc.get("half_day", False)),
        "history": [{"at": to_epoch_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"),
                     "changes": {k: {"from": to_epoch_ms(v.get("from")) if k in ("punch_in", "punch_out") else v.get("from"),
                                     "to": to_epoch_ms(v.get("to")) if k in ("punch_in", "punch_out") else v.get("to")}
                                 for k, v in (h.get("changes") or {}).items()}}
                    for h in (doc.get("history") or [])]
    }

def serialize_employee(doc: dict) -> dict:
    return {"emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
            "department": doc["department"], "shift_start": doc["shift_start"], "shift_end": doc["shift_end"],
            "joined_on": doc["joined_on"], "created_at": to_epoch_ms(doc["created_at"])}

def month_log_match(month: str, emp_code: Optional[str] = None) -> dict:
    first, last = parse_month(month)
    q = {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}
    if emp_code:
        q["emp_code"] = emp_code
    return q

def day_count_expr(start_expr: Any, end_expr: Any) -> dict:
    """Mongo expression: weekdays from start through end inclusive; never loops in Python."""
    return {"$size": {"$filter": {"input": {"$map": {
        "input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": {"$dateFromString": {"dateString": start_expr}},
                                                            "endDate": {"$dateFromString": {"dateString": end_expr}}, "unit": "day"}}, 1]}]},
        "as": "n", "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start_expr}}, "unit": "day", "amount": "$$n"}}}},
        "as": "d", "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$$d"}, 6]}]}}}}

def bson_safe(doc: Any) -> Any:
    """Explain output can hold Timestamp/Binary/Int64 (e.g. $clusterTime); make it plain JSON."""
    return json.loads(json_util.dumps(doc, json_options=json_util.RELAXED_JSON_OPTIONS))

# Trend stages done inside MongoDB: half-up 4dp rate (Decimal128), then a 7-row moving average.
TREND_TAIL = [
    {"$set": {"attendance_rate": {"$cond": [
        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
        {"$divide": [{"$floor": {"$add": [
            {"$multiply": [{"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 10000]},
            {"$toDecimal": "0.5"}]}}, 10000]},
        None]}}},
    {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
        "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
]

def aggregation_explain(collection: str, pipeline: list[dict]) -> dict:
    try:
        return bson_safe(db.command({"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, "verbosity": "executionStats"}))
    except PyMongoError as exc:
        raise HTTPException(503, f"MongoDB explain failed: {exc.__class__.__name__}")

# --------------------------- Startup / health -------------------------------
@app.on_event("startup")
def create_indexes():
    # Existing duplicate attendance keys must be resolved before the unique index can be created.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="idx_employee_code")
    db.employees.create_index(
    [("department", ASCENDING), ("joined_on", ASCENDING)],
    name="idx_employee_department_joined"
)
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punch_in")

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")

# -------------------------------- Employees ---------------------------------
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if not re.fullmatch(r"EMP\d{4,6}", body.emp_code): 
        fail422("emp_code must match EMP followed by 4-6 digits")
    if not 1 <= len(body.name.strip()) <= 100: 
        fail422("name must contain 1-100 characters")
    if len(body.email) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", body.email): 
        fail422("email is invalid")
    if not 1 <= len(body.department.strip()) <= 50: 
        fail422("department must contain 1-50 characters")
    parse_shift(body.shift_start, "shift_start"); 
    parse_shift(body.shift_end, "shift_end")
    if body.shift_start == body.shift_end: 
        fail422("shift_start and shift_end must differ")
    parse_date(body.joined_on, "joined_on")
    doc = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    doc["created_at"] = now_utc_naive()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    if department is not None:
        query = {"department": department}
    else:
        query = {}
    total = db.employees.count_documents(query)
    docs = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(x) for x in docs], "total": total, "page": page, "page_size": page_size}

# ------------------------------- Attendance --------------------------------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None: 
        raise HTTPException(404, "Employee not found")
    if body.status not in PRESENCE: #PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
        fail422("status must be PRESENT, WFH, or ON_DUTY")
    ts = to_datetime(body.punched_at) if body.punched_at is not None else now_utc_naive()
    att_date = attendance_date_for(ts, emp)
    doc = {"emp_code": body.emp_code, "date": att_date, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None, "late_minutes": late_minutes(ts, emp["shift_start"], att_date),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Attendance record already exists for this employee and date")
    return serialize_attendance(doc)



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")
    ts = to_datetime(body.punched_at) if body.punched_at is not None else now_utc_naive()
    # Most recent record that started at or before the punch-out, open OR closed.
    rec = db.attendance_logs.find_one({"emp_code": body.emp_code,"punch_in": {"$lte": ts},"punch_out": None,},
                                      sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "No punch-in record found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    derived = derived_values(emp, rec["date"], rec["punch_in"], ts, rec["status"])   # 422 if <= punch_in or > 24h
    # Atomic guard: only one of several simultaneous punch-outs can match punch_out == null.
    result = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None},
                                           {"$set": {"punch_out": ts, **derived}})
    if result.modified_count != 1:
        raise HTTPException(409, "Already punched out")
    rec.update({"punch_out": ts, **derived})
    return serialize_attendance(rec)

@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q: dict[str, Any] = {}
    if emp_code is not None: 
        q["emp_code"] = emp_code
    if status is not None:
        if status not in STATUSES: 
            fail422("Invalid status")
        q["status"] = status
    if date_from is not None: 
        parse_date(date_from, "date_from")
    if date_to is not None: 
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to: 
        fail422("date_from must be <= date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to: 
            q["date"]["$lte"] = date_to
    total = db.attendance_logs.count_documents(q)
    docs = list(db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None: 
        raise HTTPException(404, "Employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None: 
        raise HTTPException(404, "Attendance record not found")
    if len(body.reason.strip()) < 5 or len(body.regularized_by.strip()) < 1: 
        fail422("reason or regularized_by is invalid")
    if body.status is not None and body.status not in STATUSES: 
        fail422("Invalid status")
    if body.punch_in is not None: 
        validate_epoch_ms(body.punch_in, "punch_in")
    if body.punch_out is not None: 
        validate_epoch_ms(body.punch_out, "punch_out")
    old = {"status": rec.get("status"), "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
           "late_minutes": int(rec.get("late_minutes", 0) or 0), "work_hours": rec.get("work_hours"),
           "overtime_minutes": int(rec.get("overtime_minutes", 0) or 0), "half_day": bool(rec.get("half_day", False))}
    status = body.status if body.status is not None else old["status"]
    pin = to_datetime(body.punch_in) if body.punch_in is not None else old["punch_in"]
    pout = to_datetime(body.punch_out) if body.punch_out is not None else old["punch_out"]
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None: fail422("ABSENT and LEAVE cannot have punch times")
        pin = pout = None
    else:
        if pin is None: fail422("A presence status requires a punch-in time")
        if attendance_date_for(pin, emp) != date:
            fail422("Punch-in must be on the attendance date")
        if pout is not None and (pout <= pin or (pout - pin).total_seconds() > 86400): 
            fail422("Invalid punch-out time")
    derived = derived_values(emp, date, pin, pout, status)
    new = {"status": status, "punch_in": pin, "punch_out": pout, **derived}
    changes = {}
    for key in ("status", "punch_in", "punch_out", "late_minutes", "work_hours", "overtime_minutes", "half_day"):
        if old[key] != new[key]:
            changes[key] = {"from": old[key], "to": new[key]}
    if not changes: fail422("The request does not change anything")
    history = {"at": now_utc_naive(), "by": body.regularized_by.strip(), "reason": body.reason.strip(), "changes": changes}
    # Optimistic concurrency: the previous history length and values must still match.
    selector = {"_id": rec["_id"], "status": old["status"]}
    selector["history"] = rec["history"] if "history" in rec else {"$exists": False}
    selector["punch_in"] = old["punch_in"]
    selector["punch_out"] = old["punch_out"]
    result = db.attendance_logs.update_one(selector, {"$set": new, "$push": {"history": history}})
    if result.modified_count != 1: raise HTTPException(409, "Attendance record changed concurrently; retry")
    rec.update(new); rec.setdefault("history", []).append(history)
    return serialize_attendance(rec)

# ------------------------------ Analytics ----------------------------------
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    first, last = parse_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None: raise HTTPException(404, "Employee not found")
    start = max(first, parse_date(emp["joined_on"], "joined_on"))
    working_days = 0
    if start <= last:
        # Calendar count is performed by MongoDB, not by iterating dates in Python.
        cal = db.employees.aggregate([{"$match": {"emp_code": emp_code}}, {"$project": {
            "days": {"$filter": {"input": {"$map": {"input": {"$range": [0, {"$add": [{"$dateDiff": {"startDate": {"$dateFromString": {"dateString": start.isoformat()}}, "endDate": {"$dateFromString": {"dateString": last.isoformat()}}, "unit": "day"}}, 1]}]}, "as": "n", "in": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": start.isoformat()}}, "unit": "day", "amount": "$$n"}}}}, "as": "d", "cond": {"$and": [{"$gte": [{"$dayOfWeek": "$$d"}, 2]}, {"$lte": [{"$dayOfWeek": "$$d"}, 6]}]}}}}}, {"$project": {"n": {"$size": "$days"}}}])
        row = next(cal, None); working_days = row["n"] if row else 0
    pipeline = [
    {
        "$match": {
            "emp_code": emp_code,
            "date": {
                "$gte": start.isoformat(),
                "$lte": last.isoformat()
            }
        }
    },
    {
        "$addFields": {
            "weekday": {
                "$dayOfWeek": {
                    "$dateFromString": {"dateString": "$date"}
                }
            }
        }
    },
        {"$group": {"_id": None, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$gte": ["$weekday", 2]}, {"$lte": ["$weekday", 6]}]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}]
    row = next(db.attendance_logs.aggregate(pipeline), {})
    present = float(row.get("present_days", 0))
    pct = round_half_up(present / working_days * 100, 2) if working_days else None
    return {"emp_code": emp_code, "month": month, "working_days": working_days, "present_days": present,
            "leave_days": row.get("leave_days", 0), "late_count": row.get("late_count", 0),
            "total_late_minutes": row.get("total_late_minutes", 0), "total_overtime_minutes": row.get("total_overtime_minutes", 0), "attendance_pct": pct}

@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    first, last = parse_month(month)
    match = {"$match": {"joined_on": {"$lte": last.isoformat()}}}
    if department is not None: match["$match"]["department"] = department
    # Join logs and aggregate employee-level present-day contributions, retaining employees with no logs.
    pipeline = [match, {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code", "join": "$joined_on"}, "pipeline": [
        {
    "$match": {
        "$expr": {
            "$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", first.isoformat()]},
                {"$lte": ["$date", last.isoformat()]},
                {"$gte": ["$date", "$$join"]}
            ]
        }
    }
},
        {"$addFields": {"weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}}], "as": "logs"}},
        {"$addFields": {"working_start": {"$cond": [{"$gt": ["$joined_on", first.isoformat()]}, "$joined_on", first.isoformat()]}}},
        {"$project": {"department": 1, "logs": 1}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {"_id": "$department",
          "employees": {"$addToSet": "$_id"}, "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$gte": ["$logs.weekday", 2]}, {"$lte": ["$logs.weekday", 6]}]}, {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1]}, 0]}},
          "work_hours_values": {"$push": {"$cond": [{"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", None]}},
          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
          "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
          "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
          "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}}},
        {"$project": {"department": "$_id", "headcount": {"$size": "$employees"}, "present_days": 1,
          "avg_work_hours": {"$avg": {"$filter": {"input": "$work_hours_values", "as": "h", "cond": {"$ne": ["$$h", None]}}}},
          "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}} , {"$sort": {"department": 1}}]
    items = list(db.employees.aggregate(pipeline))
    output = []
    for x in items:
        # $addToSet includes every employee, including zero-log employees. Headcount is the set size.
        avg = x.get("avg_work_hours")
        output.append({"department": x["department"], "headcount": x["headcount"], "present_days": float(x.get("present_days", 0)),
            "avg_work_hours": round_half_up(avg, 2) if avg is not None else None, "late_count": x.get("late_count", 0),
            "total_late_minutes": x.get("total_late_minutes", 0), "leave_count": x.get("leave_count", 0), "on_duty_count": x.get("on_duty_count", 0)})
    return {"month": month, "items": output}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    q = month_log_match(month)
    pipeline = [{"$match": q}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}}, {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}}, {"$unwind": "$employee"}]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
                 {"$sort": {"total_late_minutes": -1, "_id": 1}}, {"$match": {"rank": {"$lte": limit}}},
                 {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}]
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
    if end < start: fail422("to must be on or after from")
    if (end - start).days + 1 > 92: fail422("Date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}): raise HTTPException(404, "Department not found")
    # Generate calendar rows in MongoDB; this is server-side gap filling, not a Python date loop.
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {
            "employees": 1,
            "days": {"$map": {
                "input": {"$range": [0, (end - start).days + 1]},
                "as": "n",
                "in": {"$dateToString": {
                    "format": "%Y-%m-%d",
                    "date": {"$dateAdd": {
                        "startDate": {"$dateFromString": {"dateString": start.isoformat()}},
                        "unit": "day", "amount": "$$n"
                    }}
                }}
            }}
        }},
        {"$unwind": "$days"},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$days", "codes": "$employees"},
            "pipeline": [
                {
                    "$match": {
                        "$expr": {
                            "$and": [
                                {"$eq": ["$date", "$$d"]},
                                {
                                    "$anyElementTrue": {
                                        "$map": {
                                            "input": "$$codes",
                                            "as": "e",
                                            "in": {
                                                "$and": [
                                                    {"$eq": ["$$e.emp_code", "$emp_code"]},
                                                    {"$lte": ["$$e.joined_on", "$$d"]}
                                                ]
                                            }
                                        }
                                    }
                                }
                            ]
                        }
                    }
                }
            ],
            "as": "logs"
        }},
        {"$project": {
            "date": "$days",
            "headcount": {"$size": {"$filter": {"input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$days"]}}}},
            "is_working_day": {"$and": [
                {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$days"}}}, 2]},
                {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$days"}}}, 6]}
            ]},
            "present_count": {"$sum": {"$map": {
                "input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", list(PRESENCE)]}}},
                "as": "l", "in": {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1]}
            }}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}}
        }},
        {"$sort": {"date": 1}}, *TREND_TAIL
    ]
    rows = list(db.employees.aggregate(pipeline))
    def dec(v):
        return v.to_decimal() if isinstance(v, Decimal128) else (None if v is None else Decimal(str(v)))
    items = []
    for row in rows:
        rate, avg = dec(row.get("attendance_rate")), dec(row.get("moving_avg_7d"))
        items.append({"date": row["date"], "is_working_day": row["is_working_day"], "headcount": row["headcount"],
                      "present_count": float(row.get("present_count", 0)), "late_count": row.get("late_count", 0),
                      "attendance_rate": float(rate) if rate is not None else None,
                      "moving_avg_7d": round_half_up(avg, 4) if avg is not None else None})
    return {"department": department, "items": items}

# -------------------------------- Explain -----------------------------------
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None, department: Optional[str] = None,
                     limit: int = Query(10, ge=1, le=50), date_from: Optional[str] = None, date_to: Optional[str] = None,
                     status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                     from_date: Optional[str] = Query(None, alias="from"), to_date: Optional[str] = Query(None, alias="to")):
    valid = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid: fail422("Invalid endpoint")
    if endpoint == "attendance_list":
        if date_from: parse_date(date_from, "date_from")
        if date_to: parse_date(date_to, "date_to")
        if date_from and date_to and date_from > date_to: fail422("date_from must be <= date_to")
        q: dict[str, Any] = {}
        if emp_code: q["emp_code"] = emp_code
        if status:
            if status not in STATUSES: fail422("Invalid status")
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        # Same find filter, sort and pagination as GET /attendance.
        exp = db.command({"explain": {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size, "limit": page_size}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": bson_safe(exp)}
    if endpoint in ("employee_monthly", "department_summary", "late_leaderboard") and not month:
        fail422("month is required")
    if month: parse_month(month)
    if endpoint == "employee_monthly":
        if not emp_code:
            fail422("emp_code is required")

        emp = db.employees.find_one({"emp_code": emp_code})
        if emp is None:
            raise HTTPException(404, "Employee not found")

        first, last = parse_month(month)
        joined_on = parse_date(emp["joined_on"], "joined_on")
        start = max(first, joined_on)

        pipe = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": start.isoformat(),
                        "$lte": last.isoformat()
                    }
                }
            },
            {
                "$addFields": {
                    "weekday": {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", list(PRESENCE)]},
                                        {"$gte": ["$weekday", 2]},
                                        {"$lte": ["$weekday", 6]}
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$status", "LEAVE"]},
                                1,
                                0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    }
                }
            }
        ]

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": aggregation_explain("attendance_logs", pipe)
        }
    if endpoint == "department_summary":
        first, last = parse_month(month)
        employee_match = {"joined_on": {"$lte": last.isoformat()}}
        if department is not None:
            employee_match["department"] = department
        pipe = [
            {"$match": employee_match},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "code": "$emp_code",
                        "join": "$joined_on"
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$code"]},
                                        {"$gte": ["$date", first.isoformat()]},
                                        {"$lte": ["$date", last.isoformat()]},
                                        {"$gte": ["$date", "$$join"]}
                                    ]
                                }
                            }
                        },
                        {
                            "$addFields": {
                                "weekday": {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$date"
                                        }
                                    }
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            },
            {
                "$addFields": {
                    "working_start": {
                        "$cond": [
                            {"$gt": ["$joined_on", first.isoformat()]},
                            "$joined_on",
                            first.isoformat()
                        ]
                    }
                }
            },
            {"$project": {"department": 1, "logs": 1}},
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "employees": {"$addToSet": "$_id"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENCE)]},
                                        {"$gte": ["$logs.weekday", 2]},
                                        {"$lte": ["$logs.weekday", 6]}
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$logs.half_day", True]},
                                        0.5,
                                        1
                                    ]
                                },
                                0
                            ]
                        }
                    },
                    "work_hours_values": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$logs.status", list(PRESENCE)]},
                                        {"$ne": ["$logs.work_hours", None]}
                                    ]
                                },
                                "$logs.work_hours",
                                None
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]},
                                1,
                                0
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1,
                                0
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1,
                                0
                            ]
                        }
                    }
                }
            },
            {
                "$project": {
                    "department": "$_id",
                    "headcount": {"$size": "$employees"},
                    "present_days": 1,
                    "avg_work_hours": {
                        "$avg": {
                            "$filter": {
                                "input": "$work_hours_values",
                                "as": "h",
                                "cond": {"$ne": ["$$h", None]}
                            }
                        }
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1
                }
            },
            {"$sort": {"department": 1}}
        ]

        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": aggregation_explain("employees", pipe)
        }
    if endpoint == "late_leaderboard":
        pipe = [
            {"$match": month_log_match(month)},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0
                            ]
                        }
                    }
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee"
                }
            },
            {"$unwind": "$employee"}
        ]

        if department is not None:
            pipe.append({"$match": {"employee.department": department}})

        pipe.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}}
                }
            },
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1
                }
            }
        ])

        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": aggregation_explain("attendance_logs", pipe)
        }
    # department_trend: explain the same aggregation pipeline as the real endpoint.
    if not department or from_date is None or to_date is None:
        fail422("department, from and to are required")

    start, end = parse_date(from_date, "from"), parse_date(to_date, "to")

    if end < start:
        fail422("to must be on or after from")

    if (end - start).days + 1 > 92:
        fail422("Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "Department not found")

    pipe = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on"
                    }
                }
            }
        },
        {
            "$project": {
                "employees": 1,
                "days": {
                    "$map": {
                        "input": {"$range": [0, (end - start).days + 1]},
                        "as": "n",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": start.isoformat()
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$n"
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        {"$unwind": "$days"},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "d": "$days",
                    "codes": "$employees"
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$d"]},
                                    {
                                        "$anyElementTrue": {
                                            "$map": {
                                                "input": "$$codes",
                                                "as": "e",
                                                "in": {
                                                    "$and": [
                                                        {
                                                            "$eq": [
                                                                "$$e.emp_code",
                                                                "$emp_code"
                                                            ]
                                                        },
                                                        {
                                                            "$lte": [
                                                                "$$e.joined_on",
                                                                "$$d"
                                                            ]
                                                        }
                                                    ]
                                                }
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$project": {
                "date": "$days",
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$days"]}
                        }
                    }
                },
                "is_working_day": {
                    "$and": [
                        {
                            "$gte": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$days"
                                        }
                                    }
                                },
                                2
                            ]
                        },
                        {
                            "$lte": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$days"
                                        }
                                    }
                                },
                                6
                            ]
                        }
                    ]
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "l",
                                    "cond": {
                                        "$in": [
                                            "$$l.status",
                                            list(PRESENCE)
                                        ]
                                    }
                                }
                            },
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$l.half_day", True]},
                                    0.5,
                                    1
                                ]
                            }
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "l",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$l.late_minutes", 0]},
                                    0
                                ]
                            }
                        }
                    }
                }
            }
        },
        {"$sort": {"date": 1}}, *TREND_TAIL
    ]

    return {
        "endpoint": endpoint,
        "collection": "employees",
        "explain": aggregation_explain("employees", pipe)
    }
