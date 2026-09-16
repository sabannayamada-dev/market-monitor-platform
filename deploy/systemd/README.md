# VPS daily deployment

Both timers run through `monitorctl.py`, which performs preflight validation and
records a common execution history before starting the domain worker.

- `stock-bottom`: every day at 07:00 Japan time
- `patent-materiality`: every day at 20:00 Japan time
- `gdelt-news`: two-minute heartbeat; collection runs only when adaptive x is due

`Persistent=true` runs a missed job after a reboot.

Expected layout:

- application: `/opt/patent-news-monitor/app`
- virtual environment: `/opt/patent-news-monitor/venv`
- persistent data: `/var/lib/patent-news-monitor`
- shared execution history: `/var/lib/market-monitor/platform.sqlite3`
- secrets and runtime settings: `/etc/patent-news-monitor.env`

Before enabling the timer, merge missing settings from `patent-monitor.env.example` into `/etc/patent-news-monitor.env`, replace placeholders, and restrict it with `chmod 600`.

Install and verify:

```bash
sudo cp patent-monitor-daily.service patent-monitor-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start patent-monitor-daily.service
sudo journalctl -u patent-monitor-daily.service -n 100 --no-pager
sudo systemctl enable --now patent-monitor-daily.timer
systemctl list-timers patent-monitor-daily.timer
```

Install GDELT in shadow mode first:

```bash
sudo cp gdelt-news-monitor.service gdelt-news-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gdelt-news-monitor.timer
systemctl list-timers gdelt-news-monitor.timer
```

Use `GDELT_OPERATION_STAGE=1..4`: collection, rule scoring, AI review stored only in
SQLite, and email delivery. Keep the service below stage 4 during the initial
collection-quality audit. The SQLite run history still records empty responses,
non-JSON replies, rate limits, timeouts, duplicate articles, company matches, and
candidate counts. Stage 3 starts with GPT 3/day and Gemini 1/day; stage 4 is the
only stage that permits email delivery.

Email can remain unconfigured during collection tests. Important results are kept in the SQLite outbox and sent after SMTP is configured. Add `--require-email` to `ExecStartPre` only when email delivery must be mandatory.

Common operational commands:

```bash
cd /opt/patent-news-monitor/app
/opt/patent-news-monitor/venv/bin/python monitorctl.py list
/opt/patent-news-monitor/venv/bin/python monitorctl.py validate
/opt/patent-news-monitor/venv/bin/python monitorctl.py status
/opt/patent-news-monitor/venv/bin/python monitorctl.py history stock-bottom --limit 10
```

To add another monitor, create its worker and preflight scripts, add one entry to
`monitor_services.json`, add a systemd timer, and use `monitorctl.py run <service-id>`
as its `ExecStart`. Domain data and notification outboxes remain isolated so one
monitor cannot corrupt or acknowledge another monitor's messages.
