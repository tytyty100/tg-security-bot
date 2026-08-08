import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot

PORT = int(os.environ.get("PORT", 8080))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"ok"
        self.send_response(200 if self.path in ("/", "/health") else 404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve_forever():
    ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=serve_forever, daemon=True).start()
    bot.main()
