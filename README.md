# StubHub Value Score Alert Bot

This repo runs a small Playwright-based bot that checks a StubHub event page and sends an alert when it finds ticket listings with a Value/Deal score >= a threshold.

## Configure (Railway)
Set environment variables:
- STUBHUB_EVENT_URL
- MIN_VALUE_SCORE (default 9.5)
- CHECK_INTERVAL_SECONDS (default 300)
- PUSHOVER_USER_KEY (optional)
- PUSHOVER_API_TOKEN (optional)

## Run locally (optional)
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python stubhub_value_alert.py
```
