"""Sign in to a provider in the browser and get its key, with nothing to copy by hand.

OpenRouter only (OAuth with PKCE): Talos opens OpenRouter's page, you press Authorize there, and
OpenRouter sends a one-time code to this computer, which Talos trades for a key. The key never
passes through the browser's address bar, and the code is useless without the secret made here.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from . import util

PAGE = ("<!doctype html><meta charset=utf-8><title>Talos</title><body style='font:18px system-ui;margin:3em'>"
        "<p>{}</p><p>You can close this tab.</p>")


def openrouter(show=print, base="https://openrouter.ai", wait=300, open_browser=webbrowser.open) -> str:
    """The key OpenRouter gives after Authorize is pressed. Raises ValueError with a plain reason."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    got = threading.Event()
    found = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            code = (parse_qs(urlparse(self.path).query).get("code") or [""])[0]
            body = PAGE.format("Talos has the code and is fetching the key." if code
                               else "This address is only for the answer from OpenRouter.").encode()
            self.send_response(200 if code else 404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            if code:
                found["code"] = code
                got.set()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        address = base + "/auth?" + urlencode({
            "callback_url": f"http://localhost:{server.server_address[1]}/",
            "code_challenge": challenge, "code_challenge_method": "S256"})
        show("Your browser opens OpenRouter. Sign in if it asks, then press Authorize.\n"
             f"If no page opens, open this address yourself:\n{address}")
        try:
            open_browser(address)
        except Exception:
            pass
        if not got.wait(wait):
            raise ValueError(f"Nobody pressed Authorize within {int(wait / 60) or 1} minutes. Nothing was saved.")
    finally:
        server.shutdown()
        server.server_close()
    status, data = util.http_json("POST", base + "/api/v1/auth/keys", {
        "code": found["code"], "code_verifier": verifier, "code_challenge_method": "S256"})
    key = data.get("key") if isinstance(data, dict) else ""
    if status != 200 or not key:
        raise ValueError("OpenRouter did not give a key: " + util.explain_http_error(status, str(data.get("raw", data))))
    return key
