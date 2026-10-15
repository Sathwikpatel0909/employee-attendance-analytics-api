from copy import deepcopy

import pytest
from bson import BSON
from fastapi import HTTPException

from app import main


class AggregateCollection:
    def __init__(self, documents=(), result=()):
        self.documents = deepcopy(list(documents))
        self.result = deepcopy(list(result))
        self.pipeline = None
        self.aggregate_options = None

    def find_one(self, query, projection=None):
        return next((deepcopy(doc) for doc in self.documents
                     if all(doc.get(key) == value for key, value in query.items())), None)

    def aggregate(self, pipeline, **options):
        self.pipeline = deepcopy(pipeline)
        self.aggregate_options = deepcopy(options)
        # Exercise PyMongo BSON encoding for the actual pipeline constants.
        BSON.encode({"pipeline": self.pipeline})
        return deepcopy(self.result)


class AnalyticsDB:
    def __init__(self, employees=(), employee_result=(), attendance_result=()):
        self.employees = AggregateCollection(employees, employee_result)
        self.attendance_logs = AggregateCollection((), attendance_result)


def operators(value):
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.startswith("$"):
                found.append(key)
            found.extend(operators(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(operators(child))
    return found


def employee(code="EMP0001", department="Engineering", joined_on="2026-01-05"):
    return {"emp_code": code, "department": department, "joined_on": joined_on}


@pytest.fixture
def analytics(monkeypatch):
    db = AnalyticsDB([employee()])
    monkeypatch.setattr(main, "db", db)
    return db


def test_employee_monthly_metrics_pipeline_and_response(analytics):
    analytics.employees.result = [{
        "emp_code": "EMP0001", "month": "2026-07", "working_days": 23,
        "present_days": 20.5, "leave_days": 1, "late_count": 2,
        "total_late_minutes": 55, "total_overtime_minutes": 30, "attendance_pct": 89.1304,
    }]
    result = main.employee_monthly("EMP0001", "2026-07")
    assert result["present_days"] == 20.5
    assert result["attendance_pct"] == 89.1304
    ops = operators(analytics.employees.pipeline)
    assert "$lookup" in ops and "$dateDiff" in ops and "$range" in ops and "$reduce" in ops
    assert analytics.employees.pipeline[0] == {"$match": {"emp_code": "EMP0001"}}
    # Month filter uses inclusive July 1 and exclusive August 1 calendar strings.
    assert "2026-07-01" in str(analytics.employees.pipeline)
    assert "2026-08-01" in str(analytics.employees.pipeline)


def test_employee_monthly_empty_logs_and_missing_employee(analytics):
    analytics.employees.result = [{
        "emp_code": "EMP0001", "month": "2026-02", "working_days": 20,
        "present_days": 0.0, "leave_days": 0, "late_count": 0,
        "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 0.0,
    }]
    assert main.employee_monthly("EMP0001", "2026-02")["attendance_pct"] == 0.0
    analytics.employees.documents = []
    with pytest.raises(HTTPException) as error:
        main.employee_monthly("EMP4040", "2026-02")
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        main.month_period("2026-13")
    assert error.value.status_code == 422


def test_department_summary_includes_zero_log_headcount_and_rounds(analytics):
    analytics.employees.result = [{
        "department": "Engineering", "headcount": 2, "present_days": 1.5,
        "avg_work_hours": 8.13, "late_count": 1, "total_late_minutes": 16,
        "leave_count": 0, "on_duty_count": 1,
    }]
    result = main.department_summary("2026-07", "Engineering")
    assert result == {"month": "2026-07", "items": analytics.employees.result}
    pipeline = analytics.employees.pipeline
    assert pipeline[0]["$match"] == {"joined_on": {"$lt": "2026-08-01"}, "department": "Engineering"}
    ops = operators(pipeline)
    assert "$lookup" in ops and "$group" in ops and "$reduce" in ops
    assert "avg_work_hours" in str(pipeline)
    assert main.department_summary("2026-07")["items"] == analytics.employees.result


def test_department_summary_empty_results_and_month_boundary(analytics):
    analytics.employees.result = []
    assert main.department_summary("2026-12")["items"] == []
    pipeline = analytics.employees.pipeline
    assert "2027-01-01" in str(pipeline)


def test_leaderboard_ranks_ties_before_cutoff_and_filters_department(analytics):
    analytics.attendance_logs.result = [
        {"rank": 1, "emp_code": "EMP0001", "name": "A", "department": "Engineering", "total_late_minutes": 90, "late_count": 3},
        {"rank": 2, "emp_code": "EMP0002", "name": "B", "department": "Engineering", "total_late_minutes": 60, "late_count": 2},
        {"rank": 2, "emp_code": "EMP0003", "name": "C", "department": "Engineering", "total_late_minutes": 60, "late_count": 1},
    ]
    result = main.late_leaderboard("2026-07", 2, "Engineering")
    assert len(result["items"]) == 3  # both rank-2 employees survive limit=2
    pipeline = analytics.attendance_logs.pipeline
    rank_at = next(i for i, stage in enumerate(pipeline) if "$setWindowFields" in stage)
    cutoff_at = next(i for i, stage in enumerate(pipeline) if stage.get("$match", {}).get("rank"))
    assert rank_at < cutoff_at
    assert pipeline[cutoff_at] == {"$match": {"rank": {"$lte": 2}}}
    assert "$rank" in operators(pipeline)
    assert "Engineering" in str(pipeline)


def test_leaderboard_empty_and_unfiltered_department(analytics):
    analytics.attendance_logs.result = []
    assert main.late_leaderboard("2026-07", 10)["items"] == []
    pipeline_text = str(analytics.attendance_logs.pipeline)
    assert "2026-07-01" in pipeline_text and "2026-08-01" in pipeline_text


def test_trend_fills_weekends_and_gaps_in_mongo_pipeline(analytics):
    analytics.employees.result = [
        {"date": "2026-07-03", "is_working_day": True, "headcount": 1, "present_count": 1.0,
         "late_count": 0, "attendance_rate": 1.0, "moving_avg_7d": 1.0},
        {"date": "2026-07-04", "is_working_day": False, "headcount": 1, "present_count": 0.0,
         "late_count": 0, "attendance_rate": None, "moving_avg_7d": 1.0},
        {"date": "2026-07-05", "is_working_day": False, "headcount": 1, "present_count": 0.0,
         "late_count": 0, "attendance_rate": None, "moving_avg_7d": 1.0},
        {"date": "2026-07-06", "is_working_day": True, "headcount": 1, "present_count": 0.0,
         "late_count": 0, "attendance_rate": 0.0, "moving_avg_7d": 0.5},
    ]
    result = main.department_trend("Engineering", "2026-07-03", "2026-07-06")
    assert len(result["items"]) == 4
    assert [row["is_working_day"] for row in result["items"]] == [True, False, False, True]
    ops = operators(analytics.employees.pipeline)
    assert "$range" in ops and "$dateAdd" in ops and "$lookup" in ops
    assert "$setWindowFields" in ops and "-6" in str(analytics.employees.pipeline)


def test_trend_empty_days_range_validation_and_unknown_department(analytics):
    analytics.employees.result = []
    assert main.department_trend("Engineering", "2026-07-04", "2026-07-05")["items"] == []
    with pytest.raises(HTTPException) as error:
        main.department_trend("Engineering", "2026-07-05", "2026-07-04")
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        main.department_trend("Engineering", "2026-01-01", "2026-04-03")
    assert error.value.status_code == 422
    analytics.employees.documents = []
    with pytest.raises(HTTPException) as error:
        main.department_trend("Unknown", "2026-07-01", "2026-07-02")
    assert error.value.status_code == 404
