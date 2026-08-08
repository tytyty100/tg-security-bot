import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_token():
    token = os.environ.get("BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(os.path.join(BASE_DIR, "token.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


TOKEN = _load_token()
