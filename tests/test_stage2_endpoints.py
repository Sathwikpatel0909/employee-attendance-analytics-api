from copy import deepcopy
from datetime import datetime, timezone
from threading import Barrier, Lock
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import main


class Collection:
    def __init__(self, docs=()):
        self.docs = [deepcopy(d) for d in docs]
        self.lock = Lock()
        self.read_barrier = None

    def find_one(self, query, projection=None, sort=None):
        with self.lock:
            found = [deepcopy(d) for d in self.docs if self._matches(d, query)]
        if sort and found:
            key, direction = sort[0]
            found.sort(key=lambda d: d.get(key), reverse=direction < 0)
        if self.read_barrier:
            self.read_barrier.wait(timeout=3)
        return found[0] if found else None

    def update_one(self, query, update):
        with self.lock:
            for doc in self.docs:
                if self._matches(doc, query):
                    doc.update(deepcopy(update["$set"]))
                    return type("Result", (), {"modified_count": 1})()
        return type("Result", (), {"modified_count": 0})()

    def find_one_and_update(self, query, update, return_document=None):
        with self.lock:
            for doc in self.docs:
                if self._matches(doc, query):
                    doc.update(deepcopy(update["$set"]))
                    doc.setdefault("history", []).append(deepcopy(update["$push"]["history"]))
                    return deepcopy(doc)
        return None

    @classmethod
    def _matches(cls, doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(cls._matches(doc, option) for option in expected):
                    return False
            elif isinstance(expected, dict) and "$exists" in expected:
                if (key in doc) != expected["$exists"]:
                    return False
            elif isinstance(expected, dict) and "$ne" in expected:
                if doc.get(key) == expected["$ne"]:
                    return False
                if "$lte" in expected and (doc.get(key) is None or doc[key] > expected["$lte"]):
                    return False
            elif doc.get(key) != expected:
                return False
        return True


class FakeDB:
    def __init__(self, employees, logs):
        self.employees = Collection(employees)
        self.attendance_logs = Collection(logs)


class DirectClient:
    """Call route functions with the same Pydantic parsing and HTTP error mapping."""
    @staticmethod
    def response(code, value):
        return type("Response", (), {"status_code": code, "json": staticmethod(lambda: deepcopy(value))})()

    def post(self, path, json):
        try:
            if path != "/attendance/punch-out":
                return self.response(404, {})
            body = main.PunchOutIn.model_validate(json)
            return self.response(200, main.punch_out(body))
        except ValidationError as exc:
            return self.response(422, {"detail": str(exc)})
        except HTTPException as exc:
            return self.response(exc.status_code, {"detail": exc.detail})

    def patch(self, path, json):
        try:
            _, _, emp_code, attendance_day = path.split("/")
            body = main.RegularizeIn.model_validate(json)
            return self.response(200, main.regularize_attendance(emp_code, attendance_day, body))
        except ValidationError as exc:
            return self.response(422, {"detail": str(exc)})
        except HTTPException as exc:
            return self.response(exc.status_code, {"detail": exc.detail})


def ms(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def employee(code="EMP0001", start="09:30", end="18:30"):
    return {"emp_code": code, "shift_start": start, "shift_end": end}


def log(code="EMP0001", day="2026-07-06", punch_in="2026-07-06T03:30:00", punch_out=None):
    return {"_id": code + day, "emp_code": code, "date": day, "status": "PRESENT",
            "punch_in": datetime.fromisoformat(punch_in).replace(tzinfo=timezone.utc),
            "punch_out": punch_out, "work_hours": None, "late_minutes": 0,
            "overtime_minutes": 0, "half_day": False, "history": []}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(main, "db", FakeDB([employee()], [log()]))
    return DirectClient()


def test_punch_out_work_hours_half_day_boundary_and_rounding(api):
    # 4h 29m 42s rounds half-up to 4.50, so this is not a half day.
    response = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T07:59:42")})
    assert response.status_code == 200
    assert response.json()["work_hours"] == 4.5
    assert response.json()["half_day"] is False
    assert response.json()["history"] == []


@pytest.mark.parametrize("end,expected", [("2026-07-06T13:29:00", 0), ("2026-07-06T13:30:00", 30)])
def test_punch_out_overtime_threshold(api, end, expected):
    response = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(end)})
    assert response.status_code == 200
    assert response.json()["overtime_minutes"] == expected


def test_punch_out_overnight_shift(api, monkeypatch):
    monkeypatch.setattr(main, "db", FakeDB([employee(start="22:00", end="06:00")], [
        log(day="2026-07-06", punch_in="2026-07-06T16:30:00")]))
    response = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T01:00:00")})
    assert response.status_code == 200
    assert response.json()["date"] == "2026-07-06"
    assert response.json()["overtime_minutes"] == 30


@pytest.mark.parametrize("body", [
    {"emp_code": "EMP0001", "punched_at": 1783312500},
    {"emp_code": "EMP0001", "punched_at": 1783312500000.0},
    {"emp_code": "EMP0001", "punched_at": "1783312500000"},
    {"emp_code": "EMP0001", "punched_at": None},
])
def test_punch_out_rejects_invalid_timestamps(api, body):
    assert api.post("/attendance/punch-out", json=body).status_code == 422


def test_punch_out_missing_employee_record_and_conflict(api, monkeypatch):
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    logs = main.db.attendance_logs
    monkeypatch.setattr(main.db, "attendance_logs", Collection([]))
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    monkeypatch.setattr(main.db, "attendance_logs", logs)
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T08:30:00")})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:00:00")}).status_code == 409


@pytest.mark.parametrize("end", ["2026-07-06T03:29:59", "2026-07-07T03:30:01"])
def test_punch_out_duration_bounds(api, end):
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(end)}).status_code == 422


def test_parallel_punch_out_has_one_winner(api, monkeypatch):
    logs = main.db.attendance_logs
    barrier = Barrier(2)
    logs.read_barrier = barrier
    timestamp = ms("2026-07-06T08:30:00")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": timestamp}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]


def test_regularization_appends_actual_changes_and_recomputes(api):
    response = api.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": ms("2026-07-06T04:45:00"), "reason": "corrected biometric time", "regularized_by": "hr.admin"})
    assert response.status_code == 200
    record = response.json()
    assert record["late_minutes"] == 45
    assert record["history"][-1]["by"] == "hr.admin"
    assert record["history"][-1]["reason"] == "corrected biometric time"
    assert record["history"][-1]["changes"]["punch_in"] == {"from": ms("2026-07-06T03:30:00"), "to": ms("2026-07-06T04:45:00")}
    assert record["history"][-1]["changes"]["late_minutes"] == {"from": 0, "to": 45}
    assert "status" not in record["history"][-1]["changes"]


def test_regularization_supports_legacy_missing_defaults(api):
    record = main.db.attendance_logs.docs[0]
    for field in ("punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day", "history"):
        record.pop(field, None)

    response = api.patch("/attendance/EMP0001/2026-07-06", json={
        "status": "WFH", "reason": "corrected work location", "regularized_by": "hr"})

    assert response.status_code == 200
    assert response.json()["status"] == "WFH"
    assert response.json()["history"][-1]["changes"] == {"status": {"from": "PRESENT", "to": "WFH"}}


def test_regularization_absent_clears_times_and_derived_values(api):
    response = api.patch("/attendance/EMP0001/2026-07-06", json={"status": "LEAVE", "reason": "approved leave", "regularized_by": "hr"})
    assert response.status_code == 200
    assert response.json()["punch_in"] is None
    assert response.json()["late_minutes"] == 0
    assert set(response.json()["history"][-1]["changes"]) == {"status", "punch_in"}


@pytest.mark.parametrize("payload", [
    {"status": "ABSENT", "punch_in": 1783312500000, "reason": "correction", "regularized_by": "hr"},
    {"punch_in": 1783312500000.0, "reason": "correction", "regularized_by": "hr"},
    {"punch_in": None, "reason": "correction", "regularized_by": "hr"},
    {"reason": "no", "regularized_by": "hr"},
])
def test_regularization_validation(api, payload):
    assert api.patch("/attendance/EMP0001/2026-07-06", json=payload).status_code == 422


def test_regularization_missing_and_noop(api):
    assert api.patch("/attendance/EMP9999/2026-07-06", json={"reason": "valid reason", "regularized_by": "hr"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-07-07", json={"reason": "valid reason", "regularized_by": "hr"}).status_code == 404
    assert api.patch("/attendance/EMP0001/2026-07-06", json={"reason": "valid reason", "regularized_by": "hr"}).status_code == 422


def test_parallel_regularizations_keep_history_or_return_conflict(api, monkeypatch):
    logs = main.db.attendance_logs
    logs.read_barrier = Barrier(2)
    def request(actor):
        return api.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": ms("2026-07-06T03:30:01" if actor == "a" else "2026-07-06T03:30:02"),
            "reason": "correction by " + actor, "regularized_by": actor})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(request, ["a", "b"]))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(logs.docs[0]["history"]) == 1
