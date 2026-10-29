const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

let sessions = [];

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((d) => `${d.loc.slice(1).join(".")}: ${d.msg}`).join("; ")
      : data.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), isError ? 6000 : 2500);
}

async function run(action, successMessage, onError = (message) => toast(message, true)) {
  try {
    await action();
  } catch (err) {
    onError(err.message);
    return false;
  }
  if (successMessage) toast(successMessage);
  await refresh().catch((err) => toast(err.message, true));
  return true;
}

// ---------- shared match form fields ----------

function renderMatchFields(container, match = null) {
  const target = match?.points_to_win ?? 21;
  container.innerHTML = `
    <label>Games to
      <select class="points-to-win">
        ${[21, 15, 11].map((p) => `<option value="${p}" ${p === target ? "selected" : ""}>${p} points</option>`).join("")}
      </select>
    </label>
    <div class="game-row">
      <span>Score</span>
      <input type="number" min="0" max="30" class="my" placeholder="Us" value="${esc(match?.my_score)}" required>
      <span>–</span>
      <input type="number" min="0" max="30" class="opp" placeholder="Them" value="${esc(match?.opp_score)}" required>
    </div>
    <label>Notes <input type="text" class="match-notes" placeholder="Optional" value="${esc(match?.notes)}"></label>`;
}

function readMatchFields(container) {
  const my = $(".my", container).value.trim();
  const opp = $(".opp", container).value.trim();
  if (my === "" || opp === "") throw new Error("Enter both scores");
  return {
    my_score: Number(my),
    opp_score: Number(opp),
    points_to_win: Number($(".points-to-win", container).value),
    notes: $(".match-notes", container).value,
  };
}

// ---------- sessions view ----------

const formatHours = (hours) => `${hours}h`;

function renderSessions() {
  const list = $("#sessionsList");
  if (!sessions.length) {
    list.innerHTML = `<p class="empty">No sessions yet. Head to <strong>Log</strong> to add your first one.</p>`;
    return;
  }
  list.innerHTML = sessions
    .map((s) => {
      const matches = s.matches.length
        ? s.matches
            .map(
              (m, i) => `<li class="match ${m.result}">
                <span class="match-no">Match ${i + 1}</span>
                <span class="score">${esc(m.score)}</span>
                <span class="result">${m.result.toUpperCase()}</span>
                ${m.points_to_win !== 21 ? `<span class="pill">to ${m.points_to_win}</span>` : ""}
                <span class="notes">${esc(m.notes)}</span>
                <span class="actions">
                  <button class="link" data-action="edit-match" data-id="${m.id}">Edit</button>
                  <button class="link danger" data-action="delete-match" data-id="${m.id}">Delete</button>
                </span>
              </li>`
            )
            .join("")
        : `<li class="empty">No matches logged.</li>`;
      return `<article class="session-card">
        <div class="session-head">
          <div>
            <h3>${esc(s.session_date)} · ${esc(s.venue)} ${s.is_sample ? `<span class="pill sample">sample</span>` : ""}</h3>
            <p class="meta">${formatHours(s.duration_hours)} · ${s.wins}W – ${s.losses}L${s.notes ? ` · ${esc(s.notes)}` : ""}</p>
          </div>
          <div class="actions">
            <button class="secondary" data-action="add-match" data-id="${s.id}">+ Match</button>
            <button class="secondary" data-action="edit-session" data-id="${s.id}">Edit</button>
            <button class="danger" data-action="delete-session" data-id="${s.id}">Delete</button>
          </div>
        </div>
        <ul class="matches">${matches}</ul>
      </article>`;
    })
    .join("");
}

function findMatch(id) {
  for (const s of sessions) {
    const m = s.matches.find((x) => x.id === id);
    if (m) return m;
  }
  return null;
}

$("#sessionsList").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  switch (btn.dataset.action) {
    case "delete-session": {
      const s = sessions.find((x) => x.id === id);
      if (confirm(`Delete the ${s.session_date} session at ${s.venue} and its ${s.matches.length} match(es)?`)) {
        await run(() => api(`/api/sessions/${id}`, { method: "DELETE" }), "Session deleted");
      }
      break;
    }
    case "delete-match":
      if (confirm("Delete this match?")) {
        await run(() => api(`/api/matches/${id}`, { method: "DELETE" }), "Match deleted");
      }
      break;
    case "edit-session":
      openSessionDialog(sessions.find((x) => x.id === id));
      break;
    case "edit-match":
      openMatchDialog({ match: findMatch(id) });
      break;
    case "add-match":
      openMatchDialog({ sessionId: id });
      break;
  }
});

// ---------- edit dialog ----------

const dialog = $("#editDialog");
let dialogSubmit = null;

function openDialog(title, fieldsHtml, onSubmit) {
  $("#editTitle").textContent = title;
  $("#editFields").innerHTML = fieldsHtml;
  $("#editError").hidden = true;
  dialogSubmit = onSubmit;
  dialog.showModal();
}

function openSessionDialog(s) {
  openDialog(
    "Edit session",
    `<div class="grid-form">
      <label>Date <input name="session_date" type="date" value="${esc(s.session_date)}" required></label>
      <label>Venue <input name="venue" type="text" list="venueOptions" value="${esc(s.venue)}" required></label>
      <label>Duration (hours) <input name="duration_hours" type="number" min="1" max="24" step="1" value="${Number.isInteger(s.duration_hours) ? s.duration_hours : ""}" required></label>
      <label class="wide">Notes <input name="notes" type="text" value="${esc(s.notes)}"></label>
    </div>`,
    () => api(`/api/sessions/${s.id}`, { method: "PUT", body: readSessionForm($("#editForm")) })
  );
}

function openMatchDialog({ match = null, sessionId = null }) {
  openDialog(match ? "Edit match" : "Add match", `<div class="match-fields"></div>`, () => {
    const body = readMatchFields($("#editFields .match-fields"));
    return match
      ? api(`/api/matches/${match.id}`, { method: "PUT", body })
      : api(`/api/sessions/${sessionId}/matches`, { method: "POST", body });
  });
  renderMatchFields($("#editFields .match-fields"), match);
}

$("#editForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  // The modal dialog sits above the toast, so show errors inside it.
  const showError = (message) => {
    $("#editError").textContent = message;
    $("#editError").hidden = false;
  };
  if (await run(dialogSubmit, "Saved", showError)) dialog.close();
});
$("#editCancel").addEventListener("click", () => dialog.close());

// ---------- log view ----------

function readSessionForm(form) {
  return {
    session_date: form.elements.session_date.value,
    venue: form.elements.venue.value,
    duration_hours: Number(form.elements.duration_hours.value),
    notes: form.elements.notes.value,
  };
}

$("#sessionForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  let created;
  const ok = await run(async () => {
    created = await api("/api/sessions", { method: "POST", body: readSessionForm(form) });
  }, "Session saved — now add its matches");
  if (ok) {
    form.reset();
    form.elements.session_date.value = today();
    $("#matchSessionSelect").value = created.id;
  }
});

$("#matchForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fields = $("#matchForm .match-fields");
  const sessionId = $("#matchSessionSelect").value;
  const ok = await run(
    () => api(`/api/sessions/${sessionId}/matches`, { method: "POST", body: readMatchFields(fields) }),
    "Match saved"
  );
  if (ok) {
    renderMatchFields(fields);
    $("#matchSessionSelect").value = sessionId;
  }
});

function renderLogOptions(venues) {
  const select = $("#matchSessionSelect");
  const previous = select.value;
  select.innerHTML = sessions.length
    ? sessions.map((s) => `<option value="${s.id}">${esc(s.session_date)} · ${esc(s.venue)}</option>`).join("")
    : `<option value="" disabled selected>Create a session first</option>`;
  if (sessions.some((s) => String(s.id) === previous)) select.value = previous;
  $("#venueOptions").innerHTML = venues.map((v) => `<option value="${esc(v)}">`).join("");
}

function today() {
  const d = new Date();
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}

// ---------- stats view ----------

const pct = (v) => (v === null ? "–" : `${v}%`);

function renderStats(overall, venues, trend) {
  const cards = [
    ["Sessions", overall.sessions],
    ["Hours played", overall.hours_played],
    ["Matches (W-L)", `${overall.wins}-${overall.losses}`],
    ["Win %", pct(overall.win_pct)],
    ["Points (W-L)", `${overall.points_won}-${overall.points_lost}`],
  ];
  $("#overallStats").innerHTML = cards
    .map(([label, value]) => `<div class="stat-card"><div class="value">${esc(value)}</div><div class="label">${label}</div></div>`)
    .join("");

  const bar = (v) => `<div class="bar"><div style="width:${v ?? 0}%"></div></div> ${pct(v)}`;
  $("#venueTable tbody").innerHTML =
    venues.map((v) => `<tr><td>${esc(v.venue)}</td><td>${v.sessions}</td><td>${v.matches}</td><td>${v.wins}-${v.losses}</td><td>${bar(v.win_pct)}</td></tr>`).join("") ||
    `<tr><td colspan="5" class="empty">No data yet.</td></tr>`;
  $("#trendTable tbody").innerHTML =
    trend.map((t) => `<tr><td>${t.month}</td><td>${t.sessions}</td><td>${t.hours_played}</td><td>${t.wins}-${t.losses}</td><td>${bar(t.win_pct)}</td></tr>`).join("") ||
    `<tr><td colspan="5" class="empty">No data yet.</td></tr>`;
}

// ---------- sample data ----------

$("#deleteSamplesBtn").addEventListener("click", async () => {
  if (confirm("Delete all sample sessions? Your own sessions are kept.")) {
    await run(() => api("/api/sample-data", { method: "DELETE" }), "Sample data deleted");
  }
});

// ---------- tabs & refresh ----------

$$(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${tab.dataset.view}`));
  })
);

async function refresh() {
  const [list, venues, overall, venueStats, trend, sample] = await Promise.all([
    api("/api/sessions"),
    api("/api/venues"),
    api("/api/stats/overall"),
    api("/api/stats/venues"),
    api("/api/stats/trend"),
    api("/api/sample-data"),
  ]);
  sessions = list;
  renderSessions();
  renderLogOptions(venues);
  renderStats(overall, venueStats, trend);
  $("#sampleBanner").hidden = !sample.has_sample_data;
}

$("#sessionForm").elements.session_date.value = today();
renderMatchFields($("#matchForm .match-fields"));
refresh().catch((err) => toast(err.message, true));
