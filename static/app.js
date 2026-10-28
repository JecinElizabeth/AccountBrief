"use strict";

// The API owns persisted data and health rules. This file owns rendering and forms.
const $ = (id) => document.getElementById(id);
const VIEWS = {
  all: "All accounts",
  attention: "Needs attention",
  onboarding: "Onboarding",
  overdue: "Overdue work",
};
let selectedId = null;
let currentAccount = null;
let currentView = "attention";
let activeTab = "overview";
let today = "";
let listSequence = 0;
let detailSequence = 0;
let listController;
let detailController;
let writePending = false;
let toastTimer;
let previewCsv = null;
let importSequence = 0;
let importController;
let importPending = false;
let createPending = false;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new Error(
      "Could not connect to the local workspace. Check that the server is running and try again.",
    );
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      data.error || "The request could not be completed. Try again.",
    );
  return data;
}

function jsonRequest(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function params() {
  const query = new URLSearchParams({ view: currentView });
  if ($("search").value.trim()) query.set("q", $("search").value.trim());
  return query;
}

function prettyDate(value, includeYear = false) {
  if (!value) return "Not set";
  const parsed = new Date(value.length === 10 ? `${value}T12:00:00` : value);
  if (Number.isNaN(parsed.getTime())) return "Not set";
  return parsed.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    ...(includeYear ? { year: "numeric" } : {}),
  });
}

function displayNumber(value) {
  return value === null || value === undefined
    ? "—"
    : Number(value).toLocaleString("en-GB");
}

function setStatus(id, message, error = false) {
  $(id).textContent = message;
  $(id).classList.toggle("error", error);
}

function showError(message) {
  $("app-error").hidden = false;
  $("app-error").querySelector("span").textContent = message;
}

function toast(message) {
  clearTimeout(toastTimer);
  $("toast").textContent = message;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => {
    $("toast").hidden = true;
  }, 4500);
}

function healthBadge(health) {
  return element(
    "span",
    `health-badge ${health?.state || "unknown"}`,
    health?.label || "No usage data",
  );
}

function setDetailBusy(busy) {
  const disabled = busy || writePending;
  $("detail-content").setAttribute("aria-busy", String(disabled));
  $("detail-content")
    .querySelectorAll("input,select,textarea,button")
    .forEach((node) => {
      node.disabled = disabled;
    });
  $("detail-loading").hidden = !busy;
}

function setWritePending(busy) {
  writePending = busy;
  setDetailBusy(false);
  document
    .querySelectorAll(
      ".account-row,.view-filters button,#search,#clear-search,#open-import,#new-account",
    )
    .forEach((node) => {
      node.disabled = busy;
    });
}

function markSelected() {
  document.querySelectorAll(".account-row").forEach((node) => {
    const selected = String(node.dataset.accountId) === String(selectedId);
    node.classList.toggle("selected", selected);
    node.setAttribute("aria-pressed", String(selected));
  });
}

function renderQueue(accounts) {
  const list = $("account-list");
  list.replaceChildren();
  $("match-count").textContent =
    `${accounts.length} ${accounts.length === 1 ? "record" : "records"}`;
  if (!accounts.length) {
    const empty = element("div", "list-message");
    empty.append(
      element("strong", "", "No matching accounts"),
      element(
        "span",
        "",
        "Try another view or clear the search. Your saved records are unchanged.",
      ),
    );
    list.append(empty);
  }
  for (const account of accounts) {
    const button = element("button", "account-row");
    button.type = "button";
    button.dataset.accountId = account.id;
    button.disabled = writePending;
    const heading = element("span", "row-heading");
    const dot = element("span", account.health?.state || "unknown");
    dot.setAttribute("aria-hidden", "true");
    heading.append(element("strong", "", account.company), dot);
    const footer = element("span", "row-footer");
    footer.append(
      element(
        "span",
        `row-health ${account.health?.state || "unknown"}`,
        account.health?.label || "No usage data",
      ),
    );
    const overdue =
      account.followup_date &&
      account.followup_date < today &&
      account.stage !== "Closed";
    footer.append(
      element(
        "span",
        `row-date${overdue ? " overdue" : ""}`,
        account.followup_date
          ? `${overdue ? "Due " : "Follow-up "}${prettyDate(account.followup_date)}`
          : "No follow-up",
      ),
    );
    button.append(
      heading,
      element(
        "span",
        "row-meta",
        `${account.sector} · ${account.plan || "No plan"}`,
      ),
      element("span", "row-next", account.next_action || "Set the next action"),
      footer,
    );
    button.addEventListener("click", () =>
      openAccount(account.id, { focus: true }),
    );
    list.append(button);
  }
  markSelected();
}

async function loadAccounts({ retainSelection = false } = {}) {
  const sequence = ++listSequence;
  listController?.abort();
  listController = new AbortController();
  $("account-list").setAttribute("aria-busy", "true");
  $("export-accounts").href = `/api/export?${params()}`;
  $("queue-view-label").textContent = VIEWS[currentView].toUpperCase();
  try {
    const data = await api(`/api/accounts?${params()}`, {
      signal: listController.signal,
    });
    if (sequence !== listSequence) return;
    today = data.today;
    $("app-error").hidden = true;
    $("queue-summary").textContent =
      `${data.summary.total} ${data.summary.total === 1 ? "account" : "accounts"} in this workspace`;
    for (const [id, key] of [
      ["count-all", "total"],
      ["count-attention", "attention"],
      ["count-onboarding", "onboarding"],
      ["count-overdue", "overdue"],
    ])
      $(id).textContent = data.summary[key] ?? 0;
    renderQueue(data.accounts);
    if (!retainSelection) {
      const isVisible = data.accounts.some(
        (account) => String(account.id) === String(selectedId),
      );
      if (!isVisible && data.accounts.length)
        await openAccount(data.accounts[0].id);
      if (!data.accounts.length) {
        selectedId = null;
        currentAccount = null;
        detailController?.abort();
        ++detailSequence;
        $("detail-content").hidden = true;
        $("detail-placeholder").hidden = false;
      }
    }
  } catch (error) {
    if (error.name === "AbortError" || sequence !== listSequence) return;
    showError(error.message);
  } finally {
    if (sequence === listSequence)
      $("account-list").setAttribute("aria-busy", "false");
  }
}

function switchTab(name, focus = false) {
  activeTab = name;
  document.querySelectorAll("[data-tab]").forEach((button) => {
    const selected = button.dataset.tab === name;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    $(`panel-${button.dataset.tab}`).hidden = !selected;
    if (selected && focus) button.focus();
  });
  $("detail-scroll").scrollTop = 0;
}

function renderHealth(account) {
  const health = account.health || {
    state: "unknown",
    label: "No usage data",
    reasons: [],
  };
  const badge = $("detail-health");
  badge.className = `health-badge ${health.state}`;
  badge.textContent = health.label;
  document.querySelector(".health-panel").dataset.health = health.state;
  const reasons = element("ul", "health-reason-list");
  for (const reason of health.reasons || [])
    reasons.append(element("li", "", reason));
  if (!reasons.children.length)
    reasons.append(
      element("li", "", "No additional attention flags in the saved data."),
    );
  $("health-reasons").replaceChildren(reasons);
}

function renderUsage(account) {
  $("usage-seats").textContent = displayNumber(account.seats);
  $("usage-active").textContent = displayNumber(account.active_users);
  $("usage-adoption").textContent =
    account.adoption_pct === null || account.adoption_pct === undefined
      ? "—"
      : `${Math.round(account.adoption_pct)}%`;
  $("usage-last").textContent = account.last_active_date
    ? prettyDate(account.last_active_date)
    : "No snapshot";
  $("usage-date").textContent = account.last_active_date
    ? "Saved usage snapshot"
    : "Import a snapshot to begin";
  $("adoption-progress").value = account.adoption_pct || 0;
  $("adoption-progress").hidden =
    account.adoption_pct === null || account.adoption_pct === undefined;
  $("usage-caption").textContent =
    account.seats === 0
      ? "No seats assigned. Adoption is not calculated for zero seats."
      : account.seats === null || account.seats === undefined
        ? "No usage snapshot has been imported for this account."
        : "Seat adoption = active users ÷ assigned seats. The saved snapshot is used for health rules.";
}

function inputLabel(text, type, value, maximum) {
  const label = element("label", "", text);
  const input = element("input");
  input.type = type;
  input.value = value || "";
  if (maximum) input.maxLength = maximum;
  label.append(input);
  return { label, input };
}

function renderTasks(account) {
  const tasks = account.tasks || [];
  const done = tasks.filter((task) => task.status === "done").length;
  $("tasks-summary").textContent =
    `${done} of ${tasks.length} complete · ${account.overdue_tasks || 0} overdue · ${account.blockers || 0} blocked`;
  $("tasks").replaceChildren();
  if (!tasks.length)
    $("tasks").append(
      element(
        "p",
        "empty-message",
        "No onboarding tasks yet. Add the first step and assign an owner.",
      ),
    );
  for (const task of tasks) {
    const entry = element("div", "work-entry");
    const row = element("article", "work-item");
    const copy = element("div", "work-item-copy");
    const meta = element("div", "work-item-meta");
    meta.append(element("span", "", task.owner || "Unassigned"));
    meta.append(
      element(
        "span",
        task.due_date && task.due_date < today && task.status !== "done"
          ? "overdue"
          : "",
        task.due_date ? `Due ${prettyDate(task.due_date)}` : "No due date",
      ),
    );
    copy.append(
      element(
        "p",
        `work-item-title${task.status === "done" ? " done" : ""}`,
        task.title,
      ),
      meta,
    );
    const controls = element("div", "work-item-actions");
    const select = element("select", "task-state");
    select.setAttribute("aria-label", `Status for ${task.title}`);
    for (const [value, text] of [
      ["todo", "To do"],
      ["blocked", "Blocked"],
      ["done", "Done"],
    ]) {
      const option = element("option", "", text);
      option.value = value;
      select.append(option);
    }
    select.value = task.status;
    select.addEventListener("change", () =>
      mutate(
        `/api/tasks/${task.id}`,
        "PATCH",
        { status: select.value },
        "task-status",
        "Task status saved.",
      ),
    );
    const edit = element("button", "button button-quiet button-small", "Edit");
    edit.type = "button";
    const form = element("form", "task-edit-form");
    form.hidden = true;
    const title = inputLabel("Task name", "text", task.title, 180);
    title.input.required = true;
    const owner = inputLabel("Task owner", "text", task.owner, 80);
    const due = inputLabel("Due date", "date", task.due_date);
    const grid = element("div", "form-grid two-columns");
    grid.append(owner.label, due.label);
    const actions = element("div", "form-actions");
    const save = element(
      "button",
      "button button-primary button-small",
      "Save task",
    );
    save.type = "submit";
    const cancel = element(
      "button",
      "button button-quiet button-small",
      "Cancel",
    );
    cancel.type = "button";
    cancel.addEventListener("click", () => {
      form.hidden = true;
      edit.focus();
    });
    actions.append(save, cancel);
    form.append(title.label, grid, actions);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      mutate(
        `/api/tasks/${task.id}`,
        "PATCH",
        {
          title: title.input.value,
          owner: owner.input.value,
          due_date: due.input.value,
        },
        "task-status",
        "Task details saved.",
      );
    });
    edit.addEventListener("click", () => {
      form.hidden = !form.hidden;
      if (!form.hidden) title.input.focus();
    });
    controls.append(select, edit);
    row.append(copy, controls);
    entry.append(row, form);
    $("tasks").append(entry);
  }
}

function renderIssues(account) {
  const issues = account.issues || [];
  $("issues-summary").textContent =
    `${issues.filter((issue) => issue.status === "open").length} open · ${issues.filter((issue) => issue.status === "resolved").length} resolved`;
  $("issues").replaceChildren();
  if (!issues.length)
    $("issues").append(
      element(
        "p",
        "empty-message",
        "No support issues logged. Record an issue when it blocks account progress.",
      ),
    );
  for (const issue of issues) {
    const row = element("article", "work-item");
    const copy = element("div", "work-item-copy");
    const meta = element("div", "work-item-meta");
    meta.append(
      element(
        "span",
        `severity-label ${issue.severity}`,
        issue.severity === "urgent" ? "Urgent" : "Normal",
      ),
      element(
        "span",
        "",
        `${issue.status === "resolved" ? "Resolved" : "Opened"} · ${prettyDate(issue.created_at)}`,
      ),
    );
    copy.append(
      element(
        "p",
        `work-item-title${issue.status === "resolved" ? " done" : ""}`,
        issue.title,
      ),
      meta,
    );
    const button = element(
      "button",
      "button button-secondary button-small",
      issue.status === "open" ? "Resolve" : "Reopen",
    );
    button.type = "button";
    button.setAttribute(
      "aria-label",
      `${issue.status === "open" ? "Resolve" : "Reopen"} issue: ${issue.title}`,
    );
    button.addEventListener("click", () =>
      mutate(
        `/api/issues/${issue.id}`,
        "PATCH",
        { status: issue.status === "open" ? "resolved" : "open" },
        "issue-status",
        "Issue status saved.",
      ),
    );
    row.append(copy, button);
    $("issues").append(row);
  }
}

function renderNotes(account) {
  const notes = account.notes || [];
  $("notes-count").textContent = `${notes.length} saved`;
  $("notes").replaceChildren();
  if (!notes.length)
    $("notes").append(
      element(
        "p",
        "empty-message",
        "No account notes yet. Record an observation or handoff above.",
      ),
    );
  for (const note of notes) {
    const article = element("article", "note-card");
    const footer = element("div", "note-footer");
    const time = element("time", "", prettyDate(note.created_at, true));
    time.dateTime = note.created_at;
    footer.append(time);
    if (note.source_url) {
      try {
        const source = new URL(note.source_url);
        if (["http:", "https:"].includes(source.protocol)) {
          const link = element("a", "", `${source.hostname} ↗`);
          link.href = source.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          footer.append(link);
        }
      } catch {
        /* The note remains readable even if an old source URL is invalid. */
      }
    }
    article.append(element("p", "", note.body), footer);
    $("notes").append(article);
  }
  $("activity").replaceChildren();
  if (!account.activity?.length)
    $("activity").append(
      element("li", "", "Changes and imported snapshots will appear here."),
    );
  for (const activity of account.activity || []) {
    const item = element("li", "", activity.body);
    const time = element("time", "", prettyDate(activity.created_at, true));
    time.dateTime = activity.created_at;
    item.append(time);
    $("activity").append(item);
  }
}

function renderAccount(account) {
  $("detail-id").textContent = `ACCOUNT ${String(account.id).padStart(3, "0")}`;
  $("detail-name").textContent = account.company;
  $("detail-meta").textContent =
    `${account.sector} · ${account.plan || "No plan assigned"}`;
  $("detail-stage").textContent = account.stage;
  $("detail-owner").textContent = `Owner: ${account.owner || "Unassigned"}`;
  $("export-brief").href = `/api/accounts/${account.id}/brief`;
  $("work-count").textContent =
    Number(account.open_tasks || 0) + Number(account.open_issues || 0);
  $("edit-stage").value = account.stage;
  $("edit-owner").value = account.owner || "";
  $("edit-date").value = account.followup_date || "";
  $("edit-action").value = account.next_action || "";
  renderHealth(account);
  renderUsage(account);
  renderTasks(account);
  renderIssues(account);
  renderNotes(account);
  for (const id of [
    "action-status",
    "task-status",
    "issue-status",
    "note-status",
  ])
    setStatus(id, "");
}

async function openAccount(id, { keepTab = false, focus = false } = {}) {
  if (writePending) return;
  selectedId = id;
  currentAccount = null;
  const sequence = ++detailSequence;
  detailController?.abort();
  detailController = new AbortController();
  $("detail-placeholder").hidden = true;
  $("detail-content").hidden = false;
  setDetailBusy(true);
  markSelected();
  if (focus) document.body.classList.add("detail-open");
  try {
    const payload = await api(`/api/accounts/${id}`, {
      signal: detailController.signal,
    });
    if (sequence !== detailSequence || String(selectedId) !== String(id))
      return;
    currentAccount = payload.account || payload;
    renderAccount(currentAccount);
    switchTab(keepTab ? activeTab : "overview");
    if (focus) $("detail-name").focus({ preventScroll: true });
  } catch (error) {
    if (error.name !== "AbortError" && sequence === detailSequence) {
      $("detail-content").hidden = true;
      $("detail-placeholder").hidden = false;
      showError(`Account details could not be loaded. ${error.message}`);
    }
  } finally {
    if (sequence === detailSequence) setDetailBusy(false);
  }
}

async function mutate(url, method, body, statusId, success, afterSuccess) {
  if (!selectedId || writePending) return;
  const id = selectedId;
  setWritePending(true);
  setStatus(statusId, "Saving…");
  try {
    await api(url, jsonRequest(method, body));
    if (afterSuccess) afterSuccess();
    await loadAccounts({ retainSelection: true });
    // Open the same target after the write. Navigation is locked while saving.
    writePending = false;
    await openAccount(id, { keepTab: true });
    setStatus(statusId, success);
  } catch (error) {
    setStatus(statusId, error.message, true);
  } finally {
    setWritePending(false);
  }
}

function chooseView(view) {
  if (writePending) return;
  currentView = view;
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  loadAccounts();
}

$("action-form").addEventListener("submit", (event) => {
  event.preventDefault();
  mutate(
    `/api/accounts/${selectedId}`,
    "PATCH",
    {
      stage: $("edit-stage").value,
      owner: $("edit-owner").value,
      followup_date: $("edit-date").value,
      next_action: $("edit-action").value,
    },
    "action-status",
    "Next action saved.",
  );
});
$("task-form").addEventListener("submit", (event) => {
  event.preventDefault();
  mutate(
    `/api/accounts/${selectedId}/tasks`,
    "POST",
    {
      title: $("task-title").value,
      owner: $("task-owner").value.trim() || "Unassigned",
      due_date: $("task-date").value,
    },
    "task-status",
    "Onboarding task added.",
    () => {
      $("task-form").reset();
      $("task-form").hidden = true;
    },
  );
});
$("issue-form").addEventListener("submit", (event) => {
  event.preventDefault();
  mutate(
    `/api/accounts/${selectedId}/issues`,
    "POST",
    { title: $("issue-title").value, severity: $("issue-severity").value },
    "issue-status",
    "Support issue logged.",
    () => {
      $("issue-form").reset();
      $("issue-form").hidden = true;
    },
  );
});
$("note-form").addEventListener("submit", (event) => {
  event.preventDefault();
  mutate(
    `/api/accounts/${selectedId}/notes`,
    "POST",
    { body: $("note-body").value, source_url: $("note-source").value },
    "note-status",
    "Account note saved.",
    () => $("note-form").reset(),
  );
});
$("show-task-form").addEventListener("click", () => {
  $("task-form").hidden = false;
  $("task-title").focus();
});
$("show-issue-form").addEventListener("click", () => {
  $("issue-form").hidden = false;
  $("issue-title").focus();
});
document.querySelectorAll("[data-cancel]").forEach((button) =>
  button.addEventListener("click", () => {
    $(button.dataset.cancel).hidden = true;
    $(`show-${button.dataset.cancel}`).focus();
  }),
);
document
  .querySelectorAll("[data-view]")
  .forEach((button) =>
    button.addEventListener("click", () => chooseView(button.dataset.view)),
  );
document.querySelectorAll("[data-tab]").forEach((button) => {
  button.addEventListener("click", () => switchTab(button.dataset.tab));
  button.addEventListener("keydown", (event) => {
    const tabs = ["overview", "work", "notes"];
    const index = tabs.indexOf(activeTab);
    let next;
    if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
    if (event.key === "ArrowLeft")
      next = tabs[(index + tabs.length - 1) % tabs.length];
    if (event.key === "Home") next = tabs[0];
    if (event.key === "End") next = tabs[tabs.length - 1];
    if (next) {
      event.preventDefault();
      switchTab(next, true);
    }
  });
});
$("back-to-list").addEventListener("click", () => {
  document.body.classList.remove("detail-open");
  const selected = document.querySelector(
    `.account-row[data-account-id="${selectedId}"]`,
  );
  (selected || $("search")).focus({ preventScroll: true });
});
let debounce;
$("search").addEventListener("input", () => {
  $("clear-search").hidden = !$("search").value;
  clearTimeout(debounce);
  debounce = setTimeout(loadAccounts, 180);
});
$("clear-search").addEventListener("click", () => {
  $("search").value = "";
  $("clear-search").hidden = true;
  $("search").focus();
  loadAccounts();
});
$("filters").addEventListener("submit", (event) => {
  event.preventDefault();
  clearTimeout(debounce);
  loadAccounts();
});
$("retry").addEventListener("click", async () => {
  await loadAccounts({ retainSelection: true });
  if (selectedId) await openAccount(selectedId, { keepTab: true });
});

function clearImportPreview() {
  ++importSequence;
  importController?.abort();
  previewCsv = null;
  $("import-preview").hidden = true;
  $("apply-import").hidden = true;
  $("preview-import").hidden = false;
  $("preview-import").disabled = false;
  $("import-error").hidden = true;
  setStatus("import-status", "");
}

function importBusy(busy) {
  importPending = busy;
  for (const id of [
    "csv-file",
    "csv-input",
    "close-import",
    "cancel-import",
    "preview-import",
    "apply-import",
  ])
    $(id).disabled = busy;
}

function previewHealth(health) {
  return typeof health === "string"
    ? { state: health, label: health, reasons: [] }
    : health;
}

function renderPreview(rows) {
  $("preview-count").textContent =
    `${rows.length} ${rows.length === 1 ? "account" : "accounts"}`;
  $("preview-rows").replaceChildren();
  for (const row of rows) {
    const card = element("article", "preview-account");
    const before = row.before;
    const after = row.after;
    card.append(
      element("h4", "", row.company),
      element(
        "p",
        "preview-metrics",
        `Seats ${displayNumber(before.seats)} → ${displayNumber(after.seats)} · Active ${displayNumber(before.active_users)} → ${displayNumber(after.active_users)} · Last active ${prettyDate(before.last_active_date)} → ${prettyDate(after.last_active_date)}`,
      ),
    );
    const healthRow = element("div", "preview-health");
    const oldHealth = previewHealth(row.health_before);
    const newHealth = previewHealth(row.health_after);
    healthRow.append(
      healthBadge(oldHealth),
      element("span", "", "→"),
      healthBadge(newHealth),
    );
    card.append(healthRow);
    const reasons = element("ul", "preview-reasons");
    for (const reason of newHealth?.reasons || [])
      reasons.append(element("li", "", reason));
    card.append(reasons);
    $("preview-rows").append(card);
  }
  $("import-preview").hidden = false;
}

$("open-import").addEventListener("click", () => {
  clearImportPreview();
  $("import-dialog").showModal();
  $("csv-input").focus();
});
for (const id of ["close-import", "cancel-import"])
  $(id).addEventListener("click", () => {
    if (!importPending) $("import-dialog").close();
  });
$("import-dialog").addEventListener("cancel", (event) => {
  if (importPending) event.preventDefault();
});
$("import-dialog").addEventListener("close", clearImportPreview);
$("csv-input").addEventListener("input", clearImportPreview);
$("csv-file").addEventListener("change", async () => {
  clearImportPreview();
  const file = $("csv-file").files[0];
  if (!file) return;
  if (file.size > 65536) {
    $("import-error").textContent = "Keep the CSV file within 64 KiB.";
    $("import-error").hidden = false;
    return;
  }
  const sequence = importSequence;
  const text = await file.text();
  if (sequence !== importSequence) return;
  $("csv-input").value = text;
});
$("preview-import").addEventListener("click", async () => {
  const csv = $("csv-input").value;
  clearImportPreview();
  if (!csv.trim()) {
    $("import-error").textContent =
      "Paste CSV text or choose a CSV file first.";
    $("import-error").hidden = false;
    return;
  }
  const sequence = ++importSequence;
  importController = new AbortController();
  $("preview-import").disabled = true;
  setStatus("import-status", "Validating snapshot…");
  try {
    const data = await api("/api/usage/preview", {
      ...jsonRequest("POST", { csv }),
      signal: importController.signal,
    });
    if (sequence !== importSequence || !$("import-dialog").open) return;
    previewCsv = csv;
    renderPreview(data.rows);
    $("preview-import").hidden = true;
    $("apply-import").hidden = false;
    setStatus(
      "import-status",
      "Preview validated. The saved snapshot is unchanged.",
    );
    $("apply-import").focus();
  } catch (error) {
    if (error.name !== "AbortError" && sequence === importSequence) {
      $("import-error").textContent = error.message;
      $("import-error").hidden = false;
      setStatus("import-status", "");
    }
  } finally {
    if (sequence === importSequence) $("preview-import").disabled = false;
  }
});
$("apply-import").addEventListener("click", async () => {
  if (!previewCsv || importPending) return;
  importBusy(true);
  setStatus("import-status", "Applying snapshot…");
  try {
    const data = await api(
      "/api/usage/import",
      jsonRequest("POST", { csv: previewCsv }),
    );
    importBusy(false);
    $("import-dialog").close();
    await loadAccounts({ retainSelection: true });
    if (selectedId) await openAccount(selectedId, { keepTab: true });
    toast(
      `Usage snapshot applied to ${data.updated} ${data.updated === 1 ? "account" : "accounts"}.`,
    );
  } catch (error) {
    $("import-error").textContent = error.message;
    $("import-error").hidden = false;
    setStatus("import-status", "");
  } finally {
    importBusy(false);
  }
});

function createBusy(busy) {
  createPending = busy;
  $("new-account-form")
    .querySelectorAll("input,select,button")
    .forEach((node) => {
      node.disabled = busy;
    });
}
$("new-account").addEventListener("click", () => {
  $("new-account-error").hidden = true;
  setStatus("new-account-status", "");
  $("new-account-dialog").showModal();
  $("new-company").focus();
});
for (const id of ["close-new-account", "cancel-new-account"])
  $(id).addEventListener("click", () => {
    if (!createPending) $("new-account-dialog").close();
  });
$("new-account-dialog").addEventListener("cancel", (event) => {
  if (createPending) event.preventDefault();
});
$("new-account-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (createPending) return;
  const body = {
    company: $("new-company").value,
    sector: $("new-sector").value,
    owner: $("new-owner").value,
    plan: $("new-plan").value,
    stage: $("new-stage").value,
    next_action: $("new-action").value,
    followup_date: $("new-date").value,
  };
  createBusy(true);
  $("new-account-error").hidden = true;
  setStatus("new-account-status", "Creating account…");
  try {
    const data = await api("/api/accounts", jsonRequest("POST", body));
    createBusy(false);
    $("new-account-dialog").close();
    $("new-account-form").reset();
    $("search").value = "";
    $("clear-search").hidden = true;
    currentView = "all";
    document.querySelectorAll("[data-view]").forEach((button) => {
      const active = button.dataset.view === "all";
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    await loadAccounts({ retainSelection: true });
    await openAccount(data.id, { focus: true });
    toast("Account created. Import usage or add onboarding work next.");
  } catch (error) {
    $("new-account-error").textContent = error.message;
    $("new-account-error").hidden = false;
    setStatus("new-account-status", "");
  } finally {
    createBusy(false);
  }
});

loadAccounts();
