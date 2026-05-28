import os
import MetaTrader5 as mt5
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from contextlib import asynccontextmanager

def env(key, default=""):
    val = os.environ.get(key, default)
    return val.strip('"').strip("'")

SECRET = env("VALIDATOR_SECRET", "rahasia123")
HOST = env("VALIDATOR_HOST", "0.0.0.0")
PORT = int(env("VALIDATOR_PORT", "8002"))
MT5_PATH = env("MT5_PATH")


class ValidateInput(BaseModel):
    login: int
    password: str
    server: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Init MT5 di startup, tunggu sampai siap
    for i in range(30):
        if init_mt5():
            print("[Validator] MT5 terminal siap")
            break
        print(f"[Validator] Menunggu MT5 ({i+1}/30)...")
        import asyncio
        await asyncio.sleep(2)
    else:
        print("[Validator] GAGAL inisialisasi MT5")
    yield
    mt5.shutdown()


app = FastAPI(title="Journal Validator", lifespan=lifespan)


def init_mt5() -> bool:
    if mt5.terminal_info() is not None:
        return True
    if MT5_PATH:
        return mt5.initialize(path=MT5_PATH)
    return mt5.initialize()


@app.post("/validate")
def validate(body: ValidateInput, x_api_secret: str = Header(...)):
      if x_api_secret != SECRET:
          raise HTTPException(401, "Unauthorized")

      if not init_mt5():
          return {
              "success": False,
              "error_type": "mt5_offline",
              "error_message": "Tidak dapat terhubung ke MT5 terminal",
          }

      # === Attempt login ===
      print(f"[Validator] Attempting login: {body.login}@{body.server}")
      ok = mt5.login(body.login, password=body.password, server=body.server, timeout=30000)
      print(f"[Validator] Login result: {ok}")

      # === Check error SETELAH login attempt ===
      err = mt5.last_error()
      print(f"[Validator] Error state: {err}")

      # Jika ada error (code != 0 dan != 1), langsung return error
      if err is not None and err[0] not in (0, 1):
          code = err[0] if isinstance(err, tuple) else 0
          error_map = {
              2: "invalid_account",
              -6: "invalid_credentials",
              -10005: "timeout",
          }
          error_type = error_map.get(code, f"error_code_{code}")
          print(f"[Validator] Login FAILED: {error_type} (code {code})")
          return {
              "success": False,
              "error_type": error_type,
              "error_code": code,
              "error_message": str(err),
          }

      # === Jika tidak ada error, login BERHASIL ===
      info = mt5.account_info()
      print(f"[Validator] Login SUCCESSFUL for {body.login}, balance: {info.balance if info else 0}")
      return {
          "success": True,
          "balance": float(info.balance) if info and info.balance else 0,
          "currency": info.currency or "USD" if info else "USD",
      }

      error_map = {
          0: "invalid_credentials",
          1: "invalid_account",
          2: "connection_failed",
          3: "timeout",
          -6: "invalid_credentials",
      }

      return {
          "success": False,
          "error_type": error_map.get(code, "unknown"),
          "error_code": code,
          "error_message": str(err),
      }


@app.get("/health")
def health():
    return {"status": "ok", "connected": mt5.terminal_info() is not None}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
