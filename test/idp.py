#!/usr/bin/env python3
"""Throwaway OIDC-ish Identity Provider for testing device code flows.

NOT for production. Runs a minimal HTTP server that implements:
  - /.well-known/openid-configuration  (discovery)
  - /.well-known/jwks.json             (public key)
  - /device/authorize                  (device code request)
  - /verify                            (user approval page)
  - /oauth/token                       (token exchange / polling / refresh)

Env vars:
  IDP_HOST         (default: localhost)
  IDP_PORT         (default: 9090)
  IDP_ISSUER       (default: http://{host}:{port})
  IDP_KEY_FILE     (PEM private key to load; ephemeral if unset)
  IDP_AUDIENCE     (JWT audience claim; omitted if unset)
  IDP_TOKEN_LIFETIME (seconds, default: 3600)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path

try:
    import jwt as pyjwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
except ImportError:
    print("Requires: pip install PyJWT cryptography", file=sys.stderr)
    sys.exit(1)

# -- Config --
HOST = os.environ.get("IDP_HOST", "localhost")
PORT = int(os.environ.get("IDP_PORT", "9090"))
ISSUER = os.environ.get("IDP_ISSUER", f"http://{HOST}:{PORT}")
TOKEN_LIFETIME = int(os.environ.get("IDP_TOKEN_LIFETIME", "3600"))
DEVICE_CODE_LIFETIME = 600

# -- RSA key --
_key_file = os.environ.get("IDP_KEY_FILE")
if _key_file and Path(_key_file).exists():
    _private_key = serialization.load_pem_private_key(
        Path(_key_file).read_bytes(), password=None
    )
else:
    _private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()

# Key ID
_pub_der = _public_key.public_bytes(
    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
)
_kid = hashlib.sha256(_pub_der).hexdigest()[:16]

# JWKS
_pub_numbers = _public_key.public_numbers()


def _b64url_uint(n: int) -> str:
    length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).decode().rstrip("=")


_jwks = {
    "keys": [{
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": _kid,
        "n": _b64url_uint(_pub_numbers.n),
        "e": _b64url_uint(_pub_numbers.e),
    }]
}

# -- State --
_lock = threading.Lock()
_pending_codes: dict[str, dict] = {}
_refresh_tokens: dict[str, dict] = {}

# -- Token minting --
_audience = os.environ.get("IDP_AUDIENCE")


def _mint_token(sub: str, scope: str = "", audience: str | None = None) -> str:
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": sub,
        "iat": now,
        "exp": now + TOKEN_LIFETIME,
    }
    if audience:
        claims["aud"] = audience
    if scope:
        claims["scp"] = scope.split()
    return pyjwt.encode(claims, _private_key, algorithm="RS256", headers={"kid": _kid})


# -- HTTP handler --

class IdPHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [{self.command}] {args[0] if args else ''}", file=sys.stderr)

    def _json(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self, status: int, html: str):
        data = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode()
        return {
            urllib.parse.unquote_plus(k): urllib.parse.unquote_plus(v)
            for p in raw.split("&") if "=" in p
            for k, v in [p.split("=", 1)]
        }

    def do_GET(self):
        if self.path == "/.well-known/openid-configuration":
            self._json(200, {
                "issuer": ISSUER,
                "device_authorization_endpoint": f"{ISSUER}/device/authorize",
                "token_endpoint": f"{ISSUER}/oauth/token",
                "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
                "grant_types_supported": [
                    "urn:ietf:params:oauth:grant-type:device_code",
                    "refresh_token",
                ],
            })
        elif self.path == "/.well-known/jwks.json":
            self._json(200, _jwks)
        elif self.path.startswith("/verify"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = qs.get("code", [""])[0]
            self._html(200, f"""<html><body>
                <h2>Approve Device</h2>
                <form method="POST" action="/verify">
                <label>Code: <input name="user_code" value="{code}"></label><br>
                <label>Username: <input name="sub" value=""></label><br>
                <button type="submit">Approve</button>
                </form></body></html>""")
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/device/authorize":
            self._handle_device_authorize()
        elif self.path == "/verify":
            self._handle_verify()
        elif self.path == "/oauth/token":
            self._handle_token()
        else:
            self.send_error(404)

    def _handle_device_authorize(self):
        form = self._read_form()
        scope = form.get("scope", "")
        device_code = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        user_code = secrets.token_hex(3).upper()

        with _lock:
            _pending_codes[device_code] = {
                "user_code": user_code,
                "scope": scope,
                "approved": False,
                "sub": None,
                "created": time.time(),
            }

        self._json(200, {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": f"{ISSUER}/verify",
            "verification_uri_complete": f"{ISSUER}/verify?code={user_code}",
            "expires_in": DEVICE_CODE_LIFETIME,
            "interval": 5,
        })

    def _handle_verify(self):
        form = self._read_form()
        user_code = form.get("user_code", "").strip().upper()
        sub = form.get("sub", "").strip()

        if not user_code or not sub:
            return self._html(400, "<h2>Missing user_code or sub</h2>")

        with _lock:
            for dc, info in _pending_codes.items():
                if info["user_code"] == user_code and not info["approved"]:
                    info["approved"] = True
                    info["sub"] = sub
                    return self._html(200,
                        f"<h2>Approved</h2><p>Code {user_code} approved for {sub}. "
                        f"Return to the terminal.</p>")

        self._html(404, "<h2>Code not found or already used</h2>")

    def _handle_token(self):
        form = self._read_form()
        grant_type = form.get("grant_type", "")

        if grant_type == "urn:ietf:params:oauth:grant-type:device_code":
            return self._poll_device_code(form)
        elif grant_type == "refresh_token":
            return self._handle_refresh(form)
        else:
            self._json(400, {"error": "unsupported_grant_type"})

    def _poll_device_code(self, form: dict):
        device_code = form.get("device_code", "")
        with _lock:
            info = _pending_codes.get(device_code)
            if not info:
                return self._json(400, {"error": "invalid_grant"})
            if time.time() - info["created"] > DEVICE_CODE_LIFETIME:
                del _pending_codes[device_code]
                return self._json(400, {"error": "expired_token"})
            if not info["approved"]:
                return self._json(400, {"error": "authorization_pending"})

            scope = info["scope"]
            sub = info["sub"]
            del _pending_codes[device_code]

        audience = _audience or None
        access_token = _mint_token(sub, scope, audience)
        refresh_token = secrets.token_urlsafe(32)
        with _lock:
            _refresh_tokens[refresh_token] = {
                "sub": sub, "scope": scope, "created": time.time(),
            }

        self._json(200, {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": TOKEN_LIFETIME,
            "refresh_token": refresh_token,
            "scope": scope,
        })

    def _handle_refresh(self, form: dict):
        rt = form.get("refresh_token", "")
        with _lock:
            info = _refresh_tokens.pop(rt, None)
        if not info:
            return self._json(400, {"error": "invalid_grant"})

        audience = _audience or None
        access_token = _mint_token(info["sub"], info["scope"], audience)
        new_refresh = secrets.token_urlsafe(32)
        with _lock:
            _refresh_tokens[new_refresh] = {
                "sub": info["sub"], "scope": info["scope"], "created": time.time(),
            }

        self._json(200, {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": TOKEN_LIFETIME,
            "refresh_token": new_refresh,
            "scope": info["scope"],
        })


def main():
    pub_pem = _public_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    pub_body = "".join(l for l in pub_pem.strip().split("\n") if not l.startswith("-----"))

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadingHTTPServer((HOST, PORT), IdPHandler)
    print(f"Throwaway OIDC IdP running on {ISSUER}", file=sys.stderr)
    print(f"  JWKS:          {ISSUER}/.well-known/jwks.json", file=sys.stderr)
    print(f"  Device auth:   {ISSUER}/device/authorize", file=sys.stderr)
    print(f"  Token:         {ISSUER}/oauth/token", file=sys.stderr)
    print(f"  Verify page:   {ISSUER}/verify", file=sys.stderr)
    if _audience:
        print(f"  Audience:      {_audience}", file=sys.stderr)
    print(f"\n  RSA public key (for EXTERNAL_OAUTH_RSA_PUBLIC_KEY):", file=sys.stderr)
    print(f"  {pub_body[:60]}...", file=sys.stderr)
    if _key_file:
        print(f"  Key loaded from: {_key_file}", file=sys.stderr)
    else:
        print(f"  Key: ephemeral (set IDP_KEY_FILE to persist)", file=sys.stderr)
    print(f"\n  Press Ctrl+C to stop\n", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)


if __name__ == "__main__":
    main()
