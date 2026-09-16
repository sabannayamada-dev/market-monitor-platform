# GitHub Actions + Supabase setup

The workflow runs `patent_monitor_daily.py` every day at 06:15 JST. GitHub can delay
scheduled jobs by several minutes. The SQLite state is compressed and stored in a
private Supabase Storage bucket before the runner is discarded.

Each successful run sends one daily digest, including a zero-item report. A patent that
passes the strict Gemini urgent criteria is emailed immediately after its evaluation and
is also retained in the daily digest. The cumulative urgent-mail count is capped at 1%
of all digest-eligible patents. As a conservative consequence, no urgent email is sent
until at least 100 digest candidates have accumulated. Manual reruns do not send a
second digest for the same JST calendar date.

## 1. Supabase

1. Create a Supabase project.
2. Open **Storage**, create a private bucket named `patent-monitor-state`.
3. Open **Project Settings > API Keys** and copy the Project URL and a server-side
   secret key. Prefer the current `sb_secret_...` key. A legacy `service_role` key
   also works, but never expose either key in a browser or commit it to Git.

The default remote object is `state/patent_monitor.sqlite3.gz`. No Storage policy is
required for the server secret because it bypasses RLS. The bucket must remain private.

## 2. GitHub repository secrets

Open **Settings > Secrets and variables > Actions > Secrets** and add:

- `OPENAI_API_KEY`
- `GEMINI_API_KEY`
- `EPO_OPS_KEY`
- `EPO_OPS_SECRET`
- `SMTP_PASSWORD` (Gmail app password, not the normal account password)
- `SUPABASE_URL`
- `SUPABASE_SECRET_KEY`

## 3. GitHub repository variables

In **Settings > Secrets and variables > Actions > Variables**, add:

- `SMTP_HOST` = `smtp.gmail.com`
- `SMTP_PORT` = `587`
- `SMTP_USER` = sender Gmail address
- `EMAIL_RECIPIENT` = destination address
- `SUPABASE_STORAGE_BUCKET` = `patent-monitor-state`
- `SUPABASE_DB_OBJECT` = `state/patent_monitor.sqlite3.gz`

Optional notification overrides can be added as repository variables. The committed
configuration already enables the defaults below:

- `PATENT_DAILY_DIGEST_ENABLED` = `true`
- `PATENT_URGENT_NOTIFICATIONS_ENABLED` = `true`
- `PATENT_URGENT_MAX_SHARE` = `0.01` (values above 0.01 are clamped)
- `PATENT_URGENT_MIN_IMPORTANCE` = `98`
- `PATENT_URGENT_MIN_SHORT_TERM` = `95`
- `PATENT_URGENT_MIN_MATERIALITY` = `95`
- `PATENT_URGENT_MIN_COMPANY_PERCENTILE` = `99`

## 4. First run

Open **Actions > Patent monitor daily > Run workflow**. Confirm all of the following:

- The mock validation passes.
- `Supabase状態復元` says `remote_state_not_found` only on the first run.
- The final log contains `Supabase状態保存` with `uploaded: true`.
- Supabase Storage contains `state/patent_monitor.sqlite3.gz`.
- The Actions run has a diagnostic artifact.

The second manual run should report `restored: true`. This is the important test that
proves duplicate notifications, API usage counters, historical novelty, market outcomes,
and learned models survive between disposable runners.

To seed Supabase with the existing desktop history before the first Actions run, set
`SUPABASE_URL` and `SUPABASE_SECRET_KEY` in PowerShell and run:

```powershell
python sync_patent_state_supabase.py upload
```

Do this while the desktop patent monitor is stopped. The command creates a transactionally
consistent SQLite snapshot; it does not upload the DPAPI credential file.

## Operational notes

- GitHub `concurrency` prevents overlapping daily jobs.
- A failed job still attempts to upload a consistent SQLite snapshot.
- If Supabase restore fails for a reason other than a missing first-run object, the job
  stops before collecting patents. This avoids silently starting a blank history.
- API keys are read from environment variables on Linux. The Windows DPAPI store remains
  available for the desktop GUI.
- Do not upload `.patent_monitor_gui_state.json`; it contains local Windows paths and is
  intentionally ignored by the Actions runner.
