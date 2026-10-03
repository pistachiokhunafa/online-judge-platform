import time

import pytest

import app as app_module
from engine import service as service_module


@pytest.fixture(scope="module")
def client():
    flask_app = app_module.create_app()  # also starts the judge workers
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


LOCAL = {"Host": "127.0.0.1:5050"}


def submit(client, code, problem_id="sum-two-integers", headers=LOCAL):
    return client.post("/api/submissions", json={"code": code, "problem_id": problem_id},
                       headers=headers)


def test_lists_problems_without_hidden_tests(client):
    res = client.get("/api/problems", headers=LOCAL)
    assert res.status_code == 200
    problems = res.get_json()
    assert {p["problem_id"] for p in problems} >= {"sum-two-integers", "reverse-string", "is-prime"}
    assert all("hidden_tests" not in p for p in problems)


@pytest.mark.parametrize("host", ["localhost:5050", "127.0.0.1", "[::1]:5050"])
def test_local_hosts_are_allowed(client, host):
    assert client.get("/api/problems", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:5050",
                                  "127.0.0.1.evil.com", "localhost.evil.com:5050"])
def test_other_hosts_are_rejected_dns_rebinding(client, host):
    assert client.get("/api/problems", headers={"Host": host}).status_code == 403
    assert submit(client, "print(1)", headers={"Host": host}).status_code == 403


def test_security_headers_are_set(client):
    res = client.get("/", headers=LOCAL)
    assert res.status_code == 200
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"


def test_static_files_cannot_escape_frontend_folder(client):
    for path in ["/../backend/app.py", "/..%2fbackend%2fapp.py", "/%2e%2e/run.py"]:
        assert client.get(path, headers=LOCAL).status_code in (400, 403, 404)


def test_rejects_non_json_and_bad_types(client):
    assert client.post("/api/submissions", data="print(1)", headers=LOCAL).status_code == 400
    assert client.post("/api/submissions", json=["x"], headers=LOCAL).status_code == 400
    assert client.post("/api/submissions", json={"code": 5, "problem_id": "sum-two-integers"},
                       headers=LOCAL).status_code == 400
    assert submit(client, "   ").status_code == 400
    assert submit(client, "print(1)", problem_id="nope").status_code == 400


def test_oversized_request_body_is_rejected(client):
    assert submit(client, "#" * (200 * 1024)).status_code == 413


def test_oversized_code_is_rejected(client):
    code = "#" * (service_module.MAX_CODE_BYTES + 1)  # under body limit, over code limit
    assert submit(client, code).status_code == 413


def test_full_queue_returns_503(client, monkeypatch):
    monkeypatch.setattr(app_module.judge_service.scheduler, "depth",
                        lambda: service_module.MAX_QUEUE_DEPTH)
    assert submit(client, "print(1)").status_code == 503


def test_unknown_submission_is_404(client):
    assert client.get("/api/submissions/999999999", headers=LOCAL).status_code == 404


def test_end_to_end_submission_is_judged(client):
    res = submit(client, "a, b = map(int, input().split())\nprint(a + b)")
    assert res.status_code == 201
    sid = res.get_json()["submission_id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        body = client.get(f"/api/submissions/{sid}", headers=LOCAL).get_json()
        if body["verdict"]:
            break
        time.sleep(0.1)
    assert body["verdict"] == "AC"
    assert len(body["tests"]) == body["tests_total"]

    dash = client.get("/api/dashboard", headers=LOCAL).get_json()
    assert dash["stats"]["test_cases_judged"] >= body["tests_total"]
