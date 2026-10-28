"use strict";
const $ = (id) => document.getElementById(id);
let selectedId = null;
let listRequest = 0;
let detailRequest = 0;

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function params() {
  const query = new URLSearchParams();
  if ($("search").value.trim()) query.set("q", $("search").value.trim());
  if ($("stage-filter").value) query.set("stage", $("stage-filter").value);
  if ($("overdue-filter").checked) query.set("overdue", "1");
  return query;
}
async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "The request could not be saved. Try again.");
  return data;
}
function prettyDate(value) {
  return value ? new Date(`${value}T12:00:00`).toLocaleDateString("en-GB", {day:"numeric", month:"short"}) : "No date set";
}
function status(id, message, error = false) {
  $(id).textContent = message;
  $(id).classList.toggle("error", error);
}
function showError(message) {
  $("app-error").hidden = false;
  $("app-error").querySelector("span").textContent = message;
}
function detailBusy(busy) {
  $("detail-content").setAttribute("aria-busy", String(busy));
  $("detail-content").querySelectorAll("input, select, textarea, button").forEach((node) => { node.disabled = busy; });
}
async function loadAccounts() {
  const sequence = ++listRequest;
  $("export").href = `/api/export?${params()}`;
  try {
    const data = await api(`/api/accounts?${params()}`);
    if (sequence !== listRequest) return;
    $("app-error").hidden = true;
    for (const [id, key] of [["total","total"], ["active","active"], ["overdue","overdue"], ["evidence","evidence_notes"]]) $(id).textContent = data.summary[key];
    $("match-count").textContent = `(${data.accounts.length})`;
    $("account-list").replaceChildren();
    if (!data.accounts.length) {
      const empty = element("div", "list-message");
      empty.append(element("strong", "", "No matching accounts"), element("span", "", "Try another search or clear the stage and overdue filters."));
      $("account-list").append(empty);
    }
    for (const account of data.accounts) {
      const button = element("button", `account-row${account.id === selectedId ? " selected" : ""}`);
      button.type = "button";
      button.dataset.accountId = account.id;
      button.setAttribute("aria-pressed", String(account.id === selectedId));
      const info = element("div", "account-info");
      info.append(element("strong", "", account.company), element("span", "", `${account.sector} · ${account.size} people`), element("p", "", account.need));
      const state = element("div", "account-state");
      state.append(element("span", `badge ${account.stage.toLowerCase().split(" ")[0]}`, account.stage), element("small", account.overdue ? "overdue" : "", `${account.overdue ? "Overdue · " : ""}${prettyDate(account.followup_date)}`));
      button.append(info, state);
      button.addEventListener("click", () => openAccount(account.id, window.matchMedia("(max-width: 760px)").matches));
      $("account-list").append(button);
    }
    $("due-list").replaceChildren();
    if (!data.overdue_actions.length) $("due-list").append(element("p", "muted", "You're up to date. No active accounts have an overdue follow-up."));
    for (const account of data.overdue_actions) {
      const button = element("button", "due-card");
      button.type = "button";
      button.append(element("strong", "", account.company), element("span", "", account.next_action), element("small", "", `DUE ${prettyDate(account.followup_date).toUpperCase()} ↗`));
      button.addEventListener("click", () => openAccount(account.id, true));
      $("due-list").append(button);
    }
    if (!selectedId && data.accounts.length) await openAccount(data.accounts[0].id);
  } catch (error) {
    if (sequence !== listRequest) return;
    showError(`Accounts could not be loaded. ${error.message}`);
    $("account-list").replaceChildren(element("p", "list-message", "Your saved data is still in the local database. Use Try again to reload."));
  }
}
async function openAccount(id, scroll = false) {
  const sequence = ++detailRequest;
  detailBusy(true);
  try {
    const account = await api(`/api/accounts/${id}`);
    if (sequence !== detailRequest) return;
    selectedId = id;
    $("detail-placeholder").hidden = true;
    $("detail-content").hidden = false;
    $("detail-id").textContent = ` / ${String(id).padStart(2,"0")}`;
    $("detail-name").textContent = account.company;
    $("detail-meta").textContent = `${account.sector} · ${account.size} people · Synthetic account`;
    $("detail-need").textContent = account.need;
    $("edit-stage").value = account.stage;
    $("edit-date").value = account.followup_date || "";
    $("edit-action").value = account.next_action;
    $("notes-count").textContent = `${account.notes.length} saved`;
    $("notes").replaceChildren();
    if (!account.notes.length) $("notes").append(element("p", "notes-empty", "No evidence notes yet. Add an observation and its source below. Mark hypotheses clearly."));
    for (const note of account.notes) {
      const card = element("article", "note-card");
      const link = element("a", "", new URL(note.source_url).hostname + " ↗");
      link.href = note.source_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const time = element("time", "", new Date(note.created_at).toLocaleDateString("en-GB", {day:"numeric",month:"short",year:"numeric"}));
      time.dateTime = note.created_at;
      card.append(element("p", "", note.body), link, time);
      $("notes").append(card);
    }
    document.querySelectorAll(".account-row").forEach((node) => {
      const active = Number(node.dataset.accountId) === id;
      node.classList.toggle("selected", active);
      node.setAttribute("aria-pressed", String(active));
    });
    status("action-status", "");
    status("note-status", "");
    $("note-form").reset();
    if (scroll) $("detail-content").scrollIntoView({behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start"});
  } catch (error) { showError(`Account details could not be loaded. ${error.message}`); }
  finally { if (sequence === detailRequest) detailBusy(false); }
}
$("action-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedId) return;
  const id = selectedId;
  const button = event.target.querySelector("button");
  button.disabled = true;
  status("action-status", "Saving…");
  try {
    await api(`/api/accounts/${id}`, {method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify({stage:$("edit-stage").value, followup_date:$("edit-date").value, next_action:$("edit-action").value})});
    await loadAccounts();
    if (selectedId === id) status("action-status", "Next action saved. Your changes persist when you reload.");
  } catch (error) { if (selectedId === id) status("action-status", error.message, true); }
  finally { button.disabled = false; }
});
$("note-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedId) return;
  const id = selectedId;
  const button = event.target.querySelector("button");
  button.disabled = true;
  status("note-status", "Saving…");
  try {
    await api(`/api/accounts/${id}/notes`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({body:$("note-body").value, source_url:$("note-source").value})});
    if (selectedId === id) { await openAccount(id); status("note-status", "Evidence note saved with its source."); }
    await loadAccounts();
  } catch (error) { if (selectedId === id) status("note-status", error.message, true); }
  finally { button.disabled = false; }
});
let debounce;
$("search").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(loadAccounts, 180); });
$("stage-filter").addEventListener("change", loadAccounts);
$("overdue-filter").addEventListener("change", loadAccounts);
$("filters").addEventListener("submit", (event) => { event.preventDefault(); loadAccounts(); });
$("retry").addEventListener("click", loadAccounts);
loadAccounts();
