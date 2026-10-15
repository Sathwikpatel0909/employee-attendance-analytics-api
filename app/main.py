"""Employee Attendance & Analytics API."""
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# Existing process environment always takes precedence over values in .env.
load_dotenv(override=False)
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=timezone.utc)
db = client[os.getenv("MONGO_DB", "attendance_db")]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS, MAX_EPOCH_MS = 100_000_000_000, 4_102_444_800_000
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True, name="uq_employees_emp_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="ix_employees_department_code")
    db.employees.create_index("joined_on", name="ix_employees_joined_on")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="ix_employees_department_joined_on")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="ix_attendance_date_code")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="ix_attendance_employee_punch_in")


def epoch_datetime(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("must be an epoch-millisecond integer in the supported range")
    try:
        instant = datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("invalid epoch milliseconds") from exc
    return instant.replace(microsecond=0)


def to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def public_attendance(doc: dict) -> dict:
    history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, change in entry.get("changes", {}).items():
            changes[field] = {side: (to_epoch_ms(value) if field in {"punch_in", "punch_out"} else value)
                              for side, value in change.items()}
        history.append({"at": to_epoch_ms(entry.get("at")), "by": entry.get("by"),
                        "reason": entry.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")), "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
        "history": history,
    }


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: Optional[str] = None) -> int:
    local = punch_in.astimezone(IST)
    h, m = map(int, shift_start.split(":"))
    shift_day = local.date()
    if shift_end is not None and shift_end <= shift_start:
        eh, em = map(int, shift_end.split(":"))
        if local.time().replace(tzinfo=None) < time(eh, em):
            shift_day -= timedelta(days=1)
    start = datetime.combine(shift_day, time(h, m), IST)
    seconds = int((local - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = Decimal(int((punch_out - punch_in).total_seconds())) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None) -> int:
    h, m = map(int, shift_end.split(":"))
    day = date.fromisoformat(date_str)
    if shift_start is not None and shift_end <= shift_start:
        day += timedelta(days=1)
    end = datetime.combine(day, time(h, m), IST)
    minutes = int((punch_out.astimezone(IST) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def attendance_date(punch_in: datetime, employee: dict) -> str:
    local = punch_in.astimezone(IST)
    day = local.date()
    if employee["shift_end"] <= employee["shift_start"]:
        h, m = map(int, employee["shift_end"].split(":"))
        if local.time().replace(tzinfo=None) < time(h, m):
            day -= timedelta(days=1)
    return day.isoformat()


def valid_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "date must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise HTTPException(422, "date must use YYYY-MM-DD format")
    return value


def month_period(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must use YYYY-MM format")
    try:
        start = date.fromisoformat(month + "-01")
        if start.month == 12:
            following = date(start.year + 1, 1, 1)
        else:
            following = date(start.year, start.month + 1, 1)
    except ValueError as exc:
        raise HTTPException(422, "month is outside the supported date range") from exc
    return start, following


def mongo_round_half_up(expression, places: int):
    """Round a nonnegative numeric aggregation expression half-up via Decimal128."""
    scale = Decimal128(str(10 ** places))
    return {"$toDouble": {"$divide": [
        {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expression}, scale]}, Decimal128("0.5")] }},
        scale,
    ]}}


def mongo_weekday(date_expression):
    # MongoDB $dayOfWeek: Sunday=1, Monday=2, ... Saturday=7.
    return {"$in": [{"$dayOfWeek": date_expression}, [2, 3, 4, 5, 6]]}


def attendance_lookup_pipeline(month_start: str, month_end: str):
    return [
        {"$match": {"$expr": {"$and": [
            {"$eq": ["$emp_code", "$$employee_code"]},
            {"$gte": ["$date", month_start]}, {"$lt": ["$date", month_end]},
        ]}}},
        {"$set": {"_attendance_day": {"$dateFromString": {"dateString": "$date"}}}},
        {"$set": {
            "_is_weekday": mongo_weekday("$_attendance_day"),
            "_present_contribution": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    mongo_weekday("$_attendance_day"),
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]},
        }},
        {"$project": {"status": 1, "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                       "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                       "work_hours": 1, "_present_contribution": 1}},
    ]


def build_employee_monthly_pipeline(emp_code: str, month: str):
    start, following = month_period(month)
    month_start, month_end = start.isoformat(), following.isoformat()
    logs_lookup = attendance_lookup_pipeline(month_start, month_end)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {
            "_month_start": {"$dateFromString": {"dateString": month_start}},
            "_month_end": {"$dateFromString": {"dateString": month_end}},
            "_joined": {"$dateFromString": {"dateString": "$joined_on"}},
        }},
        {"$set": {"_working_start": {"$cond": [
            {"$gte": ["$_joined", "$_month_end"]}, "$_month_end",
            {"$cond": [{"$gt": ["$_joined", "$_month_start"]}, "$_joined", "$_month_start"]},
        ]}}},
        {"$set": {"_working_dates": {"$map": {
            "input": {"$range": [0, {"$max": [0, {"$dateDiff": {
                "startDate": "$_working_start", "endDate": "$_month_end", "unit": "day"}}]}]},
            "as": "offset", "in": {"$dateAdd": {"startDate": "$_working_start", "unit": "day", "amount": "$$offset"}},
        }}}},
        {"$set": {"_working_days": {"$size": {"$filter": {
            "input": "$_working_dates", "as": "work_date", "cond": mongo_weekday("$$work_date")}}}}},
        {"$lookup": {"from": "attendance_logs", "let": {"employee_code": "$emp_code"}, "pipeline": logs_lookup, "as": "_logs"}},
        {"$set": {
            "_present_days": {"$reduce": {"input": "$_logs", "initialValue": 0,
                                             "in": {"$add": ["$$value", "$$this._present_contribution"]}}},
            "_leave_days": {"$size": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}}}},
            "_late_count": {"$size": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$gt": ["$$log.late_minutes", 0]}}}},
            "_total_late": {"$reduce": {"input": "$_logs", "initialValue": 0,
                                         "in": {"$add": ["$$value", "$$this.late_minutes"]}}},
            "_total_overtime": {"$reduce": {"input": "$_logs", "initialValue": 0,
                                             "in": {"$add": ["$$value", "$$this.overtime_minutes"]}}},
        }},
        {"$project": {
            "_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": "$_working_days",
            "present_days": mongo_round_half_up("$_present_days", 2), "leave_days": "$_leave_days",
            "late_count": "$_late_count", "total_late_minutes": "$_total_late",
            "total_overtime_minutes": "$_total_overtime",
            "attendance_pct": {"$cond": [
                {"$gt": ["$_working_days", 0]},
                mongo_round_half_up({"$multiply": [{"$divide": ["$_present_days", "$_working_days"]}, 100]}, 4),
                None,
            ]},
        }},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    month_period(month)
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1})
    if employee is None:
        raise HTTPException(404, "employee not found")
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    result = list(db.employees.aggregate(pipeline, hint="uq_employees_emp_code"))
    if not result:
        # Employee existence was checked above; this fallback handles an unexpected concurrent deletion.
        raise HTTPException(404, "employee not found")
    return result[0]


def build_department_summary_pipeline(month: str, department: Optional[str] = None):
    start, following = month_period(month)
    month_start, month_end = start.isoformat(), following.isoformat()
    employee_filter = {"joined_on": {"$lt": month_end}}
    if department is not None:
        employee_filter["department"] = department
    logs_lookup = attendance_lookup_pipeline(month_start, month_end)
    pipeline = [
        {"$match": employee_filter},
        {"$lookup": {"from": "attendance_logs", "let": {"employee_code": "$emp_code"},
                      "pipeline": logs_lookup, "as": "_logs"}},
        {"$set": {
            "_present_days": {"$reduce": {"input": "$_logs", "initialValue": 0,
                                             "in": {"$add": ["$$value", "$$this._present_contribution"]}}},
            "_late_count": {"$size": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$gt": ["$$log.late_minutes", 0]}}}},
            "_total_late": {"$reduce": {"input": "$_logs", "initialValue": 0,
                                         "in": {"$add": ["$$value", "$$this.late_minutes"]}}},
            "_total_overtime": {"$reduce": {"input": "$_logs", "initialValue": 0,
                                             "in": {"$add": ["$$value", "$$this.overtime_minutes"]}}},
            "_leave_count": {"$size": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}}}},
            "_on_duty_count": {"$size": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$eq": ["$$log.status", "ON_DUTY"]}}}},
            "_hours_logs": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$and": [
                {"$in": ["$$log.status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$ne": ["$$log.work_hours", None]},
            ]}}},
        }},
        {"$set": {
            "_hours_sum": {"$reduce": {"input": "$_hours_logs", "initialValue": 0,
                                         "in": {"$add": ["$$value", "$$this.work_hours"]}}},
            "_hours_count": {"$size": "$_hours_logs"},
        }},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$_present_days"},
            "late_count": {"$sum": "$_late_count"}, "total_late_minutes": {"$sum": "$_total_late"},
            "total_overtime_minutes": {"$sum": "$_total_overtime"}, "leave_count": {"$sum": "$_leave_count"},
            "on_duty_count": {"$sum": "$_on_duty_count"}, "hours_sum": {"$sum": "$_hours_sum"},
            "hours_count": {"$sum": "$_hours_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            "present_days": mongo_round_half_up("$present_days", 2), "late_count": 1,
            "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$hours_count", 0]},
                mongo_round_half_up({"$divide": ["$hours_sum", "$hours_count"]}, 2), None,
            ]},
        }},
        {"$sort": {"department": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = None):
    pipeline = build_department_summary_pipeline(month, department)
    hint = "ix_employees_department_joined_on" if department is not None else "ix_employees_joined_on"
    return {"month": month, "items": list(db.employees.aggregate(pipeline, hint=hint))}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None):
    start, following = month_period(month)
    employee_lookup = [{"$match": {"$expr": {"$eq": ["$emp_code", "$$employee_code"]}}}]
    if department is not None:
        employee_lookup[0]["$match"]["department"] = department
    employee_lookup.append({"$project": {"_id": 0, "name": 1, "department": 1}})
    pipeline = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lt": following.isoformat()},
                     "$expr": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$set": {"emp_code": "$_id"}},
        {"$lookup": {"from": "employees", "let": {"employee_code": "$emp_code"},
                      "pipeline": employee_lookup, "as": "employee"}},
        {"$unwind": "$employee"},
        {"$set": {"name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                               "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1,
                       "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline, hint="ix_attendance_date_code"))}


def validate_trend_range(from_date: str, to_date: str):
    valid_date(from_date)
    valid_date(to_date)
    first, last = date.fromisoformat(from_date), date.fromisoformat(to_date)
    inclusive_days = (last - first).days + 1
    if inclusive_days <= 0:
        raise HTTPException(422, "to must be on or after from")
    if inclusive_days > 92:
        raise HTTPException(422, "trend range may not exceed 92 days")
    return first, last, inclusive_days


def build_department_trend_pipeline(department: str, from_date: str, to_date: str, inclusive_days: int):
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": "$department", "_people": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$set": {"_start_date": {"$dateFromString": {"dateString": from_date}},
                   "_end_date": {"$dateFromString": {"dateString": to_date}}}},
        {"$set": {"_calendar": {"$map": {
            "input": {"$range": [0, inclusive_days]}, "as": "offset", "in": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {
                    "startDate": "$_start_date", "unit": "day", "amount": "$$offset"}}}},
                "is_working_day": mongo_weekday({"$dateAdd": {
                    "startDate": "$_start_date", "unit": "day", "amount": "$$offset"}}),
            },
        }}}},
        {"$unwind": "$_calendar"},
        {"$set": {
            "_headcount": {"$size": {"$filter": {"input": "$_people", "as": "person",
                                                      "cond": {"$lte": ["$$person.joined_on", "$_calendar.date"]}}}},
            "_department_codes": {"$map": {"input": "$_people", "as": "person", "in": "$$person.emp_code"}},
        }},
        {"$lookup": {"from": "attendance_logs", "let": {"trend_date": "$_calendar.date", "codes": "$_department_codes"},
                      "pipeline": [{"$match": {"$expr": {"$and": [
                          {"$eq": ["$date", "$$trend_date"]}, {"$in": ["$emp_code", "$$codes"]},
                      ]}}},
                      {"$project": {"status": 1, "half_day": 1, "late_minutes": {"$ifNull": ["$late_minutes", 0]}}}],
                      "as": "_logs"}},
        {"$set": {
            "_present_count": {"$reduce": {"input": "$_logs", "initialValue": 0, "in": {"$add": [
                "$$value", {"$cond": [{"$in": ["$$this.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {"$cond": [{"$ifNull": ["$$this.half_day", False]}, 0.5, 1]}, 0]},
            ]}}},
            "_late_count": {"$size": {"$filter": {"input": "$_logs", "as": "log", "cond": {"$gt": ["$$log.late_minutes", 0]}}}},
        }},
        {"$set": {"_attendance_rate": {"$cond": [
            {"$and": ["$_calendar.is_working_day", {"$gt": ["$_headcount", 0]}]},
            mongo_round_half_up({"$divide": ["$_present_count", "$_headcount"]}, 4), None,
        ]}}},
        {"$setWindowFields": {"partitionBy": "$_id", "sortBy": {"_calendar.date": 1},
                               "output": {"_moving_avg": {"$avg": "$_attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$_calendar.date", "is_working_day": "$_calendar.is_working_day",
                       "headcount": "$_headcount", "present_count": "$_present_count", "late_count": "$_late_count",
                       "attendance_rate": "$_attendance_rate",
                       "moving_avg_7d": {"$cond": [{"$ne": ["$_moving_avg", None]}, mongo_round_half_up("$_moving_avg", 4), None]}}},
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    _, _, inclusive_days = validate_trend_range(from_date, to_date)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline = build_department_trend_pipeline(department, from_date, to_date, inclusive_days)
    return {"department": department, "items": list(db.employees.aggregate(pipeline, hint="ix_employees_department_code"))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Annotated[Optional[str], Query(alias="from")] = None,
    to_date: Annotated[Optional[str], Query(alias="to")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unsupported endpoint")
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "page must be positive and page_size must be between 1 and 100")
    if not 1 <= limit <= 50:
        raise HTTPException(422, "limit must be between 1 and 50")

    if endpoint == "attendance_list":
        query = build_attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        hint = "uq_attendance_employee_date" if emp_code is not None else "ix_attendance_date_code"
        operation = {"find": collection, "filter": query, "projection": {"_id": 0},
                     "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size,
                     "limit": page_size, "hint": hint}
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        month_period(month)
        if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
            raise HTTPException(404, "employee not found")
        collection = "employees"
        operation = {"aggregate": collection, "pipeline": build_employee_monthly_pipeline(emp_code, month),
                     "cursor": {}, "hint": "uq_employees_emp_code"}
    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(422, "month is required for department_summary")
        month_period(month)
        collection = "employees"
        hint = "ix_employees_department_joined_on" if department is not None else "ix_employees_joined_on"
        operation = {"aggregate": collection, "pipeline": build_department_summary_pipeline(month, department),
                     "cursor": {}, "hint": hint}
    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(422, "month is required for late_leaderboard")
        month_period(month)
        collection = "attendance_logs"
        operation = {"aggregate": collection, "pipeline": build_late_leaderboard_pipeline(month, limit, department),
                     "cursor": {}, "hint": "ix_attendance_date_code"}
    else:
        if department is None or from_date is None or to_date is None:
            raise HTTPException(422, "department, from, and to are required for department_trend")
        _, _, day_count = validate_trend_range(from_date, to_date)
        if db.employees.find_one({"department": department}, {"_id": 1}) is None:
            raise HTTPException(404, "department not found")
        collection = "employees"
        operation = {"aggregate": collection,
                     "pipeline": build_department_trend_pipeline(department, from_date, to_date, day_count),
                     "cursor": {}, "hint": "ix_employees_department_code"}

    try:
        explain = db.command("explain", operation, verbosity="executionStats")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB explain failed") from exc
    return {"endpoint": endpoint, "collection": collection, "explain": explain}


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def check_joined_on(cls, value):
        return valid_date(value)

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = Field(default="PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        return None if value is None else (epoch_datetime(value) and value)


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def check_timestamp(cls, value):
        return None if value is None else (epoch_datetime(value) and value)

    @model_validator(mode="after")
    def reject_explicit_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = Field(default=None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def check_timestamp(cls, value):
        return None if value is None else (epoch_datetime(value) and value)

    @model_validator(mode="after")
    def reject_null_punch_times(self):
        for name in ("punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size))
    for item in items:
        item["created_at"] = to_epoch_ms(item.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = epoch_datetime(body.punched_at)
    else:
        now = datetime.now(UTC)
        ts = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    day = attendance_date(ts, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_attendance(doc)


def build_attendance_query(emp_code: Optional[str] = None, date_from: Optional[str] = None,
                           date_to: Optional[str] = None, status: Optional[str] = None):
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        valid_date(date_from)
    if date_to is not None:
        valid_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        if status not in {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}:
            raise HTTPException(422, "invalid status")
        query["status"] = status
    return query


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    query = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    hint = "uq_attendance_employee_date" if emp_code is not None else "ix_attendance_date_code"
    cursor = db.attendance_logs.find(query, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).hint(hint).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_attendance(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    if body.punched_at is not None:
        ts = epoch_datetime(body.punched_at)
    else:
        now = datetime.now(UTC)
        ts = now.replace(microsecond=(now.microsecond // 1000) * 1000)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if record is None:
        future = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}}, sort=[("punch_in", -1)]
        )
        if future is not None:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    delta = (ts - record["punch_in"]).total_seconds()
    if delta <= 0 or delta > 24 * 60 * 60:
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    hours = compute_work_hours(record["punch_in"], ts)
    update = {"punch_out": ts, "work_hours": hours,
              "overtime_minutes": compute_overtime(ts, emp["shift_end"], record["date"], emp["shift_start"]),
              "half_day": hours < 4.50}
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None}, {"$set": update}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record is already punched out")
    record.update(update)
    return public_attendance(record)


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    valid_date(date)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "shift_start": 1, "shift_end": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    absent_statuses = {"ABSENT", "LEAVE"}
    presence_statuses = {"PRESENT", "WFH", "ON_DUTY"}
    new_status = body.status if "status" in supplied else record["status"]
    if new_status in absent_statuses and supplied.intersection({"punch_in", "punch_out"}):
        raise HTTPException(422, "punch times cannot be supplied with ABSENT or LEAVE")

    if new_status in absent_statuses:
        punch_in_value = punch_out_value = None
    else:
        old_in, old_out = record.get("punch_in"), record.get("punch_out")
        punch_in_value = epoch_datetime(body.punch_in) if "punch_in" in supplied else old_in
        punch_out_value = epoch_datetime(body.punch_out) if "punch_out" in supplied else old_out
        if new_status in presence_statuses and punch_in_value is None:
            raise HTTPException(422, "presence status requires punch_in")
        if punch_in_value is not None and attendance_date(punch_in_value, emp) != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if punch_out_value is not None:
            if punch_in_value is None:
                raise HTTPException(422, "punch_out requires punch_in")
            duration = (punch_out_value - punch_in_value).total_seconds()
            if duration <= 0 or duration > 24 * 60 * 60:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if new_status not in absent_statuses | presence_statuses:
        raise HTTPException(422, "invalid status")
    if new_status in absent_statuses:
        computed = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    elif punch_in_value is None:
        computed = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        late = compute_late_minutes(punch_in_value, emp["shift_start"], emp["shift_end"])
        if punch_out_value is None:
            computed = {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
        else:
            hours = compute_work_hours(punch_in_value, punch_out_value)
            computed = {"work_hours": hours, "late_minutes": late,
                        "overtime_minutes": compute_overtime(punch_out_value, emp["shift_end"], date, emp["shift_start"]),
                        "half_day": hours < 4.50}

    desired = {"status": new_status, "punch_in": punch_in_value, "punch_out": punch_out_value, **computed}
    prior = {"status": record.get("status"), "punch_in": record.get("punch_in"),
             "punch_out": record.get("punch_out"), "work_hours": record.get("work_hours"),
             "late_minutes": record.get("late_minutes", 0), "overtime_minutes": record.get("overtime_minutes", 0),
             "half_day": record.get("half_day", False)}
    changes = {key: {"from": prior[key], "to": value} for key, value in desired.items() if prior[key] != value}
    if not changes:
        raise HTTPException(422, "regularization does not change the record")

    history_entry = {"at": datetime.now(UTC), "by": body.regularized_by,
                     "reason": body.reason, "changes": changes}
    # Compare the observed values and history in the same atomic update. A concurrent
    # correction that changes this snapshot loses with 409 instead of dropping an audit entry.
    snapshot = {"_id": record["_id"], "emp_code": emp_code, "date": date,
                "status": record.get("status")}
    # Compare missing legacy fields as missing, rather than as their public defaults.
    # This preserves optimistic concurrency while allowing corrections to old records.
    for field in ("punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        snapshot[field] = record[field] if field in record else {"$exists": False}
    history_snapshot = record.get("history", [])
    snapshot["$or"] = [{"history": history_snapshot}, {"history": {"$exists": False}}] if "history" not in record else [{"history": history_snapshot}]
    set_values = {key: value for key, value in desired.items() if prior[key] != value}
    updated = db.attendance_logs.find_one_and_update(
        snapshot, {"$set": set_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record changed concurrently; retry regularization")
    return public_attendance(updated)
