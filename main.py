import json
import os
import sys
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot
from config import TOKEN
from telegram.error import Conflict

PORT = int(os.environ.get("PORT", 8080))
START_TS = time.time()

LAST_ERROR = {"ts": 0.0, "text": ""}

# Render сам проставляет RENDER_EXTERNAL_URL. Если он есть — мы в облаке и считаемся
# основным (primary) инстансом. Локальная копия на ПК при конфликте должна уступить.
IS_RENDER = bool(os.environ.get("RENDER_EXTERNAL_URL"))
PRIMARY = os.environ.get("BOT_PRIMARY", "render" if IS_RENDER else "local")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/status", "/health.json", "/health"):
            body = json.dumps(
                {
                    "ok": True,
                    "token_set": bool(TOKEN),
                    "primary": PRIMARY == "render",
                    "uptime": round(time.time() - START_TS, 1),
                    "last_error": LAST_ERROR["text"] or None,
                    "last_error_ts": LAST_ERROR["ts"] or None,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            content_type = "application/json"
        else:
            body = b"ok"
            content_type = "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve_forever():
    ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


def keepalive_forever():
    # Render free засыпает после 15 мин без входящего трафика. Самообращение по
    # внешнему URL проходит через роутер Render и держит сервис активным.
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        return
    url = url.rstrip("/") + "/health"
    while True:
        time.sleep(300)
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass


def run_bot():
    conflict_backoff = 5.0
    while True:
        started = time.time()
        try:
            bot.main()
        except Conflict as exc:
            LAST_ERROR["ts"] = time.time()
            LAST_ERROR["text"] = f"{exc!r}"
            sys.stderr.write(f"BOT Conflict: {exc!r}\n")
            if PRIMARY == "local":
                # Локальная копия — уступаем облаку (Render), чтобы не душить основной инстанс.
                sys.stderr.write(
                    "Duplicate instance detected (Render is primary). "
                    "Local copy yields and exits.\n"
                )
                os._exit(0)
            # На Render мы primary: откатываемся и пробуем снова — дубль (ПК) должен умереть.
            sys.stderr.write(
                f"BOT Conflict on Render (duplicate running). "
                f"Backing off {conflict_backoff:.0f}s, will retry.\n"
            )
            time.sleep(conflict_backoff)
            conflict_backoff = min(conflict_backoff * 2, 600.0)
            continue
        except BaseException as exc:  # noqa: BLE001 - супервизор переживает любые падения
            tb = traceback.format_exc()
            LAST_ERROR["ts"] = time.time()
            LAST_ERROR["text"] = f"{exc!r}"
            sys.stderr.write(f"BOT STOPPED: {exc!r}\n{tb}\n")
            conflict_backoff = 5.0
            if isinstance(exc, SystemExit):
                sys.stderr.write("BOT got SystemExit, exiting process.\n")
                return
        else:
            sys.stderr.write("BOT exited normally without exception.\n")
            conflict_backoff = 5.0

        # Если бот упал почти сразу — это crash-loop: увеличиваем паузу, чтобы не дёргать
        # Telegram (409 Conflict при нескольких инстансах) и не разгонять память.
        ran_for = time.time() - started
        delay = 5.0
        if ran_for < 60:
            delay = min(60.0, 5.0 + (60.0 - ran_for))
        sys.stderr.write(f"BOT restarting in {delay:.0f}s (ran {ran_for:.0f}s)\n")
        time.sleep(delay)


if __name__ == "__main__":
    threading.Thread(target=serve_forever, daemon=True).start()
    threading.Thread(target=keepalive_forever, daemon=True).start()
    sys.stderr.write(f"Health server listening on port {PORT} (primary={PRIMARY == 'render'})\n")
    if not TOKEN:
        sys.stderr.write("FATAL: BOT_TOKEN env var not set on Render\n")
        while True:
            time.sleep(3600)
    run_bot()
