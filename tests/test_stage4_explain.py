from copy import deepcopy

import pytest
from fastapi import HTTPException
from pymongo.errors import PyMongoError

from app import main


class ExplainCollection:
    def __init__(self, docs=()):
        self.docs = deepcopy(list(docs))
        self.indexes = {}
        self.index_calls = []

    def find_one(self, query, projection=None):
        return next((deepcopy(doc) for doc in self.docs
                     if all(doc.get(key) == value for key, value in query.items())), None)

    def create_index(self, keys, **options):
        name = options["name"]
        self.index_calls.append((keys, deepcopy(options)))
        self.indexes[name] = (keys, deepcopy(options))
        return name


class ExplainDB:
    def __init__(self):
        self.employees = ExplainCollection([{
            "_id": "internal", "emp_code": "EMP0001", "department": "Engineering",
            "joined_on": "2026-01-05",
        }])
        self.attendance_logs = ExplainCollection()
        self.commands = []
        self.command_result = {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}},
                               "executionStats": {"totalDocsExamined": 12}}
        self.command_error = None

    def command(self, name, operation, **kwargs):
        self.commands.append((name, deepcopy(operation), deepcopy(kwargs)))
        if self.command_error:
            raise self.command_error
        return deepcopy(self.command_result)


@pytest.fixture
def explain_db(monkeypatch):
    db = ExplainDB()
    monkeypatch.setattr(main, "db", db)
    return db


@pytest.mark.parametrize("endpoint,params,collection,expected_operation", [
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31",
                          "status": "PRESENT", "page": 2, "page_size": 5}, "attendance_logs", "find"),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "employees", "aggregate"),
    ("department_summary", {"month": "2026-07", "department": "Engineering"}, "employees", "aggregate"),
    ("late_leaderboard", {"month": "2026-07", "limit": 4, "department": "Engineering"}, "attendance_logs", "aggregate"),
    ("department_trend", {"department": "Engineering", "from_date": "2026-07-01", "to_date": "2026-07-02"}, "employees", "aggregate"),
])
def test_explain_runs_execution_stats_on_same_operation(explain_db, endpoint, params, collection, expected_operation):
    result = main.explain_endpoint(endpoint, **params)
    assert result == {"endpoint": endpoint, "collection": collection, "explain": explain_db.command_result}
    name, operation, options = explain_db.commands[-1]
    assert name == "explain"
    assert options == {"verbosity": "executionStats"}
    assert operation[expected_operation] == collection
    if endpoint == "attendance_list":
        assert operation["filter"] == {
            "emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}, "status": "PRESENT"
        }
        assert operation["sort"] == {"date": -1, "emp_code": 1}
        assert operation["skip"] == 5 and operation["limit"] == 5
        assert operation["hint"] == "uq_attendance_employee_date"
    elif endpoint == "employee_monthly":
        assert operation["pipeline"] == main.build_employee_monthly_pipeline("EMP0001", "2026-07")
    elif endpoint == "department_summary":
        assert operation["pipeline"] == main.build_department_summary_pipeline("2026-07", "Engineering")
    elif endpoint == "late_leaderboard":
        assert operation["pipeline"] == main.build_late_leaderboard_pipeline("2026-07", 4, "Engineering")
    else:
        assert operation["pipeline"] == main.build_department_trend_pipeline(
            "Engineering", "2026-07-01", "2026-07-02", 2)
    if endpoint == "employee_monthly":
        assert operation["hint"] == "uq_employees_emp_code"
    elif endpoint == "department_summary":
        assert operation["hint"] == "ix_employees_department_joined_on"
    elif endpoint == "late_leaderboard":
        assert operation["hint"] == "ix_attendance_date_code"
    elif endpoint == "department_trend":
        assert operation["hint"] == "ix_employees_department_code"


def test_explain_rejects_unsupported_identifiers_and_missing_parameters(explain_db):
    with pytest.raises(HTTPException) as error:
        main.explain_endpoint("admin", month="2026-07")
    assert error.value.status_code == 422
    for endpoint in ("employee_monthly", "department_summary", "late_leaderboard", "department_trend"):
        with pytest.raises(HTTPException) as error:
            main.explain_endpoint(endpoint)
        assert error.value.status_code == 422
    assert explain_db.commands == []


@pytest.mark.parametrize("endpoint,params,missing_collection", [
    ("employee_monthly", {"emp_code": "EMP9999", "month": "2026-07"}, "employee"),
    ("department_trend", {"department": "Unknown", "from_date": "2026-07-01", "to_date": "2026-07-02"}, "department"),
])
def test_explain_returns_not_found_for_missing_referenced_resources(explain_db, endpoint, params, missing_collection):
    explain_db.employees.docs = []
    with pytest.raises(HTTPException) as error:
        main.explain_endpoint(endpoint, **params)
    assert error.value.status_code == 404
    assert error.value.detail == f"{missing_collection} not found"
    assert explain_db.commands == []


@pytest.mark.parametrize("params", [
    {"date_from": "2026-07-31", "date_to": "2026-07-01"},
    {"status": "UNKNOWN"},
])
def test_explain_rejects_invalid_attendance_filters(explain_db, params):
    with pytest.raises(HTTPException) as error:
        main.explain_endpoint("attendance_list", **params)
    assert error.value.status_code == 422
    assert explain_db.commands == []


def test_explain_translates_mongodb_errors_without_fabricating_a_plan(explain_db):
    explain_db.command_error = PyMongoError("simulated command failure")
    with pytest.raises(HTTPException) as error:
        main.explain_endpoint("attendance_list")
    assert error.value.status_code == 503
    assert error.value.detail == "MongoDB explain failed"


def test_startup_index_creation_is_idempotent_by_name_and_required_patterns(explain_db):
    main.create_indexes()
    first_employee_indexes = deepcopy(explain_db.employees.indexes)
    first_attendance_indexes = deepcopy(explain_db.attendance_logs.indexes)
    main.create_indexes()
    assert explain_db.employees.indexes == first_employee_indexes
    assert explain_db.attendance_logs.indexes == first_attendance_indexes
    assert len(first_employee_indexes) == 4
    assert len(first_attendance_indexes) == 3
    assert "ix_employees_joined_on" in first_employee_indexes
    assert "ix_employees_department_joined_on" in first_employee_indexes
    assert "uq_attendance_employee_date" in first_attendance_indexes
    assert "ix_attendance_date_code" in first_attendance_indexes
    assert "ix_attendance_employee_punch_in" in first_attendance_indexes
    assert "ix_attendance_employee_date_desc" not in first_attendance_indexes
