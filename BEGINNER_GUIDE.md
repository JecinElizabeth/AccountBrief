# AccountBrief in 45 minutes

## 0–10 minutes: use the product

Run the app using the README. Every record is a fictional sample. Open Cedar & Loom, read the need, change its stage, set a follow-up date, and save. Reload: the change is still there because the backend wrote to SQLite. Add a research note and a complete source URL. Filter by stage or overdue date, then export the matching rows.

The workflow is simple: research a company, record what is known, decide the next step, and follow through. A source is evidence to inspect; it is not proof that every statement is true. Mark guesses as hypotheses.

## 10–20 minutes: follow one saved change

1. `templates/index.html` contains the stage/date/action form.
2. `static/app.js` catches its submit event and sends a JSON `PATCH` request.
3. `app.py` checks that the account exists, the stage is allowed, the date is valid, and the action is short enough.
4. A parameterized SQL `UPDATE` writes the fields. The transaction commits.
5. The browser refreshes the ledger and shows a saved message.

Frontend means the page a person uses. Backend means the server code validating and processing requests. The database keeps information after the page closes. An API is their agreed set of requests and responses.

## 20–30 minutes: read the SQL

The `accounts` table has one row per fictional business. The `notes` table can have many notes per account. `notes.account_id` links to `accounts.id` — a one-to-many relationship.

```sql
-- Find qualified accounts.
SELECT company, stage, next_action
FROM accounts
WHERE stage = 'Qualified';

-- Count evidence notes for each account, including accounts with no notes.
SELECT a.company, COUNT(n.id) AS evidence_count
FROM accounts AS a
LEFT JOIN notes AS n ON n.account_id = a.id
GROUP BY a.id;

-- Find follow-ups that need attention; closed records are excluded.
SELECT company, followup_date, next_action
FROM accounts
WHERE followup_date < date('now', 'localtime') AND stage != 'Closed'
ORDER BY followup_date;
```

The app uses `?` placeholders when adding user input to SQL. The values are sent separately from the SQL text, which prevents a search string being interpreted as a SQL command. No data is sent to a cloud database.

## 30–40 minutes: test the edges

- Search for a word with no match: the page explains how to clear filters.
- Set a past date: an active account becomes overdue. Today's date is not overdue.
- Switch to Closed: it no longer contributes to active overdue work.
- Add `javascript:alert(1)` as a source URL: the backend rejects it.
- Add a next action beginning with `=` and export it: the CSV value gets a leading apostrophe so spreadsheets do not treat it as a formula.
- Stop the server and try a filter: the interface keeps a retry path. Restart it and retry.
- Run the unit tests. Each test gets a temporary database, so tests cannot overwrite the demo database.

## 40–45 minutes: explain it clearly

Suggested explanation after reviewing and demonstrating it:

> AccountBrief is a local portfolio demo for account research and follow-ups. It uses a simple HTML and JavaScript interface, a Flask backend, and a SQLite database. I can search sample accounts, add a source-backed note, update a pipeline stage and next action, and export filtered data. The backend validates inputs and uses parameterized SQL. All records are synthetic, and it does not send messages or scrape websites.

Describe only the portions you personally reviewed, changed, tested, or can explain. Creating a repository does not establish experience working with real customers, professional sales targets, or production systems.

## Questions to be ready for

**How does overdue work?** The server compares a stored date with today's local date. Closed accounts are excluded. It is a date rule, not AI.

**Why SQLite?** It is a database in one local file, supports SQL, needs no separate database service, and is enough for this small local example.

**What happens on invalid input?** The server returns a 400 JSON error; the form displays a useful message. No partial update is written.

**Does it verify the source URL?** It validates that the URL has an HTTP(S) scheme and a hostname. It does not fetch the page or establish that the claim is true.

**What would you improve for a real company?** Login and record permissions first, then proper migrations, audit history, backups, and shared hosting. The current demo is intentionally local.

**What is relevant to a customer or operations role?** Organizing context, keeping evidence, recording actions, and following up. A tool supports those habits; it cannot replace good judgment or actual customer work.
