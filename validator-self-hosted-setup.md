# MT5 Account Validator — Self-Hosted Setup (Flow 2)

Dokumentasi lengkap untuk membangun **service validasi akun MetaTrader 5** menggunakan Python FastAPI + MT5 Terminal di VPS Windows sendiri. Validasi bersifat **realtime** (member dapat feedback dalam 5-15 detik saat submit akun) dengan **fallback ke lazy validation** kalau service sedang down.

> **Konteks proyek:** Fitur journal trader untuk komunitas Discord ~2000 member. Robot journal existing tetap jalan 1x/hari (by design). Validator service ini adalah komponen baru & terpisah, khusus untuk validasi akun baru saat member submit.

---

## 📋 Daftar Isi

1. [Arsitektur & Konsep](#1-arsitektur--konsep)
2. [Kebutuhan Sistem](#2-kebutuhan-sistem)
3. [Keamanan (WAJIB BACA DULU)](#3-keamanan-wajib-baca-dulu)
4. [Setup VPS Windows](#4-setup-vps-windows)
5. [Setup MT5 Terminal](#5-setup-mt5-terminal)
6. [Kode Python Validator Service](#6-kode-python-validator-service)
7. [Watchdog & Auto-Cleanup](#7-watchdog--auto-cleanup)
8. [Jalankan sebagai Windows Service (NSSM)](#8-jalankan-sebagai-windows-service-nssm)
9. [Koneksi Aman Laravel ↔ VPS (Tailscale)](#9-koneksi-aman-laravel--vps-tailscale)
10. [Integrasi Laravel](#10-integrasi-laravel)
11. [Database Schema](#11-database-schema)
12. [Fallback: Lazy Validation](#12-fallback-lazy-validation)
13. [Monitoring & Alerting](#13-monitoring--alerting)
14. [Troubleshooting](#14-troubleshooting)
15. [Checklist Deploy](#15-checklist-deploy)
16. [Maintenance Rutin](#16-maintenance-rutin)

---

## 1. Arsitektur & Konsep

### Konsep penting yang harus dipahami developer

**Validator service ini adalah API ENDPOINT, bukan robot.** Walaupun dia membuka MT5 terminal sebagai dependency:

- Dia **selalu standby** (jalan terus via uvicorn + NSSM), seperti Nginx/PHP-FPM
- Dia **pasif** — hanya bekerja saat dipanggil oleh Laravel via HTTP
- **Laravel adalah client, Python adalah server.** Arah panggilan: `Laravel → Python`
- **Tidak ada cron, tidak ada polling.** Realtime saat member submit.

Ini berbeda dari robot journal existing yang merupakan **script** (jalan via cron, kerja, lalu exit).

### Diagram alur lengkap

```
┌─────────────────────┐
│   MEMBER (Browser)  │  Submit form akun
└──────────┬──────────┘
           │ ① POST /api/trading-accounts/validate
           ▼
┌─────────────────────────────────────┐
│   LARAVEL (VPS Linux existing)      │
│   - Validate input                  │
│   - Rate limit + cache check        │
└──────────┬──────────────────────────┘
           │ ② HTTP POST (via Tailscale VPN)
           │   http://100.x.x.x:8002/validate
           ▼
┌─────────────────────────────────────┐
│   VPS WINDOWS (validator)           │
│  ┌─────────────────────────────┐    │
│  │ Python FastAPI (NSSM)       │    │
│  │ Terminal Pool orchestrator  │    │
│  └────────────┬────────────────┘    │
│               │ ③ mt5.login()       │
│               ▼                     │
│  ┌─────────────────────────────┐    │
│  │ MT5 Terminal Portable #1    │    │
│  │ MT5 Terminal Portable #2    │    │
│  │ MT5 Terminal Portable #3    │    │
│  └────────────┬────────────────┘    │
│               │ ④ result            │
│               ▼                     │
│  ┌─────────────────────────────┐    │
│  │ Python: shutdown + cleanup  │    │
│  └────────────┬────────────────┘    │
└───────────────┼─────────────────────┘
                │ ⑤ JSON response
                ▼
┌─────────────────────────────────────┐
│   LARAVEL: save DB, cache, response │
└──────────┬──────────────────────────┘
           │ ⑥ ke member
           ▼
┌─────────────────────────────────────┐
│   MEMBER: "✅ Akun valid!"          │
│   atau "⏳ Validasi tertunda"       │  ← fallback kalau validator down
└─────────────────────────────────────┘
```

---

## 2. Kebutuhan Sistem

### VPS Windows (komponen baru)

| Spec | Minimum | Recommended |
|------|---------|-------------|
| OS | Windows Server 2019 | Windows Server 2022 |
| vCPU | 2 | 2-4 |
| RAM | 2 GB | 4 GB |
| Disk | 40 GB SSD | 60 GB SSD |
| Bandwidth | Unmetered/cukup | Unmetered |

> **Catatan:** VPS hanya tersedia dalam edisi **Windows Server**, bukan Windows Desktop. Ini benar dan justru lebih cocok untuk service 24/7. "Windows Office" bukan OS — itu Microsoft Office (aplikasi).

### Provider VPS Windows yang reliable

> ⚠️ Provider lama Anda mengalami **3x downtime/bulan karena mati listrik** — itu indikasi data center tanpa UPS/generator memadai. Pertimbangkan pindah.

| Provider | Lokasi terdekat | Estimasi harga | Uptime |
|----------|----------------|----------------|--------|
| Vultr | Singapore | ~$15-20/bln | 99.99% |
| Contabo | Singapore | ~$10-15/bln | 99.9% |
| AWS Lightsail | Singapore | ~$20-30/bln | 99.99% |
| Niagahoster | Indonesia | ~Rp 300rb/bln | 99.5% |

**Rekomendasi:** Vultr Singapore — sweet spot reliability + latency untuk broker Asia.

### Software (diinstall di VPS Windows)

| Software | Versi | Sumber |
|----------|-------|--------|
| Python | 3.11+ (64-bit) | python.org |
| MetaTrader 5 Terminal | latest | metatrader5.com |
| NSSM | 2.24+ | nssm.cc |
| Tailscale | latest | tailscale.com |

### Python packages (`requirements.txt`)

```
MetaTrader5==5.0.45
fastapi==0.115.0
uvicorn[standard]==0.32.0
pydantic==2.9.0
psutil==6.0.0
```

> Versi di atas contoh; cek versi terbaru saat install. Pastikan Python **64-bit** match dengan MT5 **64-bit**.

### VPS Linux (existing — Laravel)

| Komponen | Status |
|----------|--------|
| PHP 8.1+ | ✅ Sudah ada |
| Laravel 10/11/12 | ✅ Sudah ada |
| Redis (cache) | ⚠️ Recommended |
| Tailscale | ❌ Install (untuk koneksi aman) |

---

## 3. Keamanan (WAJIB BACA DULU)

> 🚨 **JANGAN PERNAH expose port RDP atau port validator (8002) ke internet publik.** Bot brute-force RDP men-scan internet 24/7. Port custom (misal 20407) **bukan keamanan** — scanner seperti Shodan/Masscan menemukan port apapun dalam menit.

### Prinsip keamanan

1. **RDP hanya via VPN atau IP whitelist** — jangan public
2. **Validator port (8002) hanya accessible via Tailscale** — tidak public
3. **Password administrator 20+ karakter random**
4. **Aktifkan Windows Firewall** dengan rule ketat
5. **Secret token** antara Laravel ↔ Validator (header `X-Api-Secret`)
6. **Jangan share IP/port VPS** di chat, forum, screenshot, atau Discord publik

### Checklist keamanan VPS Windows

- [ ] Ganti password administrator ke yang kuat
- [ ] Install Tailscale, akses RDP via Tailscale IP saja
- [ ] Windows Firewall: blok inbound RDP dari publik, allow hanya Tailscale subnet
- [ ] Windows Firewall: port 8002 hanya allow dari Tailscale subnet
- [ ] Aktifkan auto Windows Update (atau jadwalkan agar tidak reboot saat peak)
- [ ] Install antivirus (Windows Defender cukup)
- [ ] Cek Event Viewer (Event ID 4625 = failed login) berkala

### Cek apakah VPS pernah di-brute-force

Buka **Event Viewer** → **Windows Logs** → **Security**, filter:
- **Event ID 4625** (failed login) — kalau ribuan dari IP asing, VPS sedang/pernah diserang
- **Event ID 4624** (successful login) — kalau ada login sukses dari IP yang tidak Anda kenal, **VPS compromised** → reinstall OS + ganti semua kredensial

---

## 4. Setup VPS Windows

### Langkah 1: Akses awal & hardening

1. Login RDP pertama kali dengan kredensial dari provider
2. **Segera ganti password administrator**
3. Update Windows (Settings → Update)

### Langkah 2: Install Python

1. Download Python 3.11+ **64-bit** dari https://python.org
2. Saat install, **centang "Add Python to PATH"**
3. Verifikasi di Command Prompt:
   ```cmd
   python --version
   pip --version
   ```

### Langkah 3: Siapkan folder kerja

```cmd
mkdir C:\validator
mkdir C:\mt5-pool
cd C:\validator
```

### Langkah 4: Install Python packages

```cmd
cd C:\validator
pip install MetaTrader5 fastapi "uvicorn[standard]" pydantic psutil
```

---

## 5. Setup MT5 Terminal

### Konsep: Portable Mode + Multiple Instance

Untuk menghindari **ribuan akun menumpuk di MT5** (concern penting), kita pakai:

1. **Portable mode** — semua data di folder spesifik, bukan registry Windows
2. **Multiple terminal** — untuk concurrency (3 validasi paralel)
3. **Periodic cleanup** — reset folder data berkala

### Langkah 1: Download & extract MT5

1. Download MT5 dari broker manapun (atau MetaQuotes generic)
2. Install ke folder pertama: `C:\mt5-pool\terminal-1\`

### Langkah 2: Duplikasi untuk pool

Copy folder `terminal-1` menjadi:
```
C:\mt5-pool\terminal-1\
C:\mt5-pool\terminal-2\
C:\mt5-pool\terminal-3\
```

### Langkah 3: Jalankan tiap terminal dalam portable mode

Buat file `C:\mt5-pool\start-terminals.bat`:

```bat
@echo off
start "" "C:\mt5-pool\terminal-1\terminal64.exe" /portable
timeout /t 5
start "" "C:\mt5-pool\terminal-2\terminal64.exe" /portable
timeout /t 5
start "" "C:\mt5-pool\terminal-3\terminal64.exe" /portable
echo Semua terminal MT5 dijalankan.
```

> **PENTING — login manual sekali dulu:** Buka tiap terminal, login manual ke 1 broker demo yang akan sering dipakai member (misal ICMarketsSC-Demo). Ini supaya MT5 "kenal" server tersebut. Login pertama via Python sering gagal kalau broker belum pernah ditambahkan manual.

### Langkah 4: Catat path tiap terminal

Path ini dipakai di kode Python:
```
C:\mt5-pool\terminal-1\terminal64.exe
C:\mt5-pool\terminal-2\terminal64.exe
C:\mt5-pool\terminal-3\terminal64.exe
```

---

## 6. Kode Python Validator Service

Buat file `C:\validator\validator.py`:

```python
import os
import asyncio
import threading
import logging
import shutil
from pathlib import Path
from contextlib import asynccontextmanager

import MetaTrader5 as mt5
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

# ============ CONFIG ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(r"C:\validator\validator.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("validator")

def env(key, default=""):
    return os.environ.get(key, default).strip('"').strip("'")

SECRET = env("VALIDATOR_SECRET", "ganti-dengan-secret-kuat")
HOST = env("VALIDATOR_HOST", "0.0.0.0")
PORT = int(env("VALIDATOR_PORT", "8002"))

LOGIN_TIMEOUT_MS = int(env("MT5_LOGIN_TIMEOUT_MS", "15000"))   # 15 detik
HARD_TIMEOUT_SEC = int(env("MT5_HARD_TIMEOUT_SEC", "20"))      # 20 detik
RESET_EVERY = int(env("MT5_RESET_EVERY", "50"))                # reset tiap 50 validasi

# Path tiap terminal MT5 di pool
TERMINALS = [
    {"id": 1, "path": r"C:\mt5-pool\terminal-1\terminal64.exe",
     "data": r"C:\mt5-pool\terminal-1"},
    {"id": 2, "path": r"C:\mt5-pool\terminal-2\terminal64.exe",
     "data": r"C:\mt5-pool\terminal-2"},
    {"id": 3, "path": r"C:\mt5-pool\terminal-3\terminal64.exe",
     "data": r"C:\mt5-pool\terminal-3"},
]

# ============ TERMINAL POOL ============
# CATATAN ARSITEKTUR:
# Package MetaTrader5 punya GLOBAL STATE per proses Python.
# Artinya 1 proses Python = 1 koneksi MT5 aktif pada satu waktu.
# Untuk benar-benar paralel ke banyak terminal, idealnya tiap terminal
# punya proses Python terpisah (lihat catatan di bawah).
#
# Untuk kesederhanaan & keandalan, dokumentasi ini pakai pendekatan
# SERIAL dengan 1 lock global. Throughput ~6-20 validasi/menit sudah
# cukup untuk volume 2000 member (peak ~30-50/jam).
#
# Kalau butuh paralel sejati, jalankan beberapa instance validator.py
# di port berbeda (8002, 8003, 8004), masing-masing pakai 1 terminal,
# lalu load-balance dari Laravel.

_mt5_lock = threading.Lock()
_validation_counter = {"count": 0}
_active_terminal = TERMINALS[0]  # default pakai terminal pertama

DATA_SUBDIRS = ["config", "history", "bases", "logs", "MQL5\\Logs"]


class ValidateInput(BaseModel):
    login: int
    password: str
    server: str


def _init_mt5(terminal_path: str) -> bool:
    """Inisialisasi koneksi ke terminal MT5 tertentu."""
    if mt5.terminal_info() is not None:
        return True
    return mt5.initialize(path=terminal_path, timeout=10000)


def _cleanup_terminal_data(data_dir: str):
    """Hapus data akun yang menumpuk di terminal (jaga disk & privacy).
    Terminal harus dalam keadaan shutdown saat ini dipanggil."""
    for sub in DATA_SUBDIRS:
        p = Path(data_dir) / sub
        if p.exists():
            try:
                shutil.rmtree(p, ignore_errors=True)
            except Exception as e:
                log.warning(f"Gagal cleanup {p}: {e}")


def _validate_sync(login: int, password: str, server: str) -> dict:
    """Dijalankan di thread terpisah, dilindungi lock (serial)."""
    with _mt5_lock:
        terminal = _active_terminal

        if not _init_mt5(terminal["path"]):
            err = mt5.last_error()
            log.error(f"init gagal: {err}")
            return {
                "success": False,
                "error_type": "mt5_offline",
                "error_message": "Layanan validasi tidak siap, coba lagi nanti",
            }

        try:
            ok = mt5.login(login, password=password, server=server,
                           timeout=LOGIN_TIMEOUT_MS)

            if ok:
                info = mt5.account_info()
                if info is None:
                    return {
                        "success": False,
                        "error_type": "no_account_info",
                        "error_message": "Login OK tapi info akun kosong",
                    }
                result = {
                    "success": True,
                    "balance": float(info.balance or 0),
                    "currency": info.currency or "USD",
                    "leverage": int(info.leverage or 0),
                    "name": info.name or "",
                    "company": info.company or "",
                }
            else:
                err = mt5.last_error()
                code = err[0] if isinstance(err, tuple) else 0
                msg = err[1] if isinstance(err, tuple) and len(err) > 1 else str(err)
                result = _map_error(code, msg)
        finally:
            # Selalu shutdown setelah validasi → hapus session
            mt5.shutdown()

        # Periodic cleanup data folder
        _validation_counter["count"] += 1
        if _validation_counter["count"] % RESET_EVERY == 0:
            log.info(f"Cleanup data terminal setelah "
                     f"{_validation_counter['count']} validasi")
            _cleanup_terminal_data(terminal["data"])

        return result


def _map_error(code: int, msg: str) -> dict:
    """Mapping kode error MT5 → pesan ramah member (Bahasa Indonesia)."""
    if code == -6:
        etype, human = "invalid_credentials", "Nomor akun atau password investor salah"
    elif code == -4:
        etype, human = "server_not_found", "Server broker tidak ditemukan / nama server salah"
    elif code == -2:
        etype, human = "invalid_params", "Parameter login tidak valid"
    elif code == -10005:
        etype, human = "ipc_timeout", "Terminal tidak merespons, coba lagi beberapa saat"
    elif code in (-10001, -10002, -10003, -10004):
        etype, human = "ipc_failure", "Koneksi internal terminal bermasalah"
    elif code == -8:
        etype, human = "auto_trading_disabled", "Algorithmic trading dinonaktifkan di terminal"
    else:
        etype, human = "unknown", msg or f"Error tidak dikenal (code={code})"
    return {
        "success": False,
        "error_type": etype,
        "error_code": code,
        "error_message": human,
        "raw": msg,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    log.info("Validator service startup")
    # Warm-up: pastikan terminal bisa diinisialisasi
    for i in range(15):
        try:
            ok = await asyncio.wait_for(
                loop.run_in_executor(None, _init_mt5, _active_terminal["path"]),
                timeout=12,
            )
            if ok:
                log.info("MT5 terminal siap")
                with _mt5_lock:
                    mt5.shutdown()  # lepas, baru dipakai saat ada request
                break
        except asyncio.TimeoutError:
            log.warning("init timeout, retry...")
        await asyncio.sleep(2)
    else:
        log.error("GAGAL warm-up MT5 (service tetap jalan, akan retry per request)")
    yield
    with _mt5_lock:
        try:
            mt5.shutdown()
        except Exception:
            pass
    log.info("Validator service shutdown")


app = FastAPI(title="MT5 Account Validator", lifespan=lifespan)


@app.post("/validate")
async def validate(body: ValidateInput, x_api_secret: str = Header(...)):
    if x_api_secret != SECRET:
        raise HTTPException(401, "Unauthorized")

    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None, _validate_sync,
                body.login, body.password, body.server,
            ),
            timeout=HARD_TIMEOUT_SEC,
        )
        return result
    except asyncio.TimeoutError:
        log.error(f"Hard timeout: login={body.login} server={body.server}")
        return {
            "success": False,
            "error_type": "hard_timeout",
            "error_message": "Validasi melebihi batas waktu, coba lagi nanti",
        }


@app.get("/health")
async def health():
    return {"status": "ok", "validations": _validation_counter["count"]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
```

> **Catatan tentang concurrency:** Kode di atas memproses validasi secara **serial** (1 lock global) demi keandalan. Untuk volume 2000 member dengan peak ~30-50 validasi/jam, ini cukup. Kalau suatu hari butuh paralel sejati, jalankan beberapa instance `validator.py` di port berbeda (masing-masing pakai 1 terminal dari pool) dan load-balance dari Laravel.

---

## 7. Watchdog & Auto-Cleanup

MT5 terminal kadang crash. Watchdog memastikan terminal selalu hidup.

Buat file `C:\validator\watchdog.py`:

```python
import subprocess
import psutil
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WATCHDOG] %(message)s",
    handlers=[logging.FileHandler(r"C:\validator\watchdog.log", encoding="utf-8")],
)
log = logging.getLogger("watchdog")

TERMINALS = [
    r"C:\mt5-pool\terminal-1\terminal64.exe",
    r"C:\mt5-pool\terminal-2\terminal64.exe",
    r"C:\mt5-pool\terminal-3\terminal64.exe",
]

def count_mt5_running():
    return sum(
        1 for p in psutil.process_iter(['name'])
        if p.info['name'] and 'terminal64.exe' in p.info['name'].lower()
    )

def start_terminal(path):
    subprocess.Popen([path, "/portable"])
    log.info(f"Restart terminal: {path}")

def main():
    running = count_mt5_running()
    expected = len(TERMINALS)
    if running < expected:
        log.warning(f"Hanya {running}/{expected} terminal hidup, restart...")
        for path in TERMINALS:
            start_terminal(path)
            time.sleep(8)

if __name__ == "__main__":
    main()
```

**Jadwalkan watchdog via Task Scheduler** (tiap 2 menit):

1. Buka **Task Scheduler**
2. Create Task → Name: `MT5 Watchdog`
3. Trigger: **Repeat every 2 minutes**, indefinitely
4. Action: Start program → `C:\Python311\python.exe`, argument: `C:\validator\watchdog.py`
5. Settings: centang "Run whether user is logged on or not"

---

## 8. Jalankan sebagai Windows Service (NSSM)

Supaya Python FastAPI selalu hidup & auto-restart kalau crash.

### Langkah 1: Download NSSM

Download dari https://nssm.cc, extract ke `C:\nssm\`

### Langkah 2: Install service

```cmd
C:\nssm\nssm.exe install MT5Validator
```

Di GUI yang muncul:
- **Path:** `C:\Python311\python.exe`
- **Startup directory:** `C:\validator`
- **Arguments:** `-m uvicorn validator:app --host 0.0.0.0 --port 8002`

Tab **Environment** (set variabel):
```
VALIDATOR_SECRET=secret-kuat-anda-disini
VALIDATOR_PORT=8002
MT5_LOGIN_TIMEOUT_MS=15000
MT5_HARD_TIMEOUT_SEC=20
MT5_RESET_EVERY=50
```

Tab **Details** → Startup type: `Automatic`

### Langkah 3: Start service

```cmd
C:\nssm\nssm.exe start MT5Validator
```

### Perintah berguna

```cmd
nssm status MT5Validator      # cek status
nssm restart MT5Validator     # restart
nssm stop MT5Validator        # stop
nssm edit MT5Validator        # edit config
```

### Verifikasi

Dari VPS Windows sendiri:
```cmd
curl http://localhost:8002/health
```
Harus return: `{"status":"ok","validations":0}`

---

## 9. Koneksi Aman Laravel ↔ VPS (Tailscale)

> **Jangan expose port 8002 ke publik.** Pakai Tailscale — VPN mesh gratis yang bikin VPS Linux & VPS Windows seolah dalam 1 jaringan privat.

### Langkah 1: Install Tailscale di VPS Windows

1. Download dari https://tailscale.com/download/windows
2. Install & login dengan akun Tailscale Anda
3. Catat **Tailscale IP** VPS Windows (format `100.x.x.x`)

### Langkah 2: Install Tailscale di VPS Linux

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

### Langkah 3: Firewall — hanya allow Tailscale

Di VPS Windows, buka **Windows Defender Firewall with Advanced Security**:

1. Inbound Rules → New Rule
2. Port → TCP → 8002
3. Allow the connection
4. **Scope** → Remote IP → hanya allow subnet Tailscale `100.64.0.0/10`
5. Apply

Sekarang port 8002 **hanya bisa diakses** dari dalam jaringan Tailscale, tidak dari internet publik.

### Langkah 4: Test dari VPS Linux

```bash
curl http://100.x.x.x:8002/health
```

Ganti `100.x.x.x` dengan Tailscale IP VPS Windows.

---

## 10. Integrasi Laravel

### Config — `config/services.php`

```php
'validator' => [
    'url'     => env('VALIDATOR_URL', 'http://100.x.x.x:8002'),
    'secret'  => env('VALIDATOR_SECRET'),
    'timeout' => (int) env('VALIDATOR_TIMEOUT', 25),
],
```

### `.env`

```env
VALIDATOR_URL=http://100.x.x.x:8002
VALIDATOR_SECRET=secret-kuat-anda-disini
VALIDATOR_TIMEOUT=25
```

### Service — `app/Services/AccountValidator.php`

```php
<?php

namespace App\Services;

use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Http\Client\ConnectionException;

class AccountValidator
{
    public function __construct(
        protected string $url,
        protected string $secret,
        protected int $timeout = 25,
    ) {}

    /**
     * @return array{success: bool, status: string, ...}
     */
    public function validate(int $login, string $password, string $server): array
    {
        try {
            $response = Http::withHeaders(['X-Api-Secret' => $this->secret])
                ->timeout($this->timeout)
                ->connectTimeout(8)
                ->post("{$this->url}/validate", [
                    'login'    => $login,
                    'password' => $password,
                    'server'   => $server,
                ]);
        } catch (ConnectionException $e) {
            // Validator service DOWN → fallback ke lazy validation
            Log::warning('Validator unavailable', ['login' => $login]);
            return [
                'success' => false,
                'status'  => 'queued',     // ← signal fallback
                'error_type' => 'validator_unavailable',
                'error_message' => 'Layanan validasi sedang sibuk. '
                    . 'Akun Anda akan divalidasi otomatis dalam maksimal 24 jam.',
            ];
        } catch (\Throwable $e) {
            Log::error('Validator error', ['msg' => $e->getMessage()]);
            return [
                'success' => false,
                'status'  => 'queued',
                'error_type' => 'internal',
                'error_message' => 'Terjadi kesalahan, akun akan divalidasi otomatis.',
            ];
        }

        $data = $response->json() ?? [];

        if (($data['success'] ?? false) === true) {
            return [
                'success'  => true,
                'status'   => 'verified',
                'balance'  => $data['balance'] ?? 0,
                'currency' => $data['currency'] ?? 'USD',
                'leverage' => $data['leverage'] ?? 0,
            ];
        }

        return [
            'success'       => false,
            'status'        => 'invalid',
            'error_type'    => $data['error_type'] ?? 'unknown',
            'error_message' => $data['error_message'] ?? 'Validasi gagal',
        ];
    }
}
```

### Service Provider — `app/Providers/AppServiceProvider.php`

```php
use App\Services\AccountValidator;

public function register(): void
{
    $this->app->singleton(AccountValidator::class, function () {
        return new AccountValidator(
            url:     config('services.validator.url'),
            secret:  config('services.validator.secret'),
            timeout: config('services.validator.timeout'),
        );
    });
}
```

### Controller — `app/Http/Controllers/TradingAccountController.php`

```php
<?php

namespace App\Http\Controllers;

use App\Models\TradingAccount;
use App\Services\AccountValidator;
use Illuminate\Http\Request;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\RateLimiter;

class TradingAccountController extends Controller
{
    public function store(Request $request, AccountValidator $validator): JsonResponse
    {
        $data = $request->validate([
            'login'    => ['required', 'integer', 'min:1'],
            'password' => ['required', 'string', 'min:4', 'max:100'],
            'server'   => ['required', 'string', 'max:100'],
        ]);

        $userId = $request->user()->id;

        // === Rate limit: max 5 validasi/menit per user ===
        $rateKey = "validate:{$userId}";
        if (RateLimiter::tooManyAttempts($rateKey, 5)) {
            return response()->json([
                'success' => false,
                'error_type' => 'rate_limited',
                'error_message' => 'Terlalu banyak percobaan, tunggu '
                    . RateLimiter::availableIn($rateKey) . ' detik',
            ], 429);
        }
        RateLimiter::hit($rateKey, 60);

        // === Cache hasil invalid 1 jam ===
        $cacheKey = sprintf('mt:%d:%s:%s',
            $data['login'], $data['server'], md5($data['password']));
        if ($cached = Cache::get($cacheKey)) {
            if (!$cached['success']) {
                return response()->json($cached + ['from_cache' => true]);
            }
        }

        // === Panggil validator ===
        $result = $validator->validate(
            $data['login'], $data['password'], $data['server']
        );

        // Cache: invalid 1 jam, valid 5 menit (jangan cache 'queued')
        if (in_array($result['status'], ['verified', 'invalid'])) {
            Cache::put($cacheKey, $result,
                $result['success'] ? 300 : 3600);
        }

        // === Simpan ke DB ===
        TradingAccount::updateOrCreate(
            ['user_id' => $userId, 'login' => $data['login'], 'server' => $data['server']],
            [
                'investor_password' => encrypt($data['password']),
                'status'      => $result['status'],   // verified | invalid | queued
                'verified_at' => $result['success'] ? now() : null,
                'last_error'  => $result['error_message'] ?? null,
            ]
        );

        return response()->json($result);
    }
}
```

### Route — `routes/api.php`

```php
use App\Http\Controllers\TradingAccountController;

Route::middleware('auth:sanctum')->group(function () {
    Route::post('/trading-accounts/validate',
        [TradingAccountController::class, 'store']);
});
```

---

## 11. Database Schema

```bash
php artisan make:migration create_trading_accounts_table
```

```php
<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        Schema::create('trading_accounts', function (Blueprint $table) {
            $table->id();
            $table->foreignId('user_id')->constrained()->cascadeOnDelete();
            $table->unsignedBigInteger('login');
            $table->string('server', 100);
            $table->text('investor_password');          // encrypted
            $table->enum('status', ['queued', 'verified', 'invalid', 'inactive'])
                  ->default('queued');
            $table->string('last_error')->nullable();
            $table->timestamp('verified_at')->nullable();
            $table->timestamp('last_checked_at')->nullable();
            $table->timestamps();

            $table->unique(['user_id', 'login', 'server']);
            $table->index('status');
        });
    }

    public function down(): void
    {
        Schema::dropIfExists('trading_accounts');
    }
};
```

```bash
php artisan migrate
```

---

## 12. Fallback: Lazy Validation

Saat validator service down, akun disimpan dengan status `queued`. Robot journal harian existing Anda yang akan memproses ulang.

### Modifikasi robot journal Python (existing)

```python
def process_pending_accounts():
    """Dipanggil dalam run harian robot journal.
    Proses akun yang status-nya 'queued' (gagal validasi realtime)."""

    pending = fetch_accounts_by_status('queued')  # query ke DB Laravel

    for acc in pending:
        if not mt5.initialize(timeout=10000):
            continue

        ok = mt5.login(acc['login'],
                       password=decrypt(acc['investor_password']),
                       server=acc['server'],
                       timeout=15000)

        if ok:
            update_account_status(acc['id'], 'verified')
            notify_member(acc['user_id'],
                          f"✅ Akun {acc['login']} berhasil divalidasi")
        else:
            err = mt5.last_error()
            code = err[0] if isinstance(err, tuple) else 0
            msg = map_error_to_indonesian(code)
            update_account_status(acc['id'], 'invalid', last_error=msg)
            notify_member(acc['user_id'],
                          f"❌ Akun {acc['login']} gagal: {msg}")

        mt5.shutdown()
```

### Pesan ke member saat queued

```
✓ Akun berhasil terdaftar (12345678)
⏳ Sedang dalam antrian validasi.
   Anda akan menerima notifikasi via Discord
   dalam maksimal 24 jam.
```

---

## 13. Monitoring & Alerting

### UptimeRobot (gratis)

1. Daftar di https://uptimerobot.com
2. Add Monitor → HTTP(s)
3. URL: `http://100.x.x.x:8002/health` (perlu monitor dari dalam Tailscale, atau gunakan health endpoint terpisah)

> Karena port 8002 hanya accessible via Tailscale, untuk monitoring eksternal Anda bisa buat endpoint health proxy di Laravel yang mengecek validator, lalu UptimeRobot monitor endpoint Laravel itu.

### Health proxy di Laravel

```php
// routes/web.php
Route::get('/health/validator', function (\App\Services\AccountValidator $v) {
    try {
        $r = \Illuminate\Support\Facades\Http::timeout(5)
            ->get(config('services.validator.url') . '/health');
        return response()->json([
            'validator' => $r->successful() ? 'up' : 'down',
        ], $r->successful() ? 200 : 503);
    } catch (\Throwable $e) {
        return response()->json(['validator' => 'down'], 503);
    }
});
```

Lalu UptimeRobot monitor `https://website-anda.com/health/validator`.

### Discord webhook alert

Kalau validator down, kirim alert ke channel admin Discord:

```php
// Bisa dipanggil dari scheduled command tiap 5 menit
$health = Http::timeout(5)->get(config('services.validator.url').'/health');

if (!$health->successful()) {
    Http::post(env('DISCORD_ADMIN_WEBHOOK'), [
        'content' => '🚨 Validator service DOWN! Cek VPS Windows.',
    ]);
}
```

---

## 14. Troubleshooting

### Validator return `mt5_offline`

- Cek MT5 terminal hidup: Task Manager → cari `terminal64.exe`
- Jalankan watchdog manual: `python C:\validator\watchdog.py`
- Cek log: `C:\validator\validator.log`

### Validator return `ipc_timeout` terus-menerus

- Restart terminal MT5
- Pastikan Python 64-bit match MT5 64-bit
- Cek apakah server broker pernah ditambahkan manual di terminal

### Validator return `server_not_found`

- Member salah ketik nama server
- Server broker tersebut belum pernah login manual di terminal pool
- **Solusi:** login manual sekali untuk broker yang sering dipakai member

### Laravel selalu dapat `validator_unavailable`

- Cek Tailscale connect: `tailscale status` di kedua VPS
- Cek firewall VPS Windows allow port 8002 dari subnet Tailscale
- Test manual: `curl http://100.x.x.x:8002/health` dari VPS Linux

### Disk VPS Windows penuh

- Folder data MT5 menumpuk → cek `MT5_RESET_EVERY` jalan
- Manual cleanup: stop service, hapus `C:\mt5-pool\terminal-*\history`, `\bases`, `\logs`

### Validasi lambat (>20 detik)

- Server broker member memang lambat
- Kurangi `MT5_LOGIN_TIMEOUT_MS` (tapi jangan terlalu kecil, false negative)
- Pertimbangkan multi-instance untuk paralel

---

## 15. Checklist Deploy

### VPS Windows

- [ ] OS Windows Server update terbaru
- [ ] Password administrator kuat (20+ karakter)
- [ ] Tailscale install & connect
- [ ] RDP hanya via Tailscale (firewall blok publik)
- [ ] Python 3.11+ 64-bit install + PATH
- [ ] Packages install (`pip install -r requirements.txt`)
- [ ] MT5 pool (3 terminal) install di `C:\mt5-pool\`
- [ ] Login manual sekali ke broker demo utama di tiap terminal
- [ ] `validator.py` & `watchdog.py` di `C:\validator\`
- [ ] NSSM service `MT5Validator` install + automatic startup
- [ ] Watchdog di Task Scheduler (tiap 2 menit)
- [ ] Firewall port 8002 hanya allow subnet Tailscale
- [ ] Test `curl http://localhost:8002/health` → OK

### VPS Linux (Laravel)

- [ ] Tailscale install & connect
- [ ] `.env` set `VALIDATOR_URL` (Tailscale IP) & `VALIDATOR_SECRET`
- [ ] Secret sama persis dengan di VPS Windows
- [ ] `config/services.php` updated
- [ ] `AccountValidator` service + provider binding
- [ ] Controller + route
- [ ] Migration `trading_accounts` dijalankan
- [ ] Redis untuk cache aktif
- [ ] Robot journal modified untuk handle status `queued` (fallback)
- [ ] Test end-to-end: submit akun demo dari website

### Monitoring

- [ ] Health proxy endpoint di Laravel
- [ ] UptimeRobot monitor health endpoint
- [ ] Discord webhook alert untuk admin
- [ ] Test: matikan validator → pastikan fallback `queued` jalan + alert masuk

---

## 16. Maintenance Rutin

| Frekuensi | Task |
|-----------|------|
| Harian (otomatis) | Robot journal proses akun `queued` |
| Tiap 2 menit (otomatis) | Watchdog cek terminal hidup |
| Tiap 50 validasi (otomatis) | Cleanup data folder MT5 |
| Mingguan (manual/cron) | Cek disk usage, log error, restart service |
| Bulanan (manual) | Review Event Viewer (security), update Windows, update MT5 |
| Saat broker baru muncul | Login manual broker baru di terminal pool |

### Script restart mingguan (opsional)

Buat scheduled task tiap Minggu 03:00 WIB:

```bat
@echo off
C:\nssm\nssm.exe stop MT5Validator
timeout /t 5
taskkill /F /IM terminal64.exe
timeout /t 5
rmdir /S /Q C:\mt5-pool\terminal-1\history
rmdir /S /Q C:\mt5-pool\terminal-1\bases
rmdir /S /Q C:\mt5-pool\terminal-1\logs
rem ulangi untuk terminal-2, terminal-3
call C:\mt5-pool\start-terminals.bat
timeout /t 15
C:\nssm\nssm.exe start MT5Validator
```

---

## Ringkasan Biaya

| Item | Biaya/bulan |
|------|-------------|
| VPS Linux (existing) | (sudah ada) |
| VPS Windows (validator) | Rp 200-300rb |
| Tailscale | Rp 0 (free tier) |
| UptimeRobot | Rp 0 (free tier) |
| **Total tambahan** | **~Rp 200-300rb (flat)** |

Biaya **tidak naik** seiring jumlah validasi. Predictable untuk 2000+ member.

---

## Catatan Penting

1. **Validator = bonus UX, lazy validation = safety net.** Kalau validator down, sistem tetap jalan via robot journal harian. Member tidak pernah stuck.

2. **Jangan expose port apapun ke publik.** Selalu via Tailscale.

3. **Privacy:** Investor password di-encrypt di DB Laravel. Di MT5 terminal, session di-shutdown setelah tiap validasi & data di-cleanup berkala.

4. **Pindah provider VPS** kalau downtime masih sering. Pondasi reliable lebih penting dari arsitektur canggih.

---

**Dokumen versi 1.0** — untuk update, edit di repository `docs/validator-self-hosted-setup.md`
