"""
app.py — the thin web layer. Every route here does the same three
things: read the request, ask `judge_service` to do the real work, and
hand back JSON (or, for "/", the frontend's index.html). None of the
actual judging logic lives in this file on purpose — see `engine/` for
that — which keeps the web framework easy to swap out later if you ever
wanted to.
"""

from __future__ import annotations

import os

from flask import Flask, abort, jsonify, request, send_from_directory

from engine.problems import get_problem, list_problems
from engine.service import JudgeService, SubmissionRejected

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BACKEND_DIR), "frontend")

app = Flask(__name__)
# Reject request bodies over 128 KB outright (Flask answers 413).
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024

# Only these hostnames may be used to reach the server. This blocks
# "DNS rebinding": a malicious website pointing its own domain at
# 127.0.0.1 so your browser sends it requests to this local judge. Add
# more (comma-separated) with OJ_ALLOWED_HOSTS if you deploy it elsewhere.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"} | {
    h.strip().lower()
    for h in os.environ.get("OJ_ALLOWED_HOSTS", "").split(",") if h.strip()
}

CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "connect-src 'self'",
    "img-src 'self' data:",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])

# One JudgeService for the whole process — it owns the scheduler, the
# worker threads, and the stats tracker. Everything below just talks to it.
judge_service = JudgeService(submission_workers=3, test_case_workers=8)


# ---------------------------------------------------------------- security --
def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):                 # IPv6 literal, e.g. [::1]:5050
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


@app.before_request
def reject_unknown_hosts():
    if _hostname(request.host) not in ALLOWED_HOSTS:
        abort(403)


@app.after_request
def add_security_headers(response):
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


@app.errorhandler(403)
def forbidden(_error):
    return jsonify({"error": "forbidden host"}), 403


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "request body too large"}), 413


# ---------------------------------------------------------------- frontend --
@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    """Serves style.css, app.js, etc. straight out of /frontend."""
    return send_from_directory(FRONTEND_DIR, filename)


# --------------------------------------------------------------------- API --
@app.get("/api/problems")
def api_list_problems():
    problems = [
        {
            "problem_id": p.problem_id,
            "title": p.title,
            "statement": p.statement,
            "sample_input": p.sample_input,
            "sample_output": p.sample_output,
            "starter_code": p.starter_code,
            "test_count": len(p.hidden_tests),
        }
        for p in list_problems()
    ]
    return jsonify(problems)


@app.post("/api/submissions")
def api_create_submission():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected a JSON object body"}), 400
    code = payload.get("code", "")
    problem_id = payload.get("problem_id", "")

    if not isinstance(code, str) or not isinstance(problem_id, str):
        return jsonify({"error": "code and problem_id must be strings"}), 400
    if not code.strip():
        return jsonify({"error": "code must not be empty"}), 400

    try:
        submission = judge_service.submit(code=code, problem_id=problem_id)
    except KeyError:
        return jsonify({"error": "unknown problem_id"}), 400
    except SubmissionRejected as rejected:
        return jsonify({"error": str(rejected)}), rejected.http_status

    tests_total = len(get_problem(problem_id).hidden_tests)
    return jsonify(submission.to_dict(tests_total=tests_total)), 201


@app.get("/api/submissions/<int:submission_id>")
def api_get_submission(submission_id: int):
    submission = judge_service.get_submission(submission_id)
    if submission is None:
        return jsonify({"error": "submission not found"}), 404

    tests_total = len(get_problem(submission.problem_id).hidden_tests)
    return jsonify(submission.to_dict(tests_total=tests_total))


@app.get("/api/dashboard")
def api_dashboard():
    return jsonify(judge_service.dashboard())


def create_app() -> Flask:
    """Starts the background workers and returns the configured app.
    Kept separate from module import time so tests can create an app
    without immediately spinning up worker threads, if ever needed."""
    judge_service.start()
    return app


if __name__ == "__main__":
    create_app()
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
