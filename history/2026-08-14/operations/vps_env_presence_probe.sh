#!/usr/bin/env bash
set -a
source /etc/patent-news-monitor.env
set +a
for name in SMTP_HOST SMTP_PORT SMTP_USER SMTP_PASSWORD EMAIL_RECIPIENT; do
  if [[ -n "${!name:-}" ]]; then
    echo "$name=SET"
  else
    echo "$name=MISSING"
  fi
done
