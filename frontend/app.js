/**
 * app.js — everything the page does, in four parts:
 *
 *   1. Problem loading      — fill the dropdown, show the statement.
 *   2. Submitting code      — POST it, then poll the submission until
 *                             it reaches a final verdict.
 *   3. The dashboard loop   — poll /api/dashboard on a timer and repaint
 *                             the queue lane, stats, and activity feed.
 *   4. Small render helpers — pure functions that turn JSON into DOM.
 *
 * No framework, no build step — just fetch() and direct DOM updates,
 * kept deliberately simple so the whole flow reads top to bottom.
 */

const state = {
  problems: [],
  currentProblem: null,
  activeSubmissionId: null,
  pollTimer: null,
};

const el = {
  problemSelect: document.getElementById("problem-select"),
  problemStatement: document.getElementById("problem-statement"),
  codeEditor: document.getElementById("code-editor"),
  submitBtn: document.getElementById("submit-btn"),
  resultCard: document.getElementById("result-card"),
  resultId: document.getElementById("result-id"),
  resultVerdict: document.getElementById("result-verdict"),
  testGrid: document.getElementById("test-grid"),
  resultDetail: document.getElementById("result-detail"),
  workerSummary: document.getElementById("worker-summary"),
  queueLane: document.getElementById("queue-lane"),
  statGrid: document.getElementById("stat-grid"),
  verdictBars: document.getElementById("verdict-bars"),
  activityFeed: document.getElementById("activity-feed"),
};

const TERMINAL_VERDICTS = new Set(["AC", "WA", "TLE", "MLE", "RE"]);

// ============================================================== 1. Problems
async function loadProblems() {
  const response = await fetch("/api/problems");
  state.problems = await response.json();

  el.problemSelect.innerHTML = "";
  for (const problem of state.problems) {
    const option = document.createElement("option");
    option.value = problem.problem_id;
    option.textContent = problem.title;
    el.problemSelect.appendChild(option);
  }

  selectProblem(state.problems[0].problem_id);
}

function selectProblem(problemId) {
  const problem = state.problems.find((p) => p.problem_id === problemId);
  if (!problem) return;

  state.currentProblem = problem;
  el.problemSelect.value = problemId;
  el.codeEditor.value = problem.starter_code;

  el.problemStatement.innerHTML = `
    <h3>${escapeHtml(problem.title)}</h3>
    <p>${escapeHtml(problem.statement)}</p>
    <div class="sample-io">
      <div class="io-block">
        <span class="io-label">Sample input</span>
        <pre>${escapeHtml(problem.sample_input)}</pre>
      </div>
      <div class="io-block">
        <span class="io-label">Sample output</span>
        <pre>${escapeHtml(problem.sample_output)}</pre>
      </div>
    </div>
  `;
}

el.problemSelect.addEventListener("change", (event) => {
  selectProblem(event.target.value);
});

// Tab key inserts spaces instead of jumping focus out of the editor.
el.codeEditor.addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  event.preventDefault();
  const { selectionStart, selectionEnd, value } = el.codeEditor;
  el.codeEditor.value = value.slice(0, selectionStart) + "    " + value.slice(selectionEnd);
  el.codeEditor.selectionStart = el.codeEditor.selectionEnd = selectionStart + 4;
});

// ============================================================ 2. Submitting
el.submitBtn.addEventListener("click", submitCode);

async function submitCode() {
  if (!state.currentProblem) return;

  el.submitBtn.disabled = true;
  el.submitBtn.textContent = "Submitting…";

  try {
    const response = await fetch("/api/submissions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        problem_id: state.currentProblem.problem_id,
        code: el.codeEditor.value,
      }),
    });

    if (!response.ok) {
      const problem = await response.json();
      throw new Error(problem.error || "Submission failed");
    }

    const submission = await response.json();
    state.activeSubmissionId = submission.submission_id;
    el.resultCard.hidden = false;
    renderSubmission(submission);
    pollSubmission(submission.submission_id);
  } catch (error) {
    alert(error.message);
  } finally {
    el.submitBtn.disabled = false;
    el.submitBtn.textContent = "Submit";
  }
}

function pollSubmission(submissionId) {
  const tick = async () => {
    const response = await fetch(`/api/submissions/${submissionId}`);
    if (!response.ok) return;
    const submission = await response.json();
    renderSubmission(submission);

    if (!TERMINAL_VERDICTS.has(submission.status)) {
      setTimeout(tick, 500);
    }
  };
  tick();
}

// ========================================================== 3. Dashboard loop
async function refreshDashboard() {
  try {
    const response = await fetch("/api/dashboard");
    const dashboard = await response.json();

    el.workerSummary.textContent =
      `${dashboard.submission_workers} judges · ${dashboard.test_case_workers} test runners · ` +
      `${dashboard.queue_depth} queued`;

    renderQueueLane(dashboard.queue);
    renderStats(dashboard.stats);
    renderActivity(dashboard.recent_submissions);
  } catch (error) {
    el.workerSummary.textContent = "offline";
  } finally {
    setTimeout(refreshDashboard, 1000);
  }
}

// ========================================================== 4. Render helpers
function renderSubmission(submission) {
  el.resultId.textContent = `submission #${submission.submission_id}`;

  const shownVerdict = submission.verdict || submission.status;
  el.resultVerdict.textContent = shownVerdict;
  el.resultVerdict.dataset.verdict = shownVerdict;

  const total = submission.tests_total || 0;
  const chips = [];
  for (let i = 0; i < total; i++) {
    const test = submission.tests[i];
    const cls = test ? test.verdict : "pending";
    const label = test ? (test.verdict === "AC" ? "✓" : "✕") : (i + 1);
    chips.push(`<div class="test-chip ${cls}" title="test ${i + 1}">${label}</div>`);
  }
  el.testGrid.innerHTML = chips.join("");

  const firstFailure = submission.tests.find((t) => t.verdict !== "AC");
  el.resultDetail.textContent = firstFailure
    ? `Test failed (${firstFailure.verdict}): ${firstFailure.detail}`
    : submission.status === "AC"
    ? "All hidden tests passed."
    : "";
}

function renderQueueLane(queue) {
  if (queue.length === 0) {
    el.queueLane.innerHTML = '<p class="empty-note">Queue is empty.</p>';
    return;
  }

  const MAX_WAIT_FOR_BAR_S = 8; // bar reaches full height at 8s of waiting
  el.queueLane.innerHTML = queue
    .map((item, i) => {
      const heightPct = Math.min(100, (item.waiting_s / MAX_WAIT_FOR_BAR_S) * 100);
      return `
        <div class="queue-chip ${i === 0 ? "next-up" : ""}">
          <div class="chip-track">
            <div class="chip-fill" style="height:${heightPct}%"></div>
          </div>
          <span class="chip-id">#${item.submission_id}</span>
        </div>
      `;
    })
    .join("");
}

function renderStats(stats) {
  el.statGrid.innerHTML = `
    <div><dt>Mean runtime</dt><dd>${stats.mean_ms} ms</dd></div>
    <div><dt>P95 runtime</dt><dd>${stats.p95_ms} ms</dd></div>
    <div><dt>Tests judged</dt><dd>${stats.test_cases_judged}</dd></div>
    <div><dt>Std dev (σ)</dt><dd>${stats.stdev_ms} ms</dd></div>
  `;

  const total = Object.values(stats.verdicts).reduce((sum, n) => sum + n, 0) || 1;
  const order = ["AC", "WA", "TLE", "MLE", "RE"];
  el.verdictBars.innerHTML = order
    .map((verdict) => {
      const count = stats.verdicts[verdict] || 0;
      const pct = Math.round((count / total) * 100);
      return `
        <div class="verdict-bar-row">
          <span>${verdict}</span>
          <span class="verdict-bar-track">
            <span class="verdict-bar-fill ${verdict}" style="width:${pct}%"></span>
          </span>
          <span>${count}</span>
        </div>
      `;
    })
    .join("");
}

function renderActivity(recent) {
  if (recent.length === 0) {
    el.activityFeed.innerHTML = '<li class="empty-note">No submissions yet.</li>';
    return;
  }
  el.activityFeed.innerHTML = recent
    .slice(0, 8)
    .map((s) => {
      const badge = s.verdict || s.status;
      return `
        <li>
          <span class="activity-id">#${s.submission_id} · ${escapeHtml(s.problem_id)}</span>
          <span class="activity-badge ${badge}">${badge}</span>
        </li>
      `;
    })
    .join("");
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ==================================================================== boot
loadProblems();
refreshDashboard();
