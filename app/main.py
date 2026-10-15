from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.collection import Collection

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
EPOCH_MIN_MS = 100_000_000_000
EPOCH_MAX_MS = 253_402_300_799_999  # end of year 9999, in milliseconds
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "employee_attendance")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, tz_aware=False)
db = client[MONGO_DB]
employees: Collection = db["employees"]
attendance: Collection = db["attendance_logs"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------- Common conversion and validation helpers ----------
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    # PyMongo returns naive UTC datetimes when tz_aware=False.
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_db_datetime(epoch_ms: int) -> datetime:
    if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int):
        raise HTTPException(status_code=422, detail="Timestamp must be an integer epoch-millisecond value")
    if epoch_ms < EPOCH_MIN_MS or epoch_ms > EPOCH_MAX_MS:
        raise HTTPException(status_code=422, detail="Timestamp is outside the supported epoch-millisecond range")
    # Truncate to whole seconds before calculations and storage.
    seconds = epoch_ms // 1000
    try:
        return datetime.fromtimestamp(seconds, UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(status_code=422, detail="Timestamp is outside the supported range")


def to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    value = as_utc(value)
    return int(value.timestamp()) * 1000


def half_up(value: float | Decimal, places: int = 2) -> float:
    quant = Decimal("1") if places == 0 else Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def parse_date(value: str, field: str = "date") -> date:
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"{field} must be a valid calendar date")


def parse_month(value: str) -> tuple[date, date]:
    if not MONTH_RE.fullmatch(value or ""):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    try:
        start = date.fromisoformat(value + "-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    next_month = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
    return start, next_month - timedelta(days=1)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def working_days(start: date, end: date) -> int:
    if end < start:
        return 0
    total = 0
    current = start
    while current <= end:
        if is_weekday(current):
            total += 1
        current += timedelta(days=1)
    return total


def hm(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def local_dt(day: date, value: str) -> datetime:
    return datetime.combine(day, hm(value), tzinfo=IST)


def attendance_date_for(punch_in: datetime, employee: dict[str, Any]) -> date:
    local = as_utc(punch_in).astimezone(IST)
    start = hm(employee["shift_start"])
    end = hm(employee["shift_end"])
    overnight = end <= start
    if overnight and local.timetz().replace(tzinfo=None) < end:
        return local.date() - timedelta(days=1)
    return local.date()


def late_minutes_for(punch_in: datetime, employee: dict[str, Any], record_date: date) -> int:
    local = as_utc(punch_in).astimezone(IST)
    scheduled_start = local_dt(record_date, employee["shift_start"])
    elapsed = (local - scheduled_start).total_seconds() / 60
    # Late only when strictly more than 10 minutes after shift start.
    if elapsed > 10:
        return max(0, int(elapsed // 1))
    return 0


def calculate_derived(employee: dict[str, Any], record_date: date, punch_in: datetime | None,
                      punch_out: datetime | None, status: str) -> dict[str, Any]:
    if status not in PRESENCE:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = late_minutes_for(punch_in, employee, record_date) if punch_in else 0
    if punch_out is None or punch_in is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    pin = as_utc(punch_in)
    pout = as_utc(punch_out)
    elapsed_seconds = (pout - pin).total_seconds()
    hours = half_up(Decimal(str(elapsed_seconds)) / Decimal("3600"), 2)
    start = hm(employee["shift_start"])
    end = hm(employee["shift_end"])
    end_day = record_date + timedelta(days=1) if end <= start else record_date
    scheduled_end = local_dt(end_day, employee["shift_end"]).astimezone(UTC)
    overtime_elapsed = (pout - scheduled_end).total_seconds() / 60
    overtime = int(overtime_elapsed // 1) if overtime_elapsed >= 30 else 0
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": max(0, overtime), "half_day": hours < 4.50}


def serialize_history(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    result = []
    for entry in history or []:
        changes = {}
        for key, change in (entry.get("changes") or {}).items():
            changes[key] = {
                "from": to_epoch_ms(change.get("from")) if key in {"punch_in", "punch_out"} else change.get("from"),
                "to": to_epoch_ms(change.get("to")) if key in {"punch_in", "punch_out"} else change.get("to"),
            }
        result.append({"at": to_epoch_ms(entry.get("at")), "by": entry.get("by"),
                       "reason": entry.get("reason"), "changes": changes})
    return result


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    return {"emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
            "department": doc["department"], "shift_start": doc["shift_start"],
            "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
            "created_at": to_epoch_ms(doc.get("created_at"))}


def serialize_attendance(doc: dict[str, Any]) -> dict[str, Any]:
    return {"emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
            "punch_in": to_epoch_ms(doc.get("punch_in")), "punch_out": to_epoch_ms(doc.get("punch_out")),
            "work_hours": doc.get("work_hours"), "late_minutes": int(doc.get("late_minutes", 0) or 0),
            "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
            "half_day": bool(doc.get("half_day", False)), "history": serialize_history(doc.get("history", []))}


def pagination(page: int, page_size: int) -> tuple[int, int]:
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")
    return (page - 1) * page_size, page_size


# ---------- Request models ----------
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_shift_time(cls, v: str) -> str:
        if not TIME_RE.fullmatch(v):
            raise ValueError("time must be HH:MM in 24-hour format")
        return v

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v: str) -> str:
        if not DATE_RE.fullmatch(v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class EpochTimestamp(BaseModel):
    @field_validator("punched_at", mode="before", check_fields=False)
    @classmethod
    def validate_epoch_ms(cls, v):
        if v is None:
            return v
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("must be an integer epoch-millisecond timestamp")
        if not EPOCH_MIN_MS <= v <= EPOCH_MAX_MS:
            raise ValueError("timestamp is outside the supported range")
        return v


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: int | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        if isinstance(v, bool) or not isinstance(v, int) or not EPOCH_MIN_MS <= v <= EPOCH_MAX_MS:
            raise ValueError("punched_at must be an integer epoch-millisecond timestamp in range")
        return v


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(min_length=1)
    punched_at: int | None = None

    @field_validator("punched_at", mode="before")
    @classmethod
    def validate_punched_at(cls, v):
        if v is None:
            return v
        if isinstance(v, bool) or not isinstance(v, int) or not EPOCH_MIN_MS <= v <= EPOCH_MAX_MS:
            raise ValueError("punched_at must be an integer epoch-millisecond timestamp in range")
        return v


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: int | None = None
    punch_out: int | None = None

    @field_validator("punch_in", "punch_out", mode="before")
    @classmethod
    def validate_times(cls, v):
        if v is None:
            return v
        if isinstance(v, bool) or not isinstance(v, int) or not EPOCH_MIN_MS <= v <= EPOCH_MAX_MS:
            raise ValueError("must be an integer epoch-millisecond timestamp in range")
        return v


# ---------- Indexes and health ----------
@app.on_event("startup")
def create_indexes() -> None:
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_joined")
    employees.create_index([("joined_on", ASCENDING)],name="ix_employee_joined_on",)
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    attendance.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    attendance.create_index([("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_status_employee")
    attendance.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


# ---------- Employee endpoints ----------
@app.post("/employees", status_code=201)
def create_employee(payload: EmployeeCreate):
    doc = payload.model_dump()
    doc["created_at"] = now_utc().replace(tzinfo=None)
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Employee code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1), page_size: int = Query(20)):
    skip, size = pagination(page, page_size)
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = employees.count_documents(query)
    docs = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(size)
    return {"items": [serialize_employee(d) for d in docs], "page": page, "page_size": size, "total": total}


# ---------- Attendance endpoints ----------
@app.post("/attendance/punch-in", status_code=201)
def punch_in(payload: PunchInRequest):
    employee = employees.find_one({"emp_code": payload.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    punch = to_db_datetime(payload.punched_at if payload.punched_at is not None else int(now_utc().timestamp() * 1000))
    day = attendance_date_for(punch.replace(tzinfo=UTC), employee)
    date_string = day.isoformat()
    derived = calculate_derived(employee, day, punch.replace(tzinfo=UTC), None, payload.status)
    doc = {"emp_code": payload.emp_code, "date": date_string, "status": payload.status,
           "punch_in": punch, "punch_out": None, **derived, "history": []}
    try:
        attendance.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Attendance record already exists for this employee and date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(payload: PunchOutRequest):
    employee = employees.find_one({"emp_code": payload.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    punch = to_db_datetime(payload.punched_at if payload.punched_at is not None else int(now_utc().timestamp() * 1000))
    # Find the most recent eligible open record. The sort is on attendance date,
    # but the punch_in comparison ensures overnight records can be closed next day.
    candidate = attendance.find_one({"emp_code": payload.emp_code, "status": {"$in": list(PRESENCE)},
                                     "punch_in": {"$ne": None, "$lte": punch}},
                                    sort=[("punch_in", DESCENDING)])
    if not candidate:
        raise HTTPException(status_code=404, detail="No matching attendance record found")
    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")
    pin = as_utc(candidate["punch_in"])
    pout = as_utc(punch.replace(tzinfo=UTC))
    elapsed = (pout - pin).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in and no more than 24 hours later")
    record_day = parse_date(candidate["date"])
    derived = calculate_derived(employee, record_day, pin, pout, candidate["status"])
    update = {"punch_out": punch, **derived}
    # Atomic conditional update: only one concurrent punch-out can succeed.
    updated = attendance.find_one_and_update({"_id": candidate["_id"], "punch_out": None},
                                             {"$set": update}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(status_code=409, detail="Attendance record is already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None,
                    date_to: str | None = None, status: str | None = None,
                    page: int = Query(1), page_size: int = Query(20)):
    skip, size = pagination(page, page_size)
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        if status not in ALL_STATUSES:
            raise HTTPException(status_code=422, detail="Invalid status")
        query["status"] = status
    total = attendance.count_documents(query)
    docs = attendance.find(query).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(size)
    return {"items": [serialize_attendance(d) for d in docs], "page": page, "page_size": size, "total": total}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, payload: RegularizeRequest):
    record_day = parse_date(date)
    employee = employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    current = attendance.find_one({"emp_code": emp_code, "date": date})
    if not current:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    body = payload.model_dump(exclude_unset=True)
    requested_status = body.get("status", current["status"])
    if requested_status in {"ABSENT", "LEAVE"} and (body.get("punch_in") is not None or body.get("punch_out") is not None):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE records cannot have punch times")
    new_pin = to_db_datetime(body["punch_in"]) if body.get("punch_in") is not None else current.get("punch_in")
    new_pout = to_db_datetime(body["punch_out"]) if body.get("punch_out") is not None else current.get("punch_out")
    if requested_status in {"ABSENT", "LEAVE"}:
        new_pin = new_pout = None
    else:
        if new_pin is None:
            raise HTTPException(status_code=422, detail="Presence statuses require punch_in")
        if attendance_date_for(new_pin.replace(tzinfo=UTC), employee) != record_day:
            raise HTTPException(status_code=422, detail="punch_in must belong to the attendance date")
        if new_pout is not None:
            seconds = (as_utc(new_pout.replace(tzinfo=UTC)) - as_utc(new_pin.replace(tzinfo=UTC))).total_seconds()
            if seconds <= 0 or seconds > 24 * 3600:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in and no more than 24 hours later")
    derived = calculate_derived(employee, record_day, new_pin.replace(tzinfo=UTC) if new_pin else None,
                                new_pout.replace(tzinfo=UTC) if new_pout else None, requested_status)
    desired = {"status": requested_status, "punch_in": new_pin, "punch_out": new_pout, **derived}
    change_fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes: dict[str, Any] = {}
    set_fields: dict[str, Any] = {}
    for key in change_fields:
        old = current.get(key, False if key == "half_day" else 0 if key in {"late_minutes", "overtime_minutes"} else None)
        new = desired[key]
        if key in {"punch_in", "punch_out"}:
                 old_cmp, new_cmp = as_utc(old), as_utc(new)
        else:
            old_cmp, new_cmp = old, new
        if old_cmp != new_cmp:
            changes[key] = {"from": old, "to": new}
            set_fields[key] = new
    if not changes:
        raise HTTPException(status_code=422, detail="Regularization does not change any fields")
    entry = {"at": now_utc().replace(tzinfo=None), "by": payload.regularized_by,
             "reason": payload.reason, "changes": changes}
    # Optimistic concurrency check using the observed _id and previous values.
    filter_doc: dict[str, Any] = {"_id": current["_id"]}
    for key in change_fields:
        filter_doc[key] = current.get(key, {"$exists": False}) if key not in current else current[key]
    updated = attendance.find_one_and_update(filter_doc,
        {"$set": set_fields, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(status_code=409, detail="Attendance record changed concurrently; retry the correction")
    return serialize_attendance(updated)


# ---------- Analytics pipeline helpers ----------
def month_bounds(month: str) -> tuple[str, str, date, date]:
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end


def presence_expr() -> dict[str, Any]:
    return {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}


def present_day_expr() -> dict[str, Any]:
    return {"$cond": [{"$and": [presence_expr(), {"$eq": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, {"$literal": 1}]}]}, 0, 0]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    employee = employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    first_s, last_s, first, last = month_bounds(month)
    joined = parse_date(employee["joined_on"], "joined_on")
    work_start = max(first, joined)
    workdays = working_days(work_start, last)
    rows = list(attendance.aggregate([
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}},
        {"$addFields": {"_weekday": {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [presence_expr(), "$_weekday", {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]}]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}}
    ]))
    stats = rows[0] if rows else {}
    pct = half_up(Decimal(str(stats.get("present_days", 0))) / Decimal(workdays) * 100, 2) if workdays else None
    return {"emp_code": emp_code, "month": month, "working_days": workdays,
            "present_days": half_up(stats.get("present_days", 0), 2), "leave_days": int(stats.get("leave_days", 0)),
            "late_count": int(stats.get("late_count", 0)), "total_late_minutes": int(stats.get("total_late_minutes", 0)),
            "total_overtime_minutes": int(stats.get("total_overtime_minutes", 0)), "attendance_pct": pct}


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: str | None = None):
    first_s, last_s, _, _ = month_bounds(month)
    employee_match: dict[str, Any] = {"joined_on": {"$lte": last_s}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first_s]}, {"$lte": ["$date", last_s]}]}}},
            {"$project": {"_id": 0, "date": 1, "status": 1, "work_hours": 1, "late_minutes": 1, "half_day": 1}}
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$addFields": {"_isweekday": {"$and": [{"$ne": ["$logs.date", None]}, {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", last_s]}}}}, 2]}, {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", last_s]}}}}, 6]}]}}},
        {"$group": {"_id": {"department": "$department", "emp_code": "$emp_code"},
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$_isweekday"]}, {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            "work_hours_sum": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]}, "$logs.work_hours", 0]}},
            "work_hours_n": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]}, 1, 0]}}}},
        {"$group": {"_id": "$_id.department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$present_days"},
            "late_count": {"$sum": "$late_count"}, "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"}, "on_duty_count": {"$sum": "$on_duty_count"},
            "work_hours_sum": {"$sum": "$work_hours_sum"}, "work_hours_n": {"$sum": "$work_hours_n"}}},
        {"$sort": {"_id": 1}}
    ]
    items = []
    for row in employees.aggregate(pipeline):
        items.append({"department": row["_id"], "headcount": int(row["headcount"]),
            "present_days": half_up(row.get("present_days", 0), 2),
            "avg_work_hours": half_up(row["work_hours_sum"] / row["work_hours_n"], 2) if row.get("work_hours_n", 0) else None,
            "late_count": int(row.get("late_count", 0)), "total_late_minutes": int(row.get("total_late_minutes", 0)),
            "leave_count": int(row.get("leave_count", 0)), "on_duty_count": int(row.get("on_duty_count", 0))})
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: str | None = None):
    first_s, last_s, _, _ = month_bounds(month)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                     "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ])
    return {"month": month, "items": list(attendance.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    start = parse_date(from_date, "from")
    end = parse_date(to_date, "to")
    if end < start:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    days = (end - start).days + 1
    if days > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")
    if not employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")
    # Generate the calendar range inside MongoDB, then look up daily logs and
    # active headcount. This also returns rows for weekends and days with no logs.
    pipeline = [
        {"$documents": [{"start": datetime.combine(start, time.min, tzinfo=UTC), "count": days}]},
        {"$project": {"days": {"$range": [0, "$count"]}, "start": 1}},
        {"$unwind": "$days"},
        {"$project": {"day": {"$dateAdd": {"startDate": "$start", "unit": "day", "amount": "$days"}}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
                   "weekday": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"e.department": department}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                   "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$weekday", {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$floor": [{"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}]}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
         {"$project": {"_id": 0, "date": 1, "is_working_day": "$weekday", "headcount": 1, "present_count": 1,
                       "late_count": 1, "attendance_rate": 1, "moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_7d", None]}, None, {"$divide": [{"$floor": [{"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}]}, 10000]}]}}}
    ]
    try:
        rows = list(db.aggregate(pipeline))
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"Trend aggregation failed: {str(exc)}")
    return {"department": department, "items": rows}


# ---------- Explain endpoint ----------
EXPLAIN_ENDPOINTS = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: str = Path(...), emp_code: str | None = None, month: str | None = None,
                     department: str | None = None, limit: int | None = Query(None, ge=1, le=50),
                     date_from: str | None = None, date_to: str | None = None, status: str | None = None,
                     from_date: str | None = Query(None, alias="from"), to_date: str | None = Query(None, alias="to"),
                     page: int = Query(1), page_size: int = Query(20)):
    if endpoint not in EXPLAIN_ENDPOINTS:
        raise HTTPException(status_code=422, detail="Unknown explain endpoint")
    skip, size = pagination(page, page_size)
    try:
        if endpoint == "attendance_list":
            q: dict[str, Any] = {}
            if emp_code: q["emp_code"] = emp_code
            if date_from:
                parse_date(date_from, "date_from"); q.setdefault("date", {})["$gte"] = date_from
            if date_to:
                parse_date(date_to, "date_to"); q.setdefault("date", {})["$lte"] = date_to
            if date_from and date_to and date_from > date_to: raise HTTPException(422, "date_from must be on or before date_to")
            if status:
                if status not in ALL_STATUSES: raise HTTPException(422, "Invalid status")
                q["status"] = status
            exp = db.command({"explain": {"find": "attendance_logs", "filter": q, "sort": {"date": -1, "emp_code": 1}, "skip": skip, "limit": size}, "verbosity": "executionStats"})
            collection = "attendance_logs"
        elif endpoint == "employee_monthly":
            if not emp_code or not month: raise HTTPException(422, "emp_code and month are required")
            first, last, _, _ = month_bounds(month)
            pipe = [{"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}}, {"$group": {"_id": "$emp_code", "n": {"$sum": 1}}}]
            exp = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
            collection = "attendance_logs"
        elif endpoint == "department_summary":
            if not month: raise HTTPException(422, "month is required")
            first, last, _, _ = month_bounds(month)
            match: dict[str, Any] = {"joined_on": {"$lte": last}}
            if department: match["department"] = department
            pipe = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}, {"$unwind": "$logs"}, {"$match": {"logs.date": {"$gte": first, "$lte": last}}}, {"$group": {"_id": "$department", "n": {"$sum": 1}}}]
            exp = db.command({"explain": {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
            collection = "employees"
        elif endpoint == "late_leaderboard":
            if not month: raise HTTPException(422, "month is required")
            first, last, _, _ = month_bounds(month)
            pipe: list[dict[str, Any]] = [{"$match": {"date": {"$gte": first, "$lte": last}}}, {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}}, {"$match": {"total_late_minutes": {"$gt": 0}}}]
            if department:
                pipe += [{"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}}, {"$unwind": "$e"}, {"$match": {"e.department": department}}]
            exp = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
            collection = "attendance_logs"
        else:
            if not department or not from_date or not to_date: raise HTTPException(422, "department, from, and to are required")
            start, end = parse_date(from_date, "from"), parse_date(to_date, "to")
            if end < start or (end - start).days + 1 > 92: raise HTTPException(422, "Invalid trend date range")
            # Explain the indexed source query used by the trend endpoint.
            exp = db.command({"explain": {"find": "attendance_logs", "filter": {"date": {"$gte": from_date, "$lte": to_date}}, "sort": {"date": 1}}, "verbosity": "executionStats"})
            collection = "attendance_logs"
    except HTTPException:
        raise
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail=f"Explain failed: {str(exc)}")
    return {"endpoint": endpoint, "collection": collection, "explain": exp}
