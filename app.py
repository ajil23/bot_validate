import os
import asyncio
import threading
import logging
import subprocess
from pathlib import Path
from contextlib import asynccontextmanager
from dotenv import load_dotenv

load_dotenv()

import MetaTrader5 as mt5
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

# ============ LOGGING ============
log_dir = Path(r"C:\validator")
log_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "validator.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("validator")

# ============ CONFIG ============
def env(key, default=""):
    val = os.environ.get(key, default)
    return val.strip('"').strip("'")

SECRET   = env("VALIDATOR_SECRET", "rahasia123")
HOST     = env("VALIDATOR_HOST", "0.0.0.0")
PORT     = int(env("VALIDATOR_PORT", "8002"))
MT5_EXE  = env("MT5_EXE", r"C:\Program Files\MetaTrader 5\terminal64.exe")

LOGIN_TIMEOUT_MS = int(env("MT5_LOGIN_TIMEOUT_MS", "3000"))
HARD_TIMEOUT_SEC = int(env("MT5_HARD_TIMEOUT_SEC", "60"))

_mt5_lock = threading.Lock()
_validation_counter = {"count": 0}
_mt5_data_path = {"path": None}  # diisi saat pertama kali initialize berhasil

RESET_EVERY  = int(env("MT5_RESET_EVERY", "50"))
DATA_SUBDIRS = ["history", "bases", "logs", "config", r"MQL5\Logs"]


def _cleanup_terminal_data():
    """Hapus data akun yang menumpuk di terminal (jaga disk & privacy)."""
    data_dir = _mt5_data_path["path"]
    if not data_dir:
        log.warning("Cleanup skip: data path belum diketahui")
        return
    import shutil
    for sub in DATA_SUBDIRS:
        p = Path(data_dir) / sub
        if p.exists():
            try:
                shutil.rmtree(p, ignore_errors=True)
                log.info(f"Cleanup: {p}")
            except Exception as e:
                log.warning(f"Gagal cleanup {p}: {e}")


class ValidateInput(BaseModel):
    login: int
    password: str
    server: str


def _map_error(code: int, msg: str) -> dict:
    error_map = {
        -6:     ("invalid_credentials", "Nomor akun atau password investor salah"),
        -4:     ("server_not_found",    "Server broker tidak ditemukan / nama server salah"),
        -2:     ("invalid_params",      "Parameter login tidak valid"),
        -10005: ("ipc_timeout",         "Terminal tidak merespons, coba lagi beberapa saat"),
    }
    etype, human = error_map.get(code, ("unknown", msg or f"Error tidak dikenal (code={code})"))
    return {"success": False, "error_type": etype, "error_code": code, "error_message": human, "raw": msg}


def _validate_sync(login: int, password: str, server: str) -> dict:
    with _mt5_lock:
        # Selalu shutdown dulu, lalu initialize dengan credentials langsung
        # → bypass 'Authorization failed' & tidak bergantung pada sesi terminal
        mt5.shutdown()

        ok = mt5.initialize(
            path=MT5_EXE,
            login=login,
            password=password,
            server=server,
            timeout=LOGIN_TIMEOUT_MS,
        )

        try:
            if not ok:
                err  = mt5.last_error()
                code = err[0] if isinstance(err, tuple) else 0
                msg  = err[1] if isinstance(err, tuple) and len(err) > 1 else str(err)
                log.warning(f"Init+Login FAILED: {login}@{server} code={code}")
                return _map_error(code, msg)

            # Simpan data path untuk cleanup (sekali saja)
            if _mt5_data_path["path"] is None:
                t = mt5.terminal_info()
                if t:
                    _mt5_data_path["path"] = t.data_path
                    log.info(f"MT5 data path: {t.data_path}")

            info = mt5.account_info()
            if info is None:
                return {"success": False, "error_type": "no_account_info",
                        "error_message": "Login OK tapi info akun kosong"}

            log.info(f"Login OK: {login} balance={info.balance}")
            result = {
                "success":  True,
                "balance":  float(info.balance or 0),
                "currency": info.currency or "USD",
                "leverage": int(info.leverage or 0),
            }
        finally:
            mt5.shutdown()

        # Periodic cleanup
        _validation_counter["count"] += 1
        if _validation_counter["count"] % RESET_EVERY == 0:
            log.info(f"Periodic cleanup setelah {_validation_counter['count']} validasi")
            _cleanup_terminal_data()

        return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Validator service started")

    # Auto-launch MT5 kalau belum running
    import psutil
    already_running = any(
        p.info["name"] and "terminal64" in p.info["name"].lower()
        for p in psutil.process_iter(["name"])
    )
    if not already_running:
        exe = Path(MT5_EXE)
        if exe.exists():
            subprocess.Popen([str(exe)])
            log.info("MT5 launched, waiting 15 seconds...")
            await asyncio.sleep(15)
        else:
            log.error(f"MT5 exe not found: {MT5_EXE}")
    else:
        log.info("MT5 already running")

    yield
    log.info("Validator service stopped")


app = FastAPI(title="MT5 Account Validator", lifespan=lifespan)


@app.post("/validate")
async def validate(body: ValidateInput, x_api_secret: str = Header(...)):
    if x_api_secret != SECRET:
        raise HTTPException(401, "Unauthorized")

    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _validate_sync, body.login, body.password, body.server),
            timeout=HARD_TIMEOUT_SEC,
        )
        return result
    except asyncio.TimeoutError:
        log.error(f"Hard timeout: {body.login}@{body.server}")
        return {"success": False, "error_type": "hard_timeout",
                "error_message": "Validasi melebihi batas waktu, coba lagi nanti"}


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
