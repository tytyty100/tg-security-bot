import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot
from config import TOKEN

PORT = int(os.environ.get("PORT", 8080))


def _token_valid():
    if not TOKEN:
        return False
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=10
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _status():
    return {
        "token_set": bool(TOKEN),
        "token_valid": _token_valid(),
    }


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/status", "/health.json"):
            body = json.dumps(_status()).encode("utf-8")
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


def run_bot():
    while True:
        try:
            bot.main()
        except Exception as exc:
            sys.stderr.write(f"BOT FAILED: {exc!r}\n")
        time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=serve_forever, daemon=True).start()
    if TOKEN:
        threading.Thread(target=run_bot, daemon=True).start()
    else:
        sys.stderr.write("BOT FAILED: BOT_TOKEN env var not set on Render\n")
    while True:
        time.sleep(3600)
