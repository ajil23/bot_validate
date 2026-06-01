# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A unified Python service that **validates** MetaTrader 5 (MT5) trading accounts **and** exports their journal/trade history reports to a Laravel backend (setra.id). It runs as a persistent FastAPI service on Windows.

**Flow:**
1. Member submits account in Laravel → Laravel calls `POST /validate`
2. Service validates credentials against MT5 terminal, returns result
3. Laravel saves account to DB, gets `account_id`, then calls `POST /journal/trigger`
4. Service exports HTML trade history report from MT5 and uploads to Laravel
5. Journal data is displayed in Laravel dashboard

## Running the service

```powershell
pip install -r requirements.txt
python app.py
```

Listens on `http://0.0.0.0:8002`. All config loaded from `.env` via `python-dotenv`.

**Production (NSSM Windows service):**
```cmd
nssm start MT5ValidatorJournal
nssm restart MT5ValidatorJournal
nssm stop MT5ValidatorJournal
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/validate` | `X-Api-Secret` | Validate MT5 credentials |
| `POST` | `/journal/trigger` | `X-Api-Secret` | Queue immediate journal for an account |
| `GET`  | `/health` | — | Liveness + queue stats |

**`POST /validate` body:**
```json
{ "login": 12345678, "password": "...", "server": "Broker-Server" }
```

**`POST /journal/trigger` body:**
```json
{ "account_id": 1, "login": "12345678", "password": "...", "server": "Broker-Server" }
```
`account_id` is Laravel's DB id — required for report upload.

## Environment variables (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `VALIDATOR_SECRET` | `rahasia123` | Shared secret, sent as `X-Api-Secret` header |
| `SERVICE_HOST` / `SERVICE_PORT` | `0.0.0.0` / `8002` | Bind address |
| `MT5_EXE` | `C:\Program Files\MetaTrader 5\terminal64.exe` | MT5 terminal path |
| `MT5_LOGIN_TIMEOUT_MS` | `15000` | MT5 login timeout |
| `MT5_HARD_TIMEOUT_SEC` | `60` | FastAPI hard timeout for `/validate` |
| `MT5_RESET_EVERY` | `50` | Cleanup MT5 data dirs every N validations |
| `JOURNAL_API_BASE` | `http://127.0.0.1:8000/api` | Laravel backend API base |
| `JOURNAL_API_KEY` | — | API key sent as `?key=` query param |
| `JOURNAL_INTERVAL_SEC` | `3600` | Scheduler interval (no open positions) |
| `JOURNAL_SHORT_INTERVAL_SEC` | `600` | Scheduler interval (has open positions) |
| `JOURNAL_MAX_JITTER_SEC` | `30` | Random jitter added to interval |
| `JOURNAL_SCHEDULER_ENABLED` | `true` | Set to `false` to disable periodic cycle |

## Architecture

### Threading model

```
Main thread       — FastAPI/uvicorn HTTP server
Executor pool     — handles _validate_sync() calls from POST /validate
journal-worker    — background thread, processes _journal_queue one by one
journal-scheduler — background thread, runs periodic cycle
```

### MT5 global state constraint

The `MetaTrader5` Python package has **global state per process** — only one MT5 connection active at a time. All MT5 calls go through `_mt5_lock` (a `threading.Lock`). Every operation follows the same pattern:

```
acquire lock → mt5.shutdown() → mt5.initialize(login=...) → do work → mt5.shutdown() → release lock
```

This means validation and journal export never run simultaneously — they queue behind the lock. For the expected volume (~30–50 validations/hour, hourly journal cycle), this is sufficient.

### Validation vs journal priority

Validation requests arrive via HTTP (FastAPI executor) and journal tasks via the queue worker. Both compete for `_mt5_lock` — whichever acquires it first runs. Since each MT5 operation is short-lived (5–15s), worst-case wait for validation is one journal account's MT5 time.

### HTML report builder

`_build_html_report()` must be called **while MT5 is still connected** (inside `_mt5_lock`), because it calls `mt5.history_orders_get(position=pid)` for SL/TP fallback retrieval (Metode 3 of the 4-tier SL/TP lookup). The upload to Laravel happens **outside the lock** since it's pure HTTP.

### Journal scheduler

On startup the scheduler runs the first cycle immediately, then sleeps for:
- `JOURNAL_SHORT_INTERVAL_SEC` if any account had open positions last cycle
- `JOURNAL_INTERVAL_SEC` otherwise
- Plus random jitter up to `JOURNAL_MAX_JITTER_SEC`

### Laravel API endpoints called by this service

| Endpoint | Purpose |
|---|---|
| `GET /journal/active-accounts` | Fetch all verified accounts to process |
| `GET /journal/retry-queue` | Accounts flagged for immediate retry |
| `POST /journal/upload-report` | Upload HTML report (`multipart/form-data`) |
| `POST /journal/report-auth-error` | Notify Laravel of login failures |
| `POST /journal/report-unknown-server` | Notify of unknown broker servers |
| `POST /journal/heartbeat` | Send cycle stats + captured WARNING/ERROR logs |
| `GET /journal/sltp-cache` | Fetch SL/TP cache from DB |
| `POST /journal/sltp-cache/sync` | Push SL/TP changes to DB |

### Files persisted locally

| Path | Purpose |
|---|---|
| `C:\validator\service.log` | Application log |
| `C:\validator\sltp_cache.json` | Local SL/TP cache (fallback if DB unreachable) |

## Laravel integration example

After calling `POST /validate` and saving the account:

```php
// After validation success and DB save:
Http::withHeaders(['X-Api-Secret' => config('services.validator.secret')])
    ->post(config('services.validator.url') . '/journal/trigger', [
        'account_id' => $account->id,
        'login'      => $account->login,
        'password'   => decrypt($account->investor_password),
        'server'     => $account->server,
    ]);
```

## Platform constraints

- **Windows only** — MT5 SDK is Windows-exclusive.
- **Python 64-bit required** — must match MT5 terminal 64-bit.
- **First-time broker login** — each broker server must be added manually in the MT5 terminal GUI at least once before Python-initiated logins work.
- **Network** — in production, expose port 8002 only via Tailscale VPN, not public internet. See `validator-self-hosted-setup.md` for full deployment guide.
