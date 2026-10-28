# Understand AccountBrief in 45 minutes

## The job it helps with — 5 minutes

A product-success person needs more than a customer's name and a follow-up date. They need to know whether the customer is using the product, what is blocking setup, who owns the next task and what to explain at the next review.

AccountBrief combines that information. It starts with fictional examples, and you can add your own account records. There is no AI model: explicit rules turn current data into review reasons.

## Walk through one account — 10 minutes

1. Run the README commands and open Cedar & Loom.
2. Read the attention reasons and compare them with the displayed usage and open work.
3. Open the work area. Change the blocked catalog task to complete.
4. Resolve or reopen a support issue. Review the updated account flags.
5. Add a note explaining the next step. A source URL is optional.
6. Export the account brief. Check that the open work and notes match the screen.

Completing one task does not automatically make an account healthy. Low usage, inactivity or another unresolved issue can still require attention.

## Import a snapshot — 10 minutes

Download the usage template. Its four columns are:

```csv
account_id,seats,active_users,last_active_date
```

Open the file and edit one account's active-user count and date. Active users must not exceed seats, and the last-active date cannot be in the future.

Select the file and inspect the preview. It shows the current and proposed values, plus the attention result. Previewing changes nothing. Apply only after reviewing it.

Try an invalid row, such as more active users than seats. The whole import should be rejected. It must not save the earlier valid rows while silently dropping the bad one.

For your own account, create it first and use its displayed account ID in the CSV. New accounts have no invented usage snapshot.

## Read the complete path — 10 minutes

- `templates/index.html` defines forms and workspace panels.
- `static/app.js` reads form values, sends JSON requests and renders the returned data.
- `app.py` validates those requests and writes parameterized SQL to SQLite.
- `calculate_health` explains the attention rules.
- `validate_usage_csv` checks the entire batch before an import transaction writes.
- `render_brief` turns saved account information into a downloadable Markdown file.

The database has linked accounts, tasks, issues, notes and activity. The account ID connects each item to its account. UI changes survive refreshes because the data is stored in SQLite, rather than only in browser state.

## Verify and explain — 10 minutes

Run:

```bash
python -m unittest discover -s tests -v
```

Tests cover health thresholds, field checks, saved changes, old-database migration and import rollback. The browser checker separately exercises real forms, previews, exports and mobile navigation.

A concise explanation:

“AccountBrief is a local customer onboarding and account-review workspace. It combines usage snapshots with onboarding tasks and support issues. Explicit rules explain why an account needs attention. An owner can review the context, update work and export a brief for the next person. CSV changes are previewed and validated before they are saved together.”

Before describing your personal contribution, make one small change, run the relevant check and explain the diff. Useful practice changes include a task label, a new account field, or a clearly documented health threshold. State team and individual work accurately.
