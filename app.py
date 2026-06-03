"""
Unified MT5 Validator + Journal Service v1.0
Sekolah Trading (setra.id)

Endpoints:
  POST /validate         — validate MT5 credentials (dipanggil Laravel saat member submit)
  POST /journal/trigger  — queue immediate journal setelah validasi sukses (dipanggil Laravel)
  GET  /health           — liveness + stats

Background:
  journal-worker     — proses antrian journal satu per satu
  journal-scheduler  — periodic cycle semua akun aktif dari Laravel API
"""

import os
import asyncio
import threading
import queue
import logging
import time
import json
import ssl
import random
import re
import tempfile
import traceback
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

try:
    import MetaTrader5 as mt5
except ImportError:
    raise SystemExit("[FATAL] pip install MetaTrader5")

try:
    import requests
    import requests.adapters
    import urllib3
except ImportError:
    raise SystemExit("[FATAL] pip install requests")

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel


# ══════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip('"').strip("'")

VALIDATOR_SECRET       = _env("VALIDATOR_SECRET",         "rahasia123")
SERVICE_HOST           = _env("SERVICE_HOST",              "0.0.0.0")
SERVICE_PORT           = int(_env("SERVICE_PORT",          "8002"))

MT5_EXE                = _env("MT5_EXE",                  r"C:\Program Files\MetaTrader 5\terminal64.exe")
MT5_LOGIN_TIMEOUT_MS   = int(_env("MT5_LOGIN_TIMEOUT_MS",  "15000"))
MT5_HARD_TIMEOUT_SEC   = int(_env("MT5_HARD_TIMEOUT_SEC",  "60"))
MT5_RESET_EVERY        = int(_env("MT5_RESET_EVERY",       "50"))

JOURNAL_API_BASE       = _env("JOURNAL_API_BASE",          "http://127.0.0.1:8000/api")
JOURNAL_API_KEY        = _env("JOURNAL_API_KEY",           "setra_journal_production2026")
JOURNAL_INTERVAL_SEC   = int(_env("JOURNAL_INTERVAL_SEC",  "3600"))
JOURNAL_SHORT_SEC      = int(_env("JOURNAL_SHORT_INTERVAL_SEC", "600"))
JOURNAL_MAX_JITTER_SEC = int(_env("JOURNAL_MAX_JITTER_SEC","30"))
SCHEDULER_ENABLED      = _env("JOURNAL_SCHEDULER_ENABLED", "true").lower() == "true"


# ══════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════

_LOG_DIR = Path(r"C:\validator")
_LOG_DIR.mkdir(parents=True, exist_ok=True)


class _LogCollector(logging.Handler):
    """Kumpulkan WARNING/ERROR di memory untuk dikirim via heartbeat."""
    def __init__(self):
        super().__init__()
        self._events: list = []
        self._lock = threading.Lock()

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            with self._lock:
                self._events.append({
                    "level":     record.levelname,
                    "timestamp": datetime.fromtimestamp(record.created).isoformat(),
                    "message":   self.format(record),
                })

    def flush_events(self) -> list:
        with self._lock:
            events, self._events = list(self._events), []
        return events


_log_collector = _LogCollector()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_LOG_DIR / "service.log", encoding="utf-8"),
        logging.StreamHandler(),
        _log_collector,
    ],
)
log = logging.getLogger("service")


# ══════════════════════════════════════════════════════════════
# TLS ADAPTER  (fix handshake failure pada beberapa broker)
# ══════════════════════════════════════════════════════════════

class _TLSAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        kwargs["ssl_context"] = ctx
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return super().init_poolmanager(*args, **kwargs)


# ══════════════════════════════════════════════════════════════
# JOURNAL API CLIENT
# ══════════════════════════════════════════════════════════════

class _ApiClient:
    def __init__(self):
        self._base = JOURNAL_API_BASE.rstrip("/")
        self._key  = JOURNAL_API_KEY
        self._s    = requests.Session()
        self._s.mount("https://", _TLSAdapter())

    def _p(self) -> dict:
        return {"key": self._key}

    def get_active_accounts(self) -> list:
        r = self._s.get(f"{self._base}/journal/active-accounts", params=self._p(), timeout=15)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(f"API error: {data}")
        return data.get("data", [])

    def get_retry_queue(self) -> list:
        try:
            r = self._s.get(f"{self._base}/journal/retry-queue", params=self._p(), timeout=10)
            if r.status_code == 200:
                return r.json().get("data", [])
        except Exception as e:
            log.warning(f"get_retry_queue: {e}")
        return []

    def get_sltp_cache(self, account_ids: list) -> dict:
        if not account_ids:
            return {}
        try:
            ids = ",".join(str(i) for i in account_ids)
            r = self._s.get(f"{self._base}/journal/sltp-cache",
                            params={**self._p(), "account_ids": ids}, timeout=15)
            if r.status_code == 200:
                return r.json().get("data", {}) or {}
        except Exception as e:
            log.warning(f"get_sltp_cache: {e}")
        return {}

    def sync_sltp_cache(self, account_id: int, data: dict):
        if not data:
            return
        try:
            self._s.post(f"{self._base}/journal/sltp-cache/sync", params=self._p(),
                         json={"account_id": account_id, "data": data}, timeout=15)
        except Exception as e:
            log.warning(f"sync_sltp_cache: {e}")

    def upload_report(self, account_id: int, html_path: str) -> dict:
        import gzip
        gz_path = html_path + ".gz"
        with open(html_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            f_out.write(f_in.read())
        orig_kb = os.path.getsize(html_path) // 1024
        gz_kb   = os.path.getsize(gz_path) // 1024
        log.info(f"Report compressed: {orig_kb}KB → {gz_kb}KB")

        last_exc = None
        try:
            for attempt in range(1, 3):
                try:
                    with open(gz_path, "rb") as f:
                        r = self._s.post(
                            f"{self._base}/journal/upload-report",
                            params=self._p(),
                            data={"account_id": account_id},
                            files={"report_file": (os.path.basename(html_path) + ".gz", f, "application/gzip")},
                            timeout=300,
                        )
                    r.raise_for_status()
                    return r.json()
                except Exception as e:
                    last_exc = e
                    log.warning(f"upload_report attempt {attempt}/2: {e}")
                    if attempt < 2:
                        time.sleep(10)
        finally:
            try:
                os.remove(gz_path)
            except Exception:
                pass
        raise last_exc

    def report_auth_error(self, account_id, login, server,
                          error_type, error_code, error_message):
        try:
            self._s.post(
                f"{self._base}/journal/report-auth-error", params=self._p(),
                json={"account_id": account_id, "login_username": str(login),
                      "server_name": server, "error_type": error_type,
                      "error_code": error_code, "error_message": error_message},
                timeout=10,
            )
        except Exception as e:
            log.warning(f"report_auth_error: {e}")

    def report_unknown_server(self, server_name: str):
        try:
            self._s.post(f"{self._base}/journal/report-unknown-server", params=self._p(),
                         json={"server_name": server_name}, timeout=10)
        except Exception as e:
            log.warning(f"report_unknown_server: {e}")

    def send_heartbeat(self, stats: dict, log_events: list):
        try:
            self._s.post(f"{self._base}/journal/heartbeat", params=self._p(),
                         json={**stats, "log_events": log_events}, timeout=15)
            log.info("Heartbeat sent")
        except Exception as e:
            log.warning(f"send_heartbeat: {e}")


_api = _ApiClient()


# ══════════════════════════════════════════════════════════════
# MT5 SHARED STATE
# ══════════════════════════════════════════════════════════════

_mt5_lock        = threading.Lock()   # semua operasi MT5 harus masuk lewat sini
_mt5_data_path   = {"path": None}
_validate_count  = {"n": 0}
_DATA_SUBDIRS    = ["history", "bases", "logs", "config", r"MQL5\Logs"]
_SLTP_CACHE_FILE = _LOG_DIR / "sltp_cache.json"


def _mt5_init(login, password: str, server: str) -> bool:
    """Shutdown lalu initialize ulang MT5 dengan kredensial baru. Harus dipanggil dalam _mt5_lock."""
    mt5.shutdown()
    ok = mt5.initialize(
        path=MT5_EXE,
        login=int(login),
        password=str(password),
        server=server,
        timeout=MT5_LOGIN_TIMEOUT_MS,
    )
    if ok and _mt5_data_path["path"] is None:
        t = mt5.terminal_info()
        if t:
            _mt5_data_path["path"] = t.data_path
            log.info(f"MT5 data path: {t.data_path}")
    return ok


def _cleanup_mt5_data():
    import shutil
    dp = _mt5_data_path["path"]
    if not dp:
        return
    for sub in _DATA_SUBDIRS:
        p = Path(dp) / sub
        if p.exists():
            try:
                shutil.rmtree(p, ignore_errors=True)
                log.info(f"Cleanup: {p}")
            except Exception as e:
                log.warning(f"Cleanup failed {p}: {e}")


def _classify_error(code: int, msg: str) -> str:
    m = (msg or "").lower()
    if code in (-6, -2) or any(k in m for k in [
        "authorization failed", "invalid password", "invalid account", "invalid login",
    ]):
        return "invalid_credentials"
    if code in (-4, -10005) or any(k in m for k in [
        "invalid server", "server not found", "unknown server",
    ]):
        return "invalid_server"
    return "connection_failed"


def _error_response(code: int, msg: str) -> dict:
    _map = {
        -6:     ("invalid_credentials", "Nomor akun atau password investor salah"),
        -4:     ("server_not_found",    "Server broker tidak ditemukan / nama server salah"),
        -2:     ("invalid_params",      "Parameter login tidak valid"),
        -10005: ("ipc_timeout",         "Terminal tidak merespons, coba lagi"),
    }
    etype, human = _map.get(code, ("unknown", msg or f"Error code={code}"))
    return {"success": False, "error_type": etype, "error_code": code,
            "error_message": human, "raw": msg}


# ══════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════

def _validate_sync(login: int, password: str, server: str) -> dict:
    with _mt5_lock:
        ok = _mt5_init(login, password, server)
        try:
            if not ok:
                err  = mt5.last_error()
                code = err[0] if isinstance(err, tuple) else 0
                msg  = err[1] if isinstance(err, tuple) and len(err) > 1 else str(err)
                log.warning(f"Validate FAILED {login}@{server}: code={code}")
                return _error_response(code, msg)

            info = mt5.account_info()
            if not info:
                return {"success": False, "error_type": "invalid_credentials",
                        "error_message": "Nomor akun atau password investor salah"}

            # Pastikan akun yang login = akun yang diminta.
            # mt5.initialize() kadang return True tapi pakai sesi lama (terminal belum logout).
            if info.login != int(login):
                log.warning(f"Login mismatch: requested {login}, terminal logged as {info.login}")
                return {"success": False, "error_type": "invalid_credentials",
                        "error_message": "Nomor akun atau password investor salah"}

            log.info(f"Validate OK: {login} balance={info.balance}")
            return {
                "success":  True,
                "balance":  float(info.balance or 0),
                "currency": info.currency or "USD",
                "leverage": int(info.leverage or 0),
                "name":     info.name or "",
                "company":  info.company or "",
            }
        finally:
            mt5.shutdown()
            _validate_count["n"] += 1
            if _validate_count["n"] % MT5_RESET_EVERY == 0:
                _cleanup_mt5_data()


# ══════════════════════════════════════════════════════════════
# HTML REPORT BUILDER  (identik dengan format export manual MT5)
# Harus dipanggil saat MT5 masih terkoneksi (masih dalam _mt5_lock)
# ══════════════════════════════════════════════════════════════

def _parse_sltp_comment(comment: str):
    sl = tp = 0.0
    if comment:
        m = re.search(r'\[sl\s*([\d.]+)\]', comment, re.IGNORECASE)
        if m:
            sl = float(m.group(1))
        m = re.search(r'\[tp\s*([\d.]+)\]', comment, re.IGNORECASE)
        if m:
            tp = float(m.group(1))
    return sl, tp


def _build_html_report(account_info, deals: list, sltp_cache: dict, order_map: dict, pos_order_map: dict) -> str:
    name     = getattr(account_info, "name",     "N/A")
    login    = getattr(account_info, "login",    0)
    company  = getattr(account_info, "company",  "N/A")
    currency = getattr(account_info, "currency", "USD") or "USD"
    balance  = getattr(account_info, "balance",  0.0)
    equity   = getattr(account_info, "equity",   0.0)

    total_profit = total_deposit = total_withdraw = 0.0
    won = lost = 0
    running = peak = max_dd = 0.0

    # ── Deals rows ───────────────────────────────────────────
    deal_rows_parts = []
    for idx, d in enumerate(deals):
        d_time  = datetime.fromtimestamp(d.time).strftime("%Y.%m.%d %H:%M:%S")
        d_type  = {0:"buy",1:"sell",2:"balance",3:"credit",4:"charge",5:"correction",
                   6:"bonus"}.get(d.type, "?")
        d_entry = {0:"in",1:"out",2:"inout",3:"out_by"}.get(d.entry, "")

        if d.type == 2:
            running += d.profit
            if d.profit > 0: total_deposit  += d.profit
            else:            total_withdraw += abs(d.profit)
        else:
            running += d.profit + d.commission + d.swap + getattr(d, "fee", 0)

        if d.entry in (1, 2):
            net = d.profit + d.commission + d.swap
            total_profit += net
            if net > 0:   won  += 1
            elif net < 0: lost += 1

        peak   = max(peak, running)
        if peak > 0:
            max_dd = max(max_dd, (peak - running) / peak * 100)

        comment = d.comment or ""
        if d.order:
            ord_obj = order_map.get(d.order)
            if ord_obj:
                parts = []
                if ord_obj.sl and ord_obj.sl > 0: parts.append(f"[sl {ord_obj.sl:.5f}]")
                if ord_obj.tp and ord_obj.tp > 0: parts.append(f"[tp {ord_obj.tp:.5f}]")
                if parts:
                    comment = (comment + " " + " ".join(parts)).strip()

        display_order = d.order
        if d.entry in (1, 2) and d.order == 0 and d.position_id:
            display_order = d.position_id

        bg = "#FFFFFF" if idx % 2 == 0 else "#F7F7F7"
        deal_rows_parts.append(
            f'<tr bgcolor="{bg}" align="right">'
            f'<td>{d_time}</td><td>{d.ticket}</td><td>{d.symbol}</td>'
            f'<td>{d_type}</td><td>{d_entry}</td><td>{d.volume:.2f}</td>'
            f'<td>{d.price:.5f}</td><td>{display_order}</td><td></td>'
            f'<td>{d.commission:.2f}</td><td>{getattr(d,"fee",0):.2f}</td>'
            f'<td>{d.swap:.2f}</td><td>{d.profit:.2f}</td>'
            f'<td>{running:.2f}</td><td colspan="2">{comment}</td>'
            f'</tr>'
        )
    deal_rows = "".join(deal_rows_parts)

    # ── Positions ────────────────────────────────────────────
    pos_map: dict = {}
    for d in deals:
        if not d.position_id:
            continue
        pid = d.position_id
        if pid not in pos_map:
            pos_map[pid] = {"open": None, "close": None, "profit": 0.0,
                            "commission": 0.0, "swap": 0.0, "symbol": d.symbol,
                            "type": "", "open_order": 0,
                            "open_comment": "", "close_comment": ""}
        if d.entry == 0:
            pos_map[pid].update({"open": d, "type": "buy" if d.type == 0 else "sell",
                                 "open_order": d.order, "open_comment": d.comment or ""})
        elif d.entry in (1, 2):
            pos_map[pid]["close"]          = d
            pos_map[pid]["profit"]        += d.profit
            pos_map[pid]["commission"]    += d.commission
            pos_map[pid]["swap"]          += d.swap
            pos_map[pid]["close_comment"]  = d.comment or ""

    position_rows_parts = []
    total_trades  = 0
    for ri, (pid, p) in enumerate(pos_map.items()):
        if p["open"] is None or p["close"] is None:
            continue
        total_trades += 1
        o, c = p["open"], p["close"]
        sl = tp = 0.0

        # Metode 1: DB/local SL/TP cache
        cached = sltp_cache.get(str(pid))
        if cached:
            sl, tp = cached.get("sl", 0.0), cached.get("tp", 0.0)

        # Metode 2: opening order history
        if sl == 0 or tp == 0:
            ord_obj = order_map.get(p["open_order"])
            if ord_obj:
                if sl == 0: sl = ord_obj.sl or 0.0
                if tp == 0: tp = ord_obj.tp or 0.0

        # Metode 3: lookup dari pos_order_map (data sudah di-fetch sebelumnya, tanpa MT5 call tambahan)
        if sl == 0 or tp == 0:
            for ord in pos_order_map.get(pid, []):
                if sl == 0 and (ord.sl or 0) > 0: sl = float(ord.sl)
                if tp == 0 and (ord.tp or 0) > 0: tp = float(ord.tp)

        # Metode 4: parse dari comment field
        c_sl, c_tp = _parse_sltp_comment(p.get("close_comment", ""))
        if c_sl > 0: sl = c_sl
        if c_tp > 0: tp = c_tp
        o_sl, o_tp = _parse_sltp_comment(p.get("open_comment", ""))
        if sl == 0 and o_sl > 0: sl = o_sl
        if tp == 0 and o_tp > 0: tp = o_tp

        bg = "#FFFFFF" if ri % 2 == 0 else "#F7F7F7"
        position_rows_parts.append(
            f'<tr bgcolor="{bg}" align="right">'
            f'<td>{datetime.fromtimestamp(o.time).strftime("%Y.%m.%d %H:%M:%S")}</td>'
            f'<td>{pid}</td><td>{p["symbol"]}</td><td>{p["type"]}</td>'
            f'<td class="hidden" colspan="8"></td>'
            f'<td>{o.volume:.2f}</td><td>{o.price:.5f}</td>'
            f'<td>{sl:.5f}</td><td>{tp:.5f}</td>'
            f'<td>{datetime.fromtimestamp(c.time).strftime("%Y.%m.%d %H:%M:%S")}</td>'
            f'<td>{c.price:.5f}</td><td>{p["commission"]:.2f}</td>'
            f'<td>{p["swap"]:.2f}</td><td colspan="2">{p["profit"]:.2f}</td>'
            f'</tr>'
        )
    position_rows = "".join(position_rows_parts)

    # ── Orders ───────────────────────────────────────────────
    type_map  = {0:"buy",1:"sell",2:"buy limit",3:"sell limit",4:"buy stop",
                 5:"sell stop",6:"buy stop limit",7:"sell stop limit"}
    state_map = {1:"filled",2:"canceled",3:"partial"}
    order_rows_parts = []
    for i, ord_obj in enumerate(sorted(order_map.values(), key=lambda x: x.time_setup)):
        try:
            bg     = "#FFFFFF" if i % 2 == 0 else "#F7F7F7"
            o_time = datetime.fromtimestamp(ord_obj.time_setup).strftime("%Y.%m.%d %H:%M:%S")
            c_time = (datetime.fromtimestamp(ord_obj.time_done).strftime("%Y.%m.%d %H:%M:%S")
                      if ord_obj.time_done else "")
            price  = f"{ord_obj.price_open:.5f}" if ord_obj.price_open > 0 else "market"
            vol    = f"{ord_obj.volume_initial:.2f}/{ord_obj.volume_current:.2f}"
            sl_s   = f"{ord_obj.sl:.3f}" if ord_obj.sl and ord_obj.sl > 0 else ""
            tp_s   = f"{ord_obj.tp:.3f}" if ord_obj.tp and ord_obj.tp > 0 else ""
            order_rows_parts.append(
                f'<tr bgcolor="{bg}" align="right">'
                f'<td>{o_time}</td><td>{ord_obj.ticket}</td><td>{ord_obj.symbol}</td>'
                f'<td>{type_map.get(ord_obj.type, str(ord_obj.type))}</td>'
                f'<td>{vol}</td><td>{price}</td><td>{sl_s}</td><td>{tp_s}</td>'
                f'<td>{c_time}</td><td colspan="2">{state_map.get(ord_obj.state,"filled")}</td>'
                f'<td colspan="3">{ord_obj.comment or ""}</td>'
                f'</tr>\n'
            )
        except Exception:
            pass
    order_rows = "".join(order_rows_parts)

    pct = lambda n: f"{(n / total_trades * 100) if total_trades else 0:.2f}%"
    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><title>{login}: {name}</title>
<style>td{{font:8pt Tahoma,Arial;}} th{{font:10pt Tahoma,Arial;}} .hidden{{display:none;}}</style>
</head><body><div align="center">
<table cellspacing="1" cellpadding="3" border="0">
<tr align="center"><td colspan="15"><div style="font:14pt Tahoma"><b>Trade History Report</b></div></td></tr>
<tr><th colspan="4" align="right">Name:</th><th colspan="11"><b>{name}</b></th></tr>
<tr><th colspan="4" align="right">Account:</th><th colspan="11"><b>{login}</b></th></tr>
<tr><th colspan="4" align="right">Company:</th><th colspan="11"><b>{company}</b></th></tr>
<tr><th colspan="4" align="right">Date:</th><th colspan="11"><b>{datetime.now().strftime("%Y.%m.%d %H:%M")}</b></th></tr>
<tr><th colspan="4" align="right">Currency:</th><th colspan="11"><b>{currency}</b></th></tr>
<tr align="center"><th colspan="15" style="height:25px"><div style="font:10pt Tahoma"><b>Positions</b></div></th></tr>
<tr align="center" bgcolor="#E5F0FC">
<td><b>Time</b></td><td><b>Position</b></td><td><b>Symbol</b></td><td><b>Type</b></td>
<td><b>Volume</b></td><td><b>Price</b></td><td><b>S / L</b></td><td><b>T / P</b></td>
<td><b>Time</b></td><td><b>Price</b></td><td><b>Commission</b></td><td><b>Swap</b></td>
<td colspan="3"><b>Profit</b></td>
</tr>{position_rows}
<tr align="center"><th colspan="14" style="height:25px"><div style="font:10pt Tahoma"><b>Orders</b></div></th></tr>
<tr align="center" bgcolor="#E5F0FC">
<td><b>Open Time</b></td><td><b>Order</b></td><td><b>Symbol</b></td><td><b>Type</b></td>
<td><b>Volume</b></td><td><b>Price</b></td><td><b>S / L</b></td><td><b>T / P</b></td>
<td><b>Time</b></td><td colspan="2"><b>State</b></td><td colspan="3"><b>Comment</b></td>
</tr>{order_rows}
<tr align="center"><th colspan="15" style="height:25px"><div style="font:10pt Tahoma"><b>Deals</b></div></th></tr>
<tr align="center" bgcolor="#E5F0FC">
<td><b>Time</b></td><td><b>Deal</b></td><td><b>Symbol</b></td><td><b>Type</b></td>
<td><b>Direction</b></td><td><b>Volume</b></td><td><b>Price</b></td><td><b>Order</b></td>
<td></td><td><b>Commission</b></td><td><b>Fee</b></td><td><b>Swap</b></td>
<td><b>Profit</b></td><td><b>Balance</b></td><td colspan="2"><b>Comment</b></td>
</tr>{deal_rows}
<tr><td colspan="11"><b>Total Net Profit:</b></td><td colspan="4" align="right"><b>{total_profit:.2f}</b></td></tr>
<tr><td colspan="11"><b>Deposit:</b></td><td colspan="4" align="right"><b>{total_deposit:.2f}</b></td></tr>
<tr><td colspan="11"><b>Balance:</b></td><td colspan="4" align="right"><b>{balance:.2f}</b></td></tr>
<tr><td colspan="11"><b>Equity:</b></td><td colspan="4" align="right"><b>{equity:.2f}</b></td></tr>
<tr><td colspan="11">Profit Trades (% of total):</td>
    <td colspan="4" align="right">{won} ({pct(won)})</td></tr>
<tr><td colspan="11">Loss Trades (% of total):</td>
    <td colspan="4" align="right">{lost} ({pct(lost)})</td></tr>
</table></div></body></html>"""


# ══════════════════════════════════════════════════════════════
# SL/TP CACHE HELPERS
# ══════════════════════════════════════════════════════════════

def _load_sltp() -> dict:
    try:
        if _SLTP_CACHE_FILE.exists():
            return json.loads(_SLTP_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_sltp(cache: dict):
    try:
        _SLTP_CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# JOURNAL EXECUTION  (satu akun)
# ══════════════════════════════════════════════════════════════

def _run_journal_for(acc: dict) -> tuple:
    """
    Full journal flow untuk satu akun.
      Step 1 (dalam _mt5_lock): login → export HTML report
      Step 2 (di luar lock):    upload ke Laravel, sync SL/TP
    Returns (success: bool, open_positions: int)
    """
    acc_id   = acc["id"]
    login    = acc["login_username"]
    password = acc["password"]
    server   = acc["server_name"]

    log.info(f"[Journal] {login}@{server} (id={acc_id})")

    # Merge SL/TP cache: local file + DB
    sltp = _load_sltp()
    db_sltp = _api.get_sltp_cache([acc_id])
    for pos_id, v in db_sltp.get(str(acc_id), {}).items():
        sltp[pos_id] = v

    # ── MT5 operations (serialized via lock) ─────────────────
    filepath    = None
    open_count  = 0
    new_sltp    = {}
    error_type  = None
    error_code  = 0
    error_msg   = ""

    with _mt5_lock:
        ok = _mt5_init(login, password, server)
        if not ok:
            err        = mt5.last_error()
            error_code = err[0] if isinstance(err, tuple) else 0
            error_msg  = err[1] if isinstance(err, tuple) and len(err) > 1 else str(err)
            error_type = _classify_error(error_code, error_msg)
            mt5.shutdown()
        else:
            try:
                info = mt5.account_info()
                if not info:
                    error_type, error_code, error_msg = "no_account_info", 0, "account info kosong"
                else:
                    # Tunggu data deals tersinkron
                    time.sleep(2)
                    deals = None
                    for _ in range(3):
                        deals = mt5.history_deals_get(datetime(2020, 1, 1), datetime.now())
                        if deals is not None and len(deals) > 0:
                            break
                        time.sleep(2)
                    deals = list(deals) if deals else []

                    # Track posisi terbuka + update SL/TP cache
                    open_positions = mt5.positions_get() or []
                    open_count     = len(open_positions)
                    for pos in open_positions:
                        pos_id   = str(pos.ticket)
                        existing = sltp.get(pos_id, {})
                        new_sl   = pos.sl if pos.sl > 0 else existing.get("sl", 0.0)
                        new_tp   = pos.tp if pos.tp > 0 else existing.get("tp", 0.0)
                        if sltp.get(pos_id) != {"sl": new_sl, "tp": new_tp}:
                            new_sltp[pos_id] = {"sl": new_sl, "tp": new_tp}
                        sltp[pos_id] = {"sl": new_sl, "tp": new_tp}

                    # Build order map untuk fallback SL/TP
                    order_map: dict = {}
                    pos_order_map: dict = {}
                    try:
                        all_orders = mt5.history_orders_get(datetime(2020, 1, 1), datetime.now())
                        if all_orders:
                            order_map = {o.ticket: o for o in all_orders}
                            for o in all_orders:
                                if o.position_id:
                                    pos_order_map.setdefault(o.position_id, []).append(o)
                        log.info(f"Order history: {len(order_map)} orders")
                    except Exception as e:
                        log.warning(f"history_orders_get failed: {e}")

                    # Build HTML (di luar MT5 call — pos_order_map sudah berisi semua data)
                    html = _build_html_report(info, deals, sltp, order_map, pos_order_map)
                    tmp_dir  = tempfile.mkdtemp(prefix="journal_")
                    filepath = os.path.join(tmp_dir, f"report_{login}_{int(time.time())}.html")
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(html)
                    log.info(f"[Journal] Export OK: {login} ({len(deals)} deals, {open_count} open)")

            except Exception as e:
                error_type, error_code, error_msg = "export_failed", 0, str(e)
                log.error(f"[Journal] Export error {login}: {e}\n{traceback.format_exc()}")
            finally:
                mt5.shutdown()

    # ── Error handling ───────────────────────────────────────
    if error_type:
        if error_type == "invalid_server":
            _api.report_unknown_server(server)
        if error_type in ("invalid_credentials", "invalid_server", "connection_failed"):
            _api.report_auth_error(acc_id, login, server, error_type, error_code, error_msg)
        log.warning(f"[Journal] Failed: {login}@{server} → {error_type}")
        return False, 0

    # ── Upload (di luar lock — pure HTTP) ────────────────────
    try:
        result = _api.upload_report(acc_id, filepath)
        if result.get("success"):
            log.info(f"[Journal] Upload OK: {login}")
            if new_sltp:
                _api.sync_sltp_cache(acc_id, new_sltp)
            _save_sltp(sltp)
            return True, open_count
        else:
            log.error(f"[Journal] Upload response error: {result}")
            return False, open_count
    except Exception as e:
        log.error(f"[Journal] Upload failed {login}: {e}")
        _api.report_auth_error(acc_id, login, server, "upload_failed", 0, str(e)[:900])
        return False, open_count
    finally:
        try:
            os.remove(filepath)
            os.rmdir(os.path.dirname(filepath))
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# JOURNAL WORKER  (background thread)
# ══════════════════════════════════════════════════════════════

_journal_queue: queue.Queue = queue.Queue()


def _journal_worker():
    log.info("[Worker] Journal worker started")
    while True:
        acc = _journal_queue.get()
        try:
            _run_journal_for(acc)
        except Exception as e:
            log.error(f"[Worker] Unhandled error: {e}")
        finally:
            _journal_queue.task_done()


# ══════════════════════════════════════════════════════════════
# JOURNAL SCHEDULER  (background thread)
# ══════════════════════════════════════════════════════════════

_has_open_positions = threading.Event()


def _run_cycle():
    """Satu siklus lengkap: fetch semua akun aktif → proses satu per satu."""
    log.info("[Scheduler] ══ Cycle start ══")
    cycle_start = datetime.now()

    try:
        retry_accounts = _api.get_retry_queue()
        accounts       = _api.get_active_accounts()
    except Exception as e:
        log.error(f"[Scheduler] Fetch accounts failed: {e}")
        return

    retry_ids  = {a["id"] for a in retry_accounts}
    to_process = list(retry_accounts) + [a for a in accounts if a["id"] not in retry_ids]

    if not to_process:
        log.info("[Scheduler] No accounts to process")
        return

    log.info(f"[Scheduler] {len(to_process)} accounts to process")
    _api.send_heartbeat({"started_at": cycle_start.isoformat(),
                         "total_accounts": len(to_process)}, [])

    ok_count = fail_count = 0
    any_open = False

    for acc in to_process:
        try:
            ok, open_count = _run_journal_for(acc)
            if ok:
                ok_count += 1
                if open_count > 0:
                    any_open = True
            else:
                fail_count += 1
        except Exception as e:
            log.error(f"[Scheduler] Account error: {e}")
            fail_count += 1

    if any_open:
        _has_open_positions.set()
    else:
        _has_open_positions.clear()

    cycle_end = datetime.now()
    duration  = int((cycle_end - cycle_start).total_seconds())
    log.info(f"[Scheduler] Done: {ok_count} OK, {fail_count} failed ({duration}s)")

    _api.send_heartbeat({
        "started_at":         cycle_start.isoformat(),
        "finished_at":        cycle_end.isoformat(),
        "duration_seconds":   duration,
        "total_accounts":     len(to_process),
        "accounts_success":   ok_count,
        "accounts_failed":    fail_count,
        "has_open_positions": any_open,
    }, _log_collector.flush_events())


def _journal_scheduler():
    if not SCHEDULER_ENABLED:
        log.info("[Scheduler] Disabled via JOURNAL_SCHEDULER_ENABLED=false")
        return
    log.info("[Scheduler] Started")
    while True:
        try:
            _run_cycle()
        except Exception as e:
            log.error(f"[Scheduler] Fatal: {e}\n{traceback.format_exc()}")

        has_open  = _has_open_positions.is_set()
        interval  = JOURNAL_SHORT_SEC if has_open else JOURNAL_INTERVAL_SEC
        jitter    = random.randint(0, JOURNAL_MAX_JITTER_SEC)
        sleep_for = interval + jitter
        mode      = "10m (open positions)" if has_open else "1h"
        log.info(f"[Scheduler] Next cycle in {sleep_for}s ({mode})")
        time.sleep(sleep_for)


# ══════════════════════════════════════════════════════════════
# FASTAPI
# ══════════════════════════════════════════════════════════════

class ValidateInput(BaseModel):
    login:    int
    password: str
    server:   str


class JournalTriggerInput(BaseModel):
    account_id: int
    login:      str
    password:   str
    server:     str


def _auth(secret: str):
    if secret != VALIDATOR_SECRET:
        raise HTTPException(401, "Unauthorized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("══ MT5 Validator + Journal Service starting ══")
    threading.Thread(target=_journal_worker,    daemon=True, name="journal-worker").start()
    threading.Thread(target=_journal_scheduler, daemon=True, name="journal-scheduler").start()
    log.info(f"Ready on {SERVICE_HOST}:{SERVICE_PORT}")
    yield
    log.info("Service stopped")


app = FastAPI(title="MT5 Validator + Journal", version="1.0.0", lifespan=lifespan)


@app.post("/validate")
async def validate(body: ValidateInput, x_api_secret: str = Header(...)):
    """
    Validate MT5 credentials.
    Dipanggil Laravel saat member submit akun baru.
    Setelah ini, Laravel simpan ke DB lalu panggil /journal/trigger.
    """
    _auth(x_api_secret)
    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _validate_sync, body.login, body.password, body.server),
            timeout=MT5_HARD_TIMEOUT_SEC,
        )
        return result
    except asyncio.TimeoutError:
        log.error(f"Hard timeout: {body.login}@{body.server}")
        return {"success": False, "error_type": "hard_timeout",
                "error_message": "Validasi timeout, coba lagi nanti"}


@app.post("/journal/trigger")
async def journal_trigger(body: JournalTriggerInput, x_api_secret: str = Header(...)):
    """
    Queue immediate journal export untuk akun yang baru saja divalidasi.
    Dipanggil Laravel setelah validasi berhasil dan account_id sudah tersimpan di DB.
    """
    _auth(x_api_secret)
    acc = {
        "id":             body.account_id,
        "login_username": body.login,
        "password":       body.password,
        "server_name":    body.server,
    }
    _journal_queue.put(acc)
    log.info(f"[Trigger] Queued: {body.login}@{body.server} (id={body.account_id})")
    return {"success": True, "queue_size": _journal_queue.qsize()}


@app.get("/health")
async def health():
    return {
        "status":             "ok",
        "journal_queue":      _journal_queue.qsize(),
        "has_open_positions": _has_open_positions.is_set(),
        "validations_done":   _validate_count["n"],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVICE_HOST, port=SERVICE_PORT)
