"""Shared Snowflake authentication providers.

Client-agnostic auth: given a config dict (``account``, ``user``,
``auth: {type, ...}``) and a state store (anything with ``.load()``/
``.save(**kwargs)``, e.g. AuthStateManager below), returns an AuthResult.

Used by both snowflake-auth-sidecar.py (which writes tokens into kilo.json
and proxies MCP servers) and snowflake-cortex-proxy.py (which translates
Kilo's Responses API into Cortex Agent API calls). Kept import-only and
side-effect-free beyond the auth flows themselves, so either consumer can
import it without pulling in the other's HTTP server / proxying logic.

Supports: PAT, keypair JWT, Snowflake OAuth (authorization code + PKCE),
external OAuth (device code flow, RFC 8628).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    import jwt as pyjwt
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# -- ANSI colors --
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"

DEFAULT_REFRESH_MARGIN = 60  # seconds before expiry to trigger refresh


# =========================================================================
# Auth state persistence
# =========================================================================

class AuthStateManager:
    """Persists refresh tokens and token metadata across restarts."""

    def __init__(self, state_path: Path):
        self.state_path = state_path
        self._lock = threading.Lock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            if self.state_path.exists():
                with open(self.state_path) as f:
                    return json.load(f)
        return {}

    def save(self, **kwargs) -> None:
        with self._lock:
            state = {}
            if self.state_path.exists():
                with open(self.state_path) as f:
                    state = json.load(f)
            state.update(kwargs)
            tmp = str(self.state_path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
                f.write("\n")
            os.replace(tmp, str(self.state_path))

    def clear(self) -> None:
        with self._lock:
            if self.state_path.exists():
                self.state_path.unlink()


class InMemoryAuthStateManager:
    """Keeps refresh tokens in memory only — nothing written to disk."""

    def __init__(self):
        self._state: Dict[str, Any] = {}

    def load(self) -> Dict[str, Any]:
        return dict(self._state)

    def save(self, **kwargs) -> None:
        self._state.update(kwargs)

    def clear(self) -> None:
        self._state.clear()


# =========================================================================
# Auth result
# =========================================================================

class AuthResult:
    """Result of an authentication attempt."""
    def __init__(self, token: str, token_type: str = "PROGRAMMATIC_ACCESS_TOKEN",
                 expires_in: Optional[int] = None,
                 refresh_token: Optional[str] = None):
        self.token = token
        self.token_type = token_type
        self.expires_in = expires_in
        self.refresh_token = refresh_token
        self.obtained_at = time.time()

    @property
    def expires_at(self) -> Optional[float]:
        if self.expires_in:
            return self.obtained_at + self.expires_in
        return None

    @property
    def age(self) -> float:
        return time.time() - self.obtained_at

    def is_expired(self, margin: int = DEFAULT_REFRESH_MARGIN) -> bool:
        if self.expires_at:
            return time.time() >= (self.expires_at - margin)
        return False


# =========================================================================
# Auth providers
# =========================================================================

def auth_pat(config: Dict[str, Any]) -> AuthResult:
    """PAT is static — just return it."""
    pat = config.get("auth", {}).get("pat")
    if not pat:
        raise ValueError("PAT not found in config.auth.pat")
    return AuthResult(token=pat, token_type="PROGRAMMATIC_ACCESS_TOKEN")


def mint_keypair_jwt(account: str, user: str, key_path: Path,
                     passphrase: Optional[str] = None,
                     ttl_seconds: int = 3540) -> str:
    """Build a Snowflake keypair-auth JWT from a PEM private key.

    Shared by auth_keypair() below and snowflake-cortex-proxy.py's
    _auth_private_key(), which mints the same JWT but exchanges it for a
    session token afterwards instead of using it directly as a bearer token.
    """
    if not HAS_CRYPTO:
        raise RuntimeError("Keypair auth requires: pip install cryptography PyJWT")
    if not key_path.exists():
        raise FileNotFoundError(f"Private key not found: {key_path}")

    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(
            f.read(),
            password=passphrase.encode() if passphrase else None,
            backend=default_backend(),
        )

    public_key = private_key.public_key()
    pub_der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fp = f"SHA256:{base64.b64encode(hashlib.sha256(pub_der).digest()).decode()}"

    qualified_account = account.upper().replace(".", "-")
    qualified_user = user.upper()
    now = int(time.time())
    payload = {
        "iss": f"{qualified_account}.{qualified_user}.{fp}",
        "sub": f"{qualified_account}.{qualified_user}",
        "iat": now,
        "exp": now + ttl_seconds,  # max 60 min
    }
    return pyjwt.encode(payload, private_key, algorithm="RS256")


def auth_keypair(config: Dict[str, Any]) -> AuthResult:
    """Mint a JWT from the private key."""
    if not HAS_CRYPTO:
        raise RuntimeError("Keypair auth requires: pip install cryptography PyJWT")

    auth = config.get("auth", {})
    key_path = Path(auth.get("private_key_path", "")).expanduser()
    passphrase = auth.get("private_key_passphrase")
    token = mint_keypair_jwt(config["account"], config["user"], key_path, passphrase)
    return AuthResult(token=token, token_type="KEYPAIR_JWT", expires_in=3540)


def auth_snowflake_oauth(config: Dict[str, Any],
                         state: AuthStateManager) -> AuthResult:
    """Snowflake OAuth authorization code + PKCE flow."""
    auth = config.get("auth", {})
    client_id = auth.get("client_id")
    client_secret = auth.get("client_secret")
    if not client_id or not client_secret:
        raise ValueError("snowflake_oauth requires auth.client_id and auth.client_secret")

    base_url = f"https://{config['account']}.snowflakecomputing.com"
    redirect_port = int(auth.get("redirect_port", 8765))
    redirect_uri = f"http://localhost:{redirect_port}"
    scope = auth.get("scope", "")

    # Try refresh token first
    saved = state.load()
    refresh_token = saved.get("refresh_token")
    if refresh_token and saved.get("auth_type") == "snowflake_oauth":
        try:
            return _refresh_snowflake_oauth(
                base_url, client_id, client_secret, redirect_uri,
                refresh_token, state,
            )
        except Exception as e:
            print(f"{YELLOW}!{RESET} Refresh failed ({e}), starting full auth", file=sys.stderr)

    # PKCE
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    challenge_digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_digest).decode().rstrip("=")

    # Callback server
    auth_result: Dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = qs.get("code", [None])[0]
            error = qs.get("error", [None])[0]
            if code:
                auth_result["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body><h2>Authenticated.</h2>"
                                 b"<p>You can close this window.</p></body></html>")
            else:
                auth_result["error"] = error or "no code"
                self.send_error(400, auth_result["error"])

    server = HTTPServer(("localhost", redirect_port), Handler)
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    params = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": scope,
    })
    auth_url = f"{base_url}/oauth/authorize?{params}"

    print(f"\n{BOLD}Snowflake OAuth{RESET}", file=sys.stderr)
    print(f"  Opening browser: {auth_url[:80]}...", file=sys.stderr)
    webbrowser.open(auth_url)
    print(f"  Waiting for callback on port {redirect_port}...", file=sys.stderr)

    t.join(timeout=120)
    server.server_close()

    if "error" in auth_result:
        raise RuntimeError(f"OAuth error: {auth_result['error']}")
    if "code" not in auth_result:
        raise TimeoutError("OAuth timeout (120s)")

    # Exchange code for token
    token_data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": auth_result["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }).encode()
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        f"{base_url}/oauth/token-request",
        data=token_data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {creds}",
        },
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    access_token = resp.get("access_token")
    if not access_token:
        raise RuntimeError(f"No access_token: {resp}")

    result = AuthResult(
        token=access_token,
        token_type="OAUTH",
        expires_in=resp.get("expires_in"),
        refresh_token=resp.get("refresh_token"),
    )
    if result.refresh_token:
        state.save(
            auth_type="snowflake_oauth",
            refresh_token=result.refresh_token,
            expires_at=result.expires_at,
        )
    return result


def _refresh_snowflake_oauth(base_url: str, client_id: str, client_secret: str,
                              redirect_uri: str, refresh_token: str,
                              state: AuthStateManager) -> AuthResult:
    """Refresh a Snowflake OAuth token."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "redirect_uri": redirect_uri,
    }).encode()
    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        f"{base_url}/oauth/token-request",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {creds}",
        },
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    access_token = resp.get("access_token")
    if not access_token:
        raise RuntimeError("Refresh failed: no access_token")

    result = AuthResult(
        token=access_token,
        token_type="OAUTH",
        expires_in=resp.get("expires_in"),
        refresh_token=resp.get("refresh_token") or refresh_token,
    )
    state.save(
        auth_type="snowflake_oauth",
        refresh_token=result.refresh_token,
        expires_at=result.expires_at,
    )
    print(f"{GREEN}ok{RESET} Refreshed Snowflake OAuth token", file=sys.stderr)
    return result


def auth_device_code(config: Dict[str, Any],
                     state: AuthStateManager) -> AuthResult:
    """External OAuth device code flow (RFC 8628)."""
    auth = config.get("auth", {})
    client_id = auth.get("client_id")
    device_auth_endpoint = auth.get("device_authorization_endpoint")
    token_endpoint = auth.get("token_endpoint")

    # Support OpenID Connect discovery
    discovery_url = auth.get("idp_discovery_url")
    if discovery_url and (not device_auth_endpoint or not token_endpoint):
        disc = json.loads(urllib.request.urlopen(discovery_url, timeout=10).read())
        device_auth_endpoint = device_auth_endpoint or disc.get("device_authorization_endpoint")
        token_endpoint = token_endpoint or disc.get("token_endpoint")

    if not all([client_id, device_auth_endpoint, token_endpoint]):
        raise ValueError("device_code requires auth.client_id, auth.device_authorization_endpoint, auth.token_endpoint")

    scope = auth.get("scope", "")
    poll_interval = int(auth.get("poll_interval", 5))

    # Try refresh token first
    saved = state.load()
    refresh_token = saved.get("refresh_token")
    if refresh_token and saved.get("auth_type") == "device_code":
        try:
            return _refresh_device_code(token_endpoint, client_id, refresh_token, state)
        except Exception as e:
            print(f"{YELLOW}!{RESET} Refresh failed ({e}), starting device code flow", file=sys.stderr)

    # Request device code
    data = urllib.parse.urlencode({"client_id": client_id, "scope": scope}).encode()
    req = urllib.request.Request(
        device_auth_endpoint, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    device_resp = json.loads(urllib.request.urlopen(req, timeout=30).read())

    device_code = device_resp["device_code"]
    user_code = device_resp["user_code"]
    verification_uri = device_resp.get("verification_uri") or device_resp.get("verification_url", "")
    interval = device_resp.get("interval", poll_interval)
    expires_in = device_resp.get("expires_in", 600)

    print(f"\n{BOLD}Device Code Authentication{RESET}", file=sys.stderr)
    print(f"  Go to:     {BOLD}{verification_uri}{RESET}", file=sys.stderr)
    print(f"  Enter code: {BOLD}{user_code}{RESET}\n", file=sys.stderr)

    complete_uri = device_resp.get("verification_uri_complete")
    if complete_uri or verification_uri:
        try:
            webbrowser.open(complete_uri or verification_uri)
        except Exception:
            pass

    # Poll for token
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        poll_data = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device_code,
            "client_id": client_id,
        }).encode()
        poll_req = urllib.request.Request(
            token_endpoint, data=poll_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            poll_resp = urllib.request.urlopen(poll_req, timeout=30)
            token_resp = json.loads(poll_resp.read())
            access_token = token_resp.get("access_token")
            if access_token:
                result = AuthResult(
                    token=access_token,
                    token_type="OAUTH",
                    expires_in=token_resp.get("expires_in"),
                    refresh_token=token_resp.get("refresh_token"),
                )
                if result.refresh_token:
                    state.save(
                        auth_type="device_code",
                        refresh_token=result.refresh_token,
                        expires_at=result.expires_at,
                    )
                return result
        except urllib.error.HTTPError as e:
            body = json.loads(e.read())
            error = body.get("error", "")
            if error == "authorization_pending":
                continue
            elif error == "slow_down":
                interval = min(interval + 5, 30)
                continue
            elif error == "expired_token":
                raise TimeoutError("Device code expired")
            else:
                raise RuntimeError(f"Poll error: {error}")

    raise TimeoutError(f"Device code flow timed out ({expires_in}s)")


def _refresh_device_code(token_endpoint: str, client_id: str,
                         refresh_token: str,
                         state: AuthStateManager) -> AuthResult:
    """Refresh an external OAuth token."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode()
    req = urllib.request.Request(
        token_endpoint, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
    access_token = resp.get("access_token")
    if not access_token:
        raise RuntimeError("Refresh failed: no access_token")

    result = AuthResult(
        token=access_token,
        token_type="OAUTH",
        expires_in=resp.get("expires_in"),
        refresh_token=resp.get("refresh_token") or refresh_token,
    )
    state.save(
        auth_type="device_code",
        refresh_token=result.refresh_token,
        expires_at=result.expires_at,
    )
    print(f"{GREEN}ok{RESET} Refreshed external OAuth token", file=sys.stderr)
    return result


# =========================================================================
# Auth dispatcher
# =========================================================================

AUTH_PROVIDERS = {
    "pat": lambda cfg, _state: auth_pat(cfg),
    "privatekey": lambda cfg, _state: auth_keypair(cfg),
    "snowflake_oauth": auth_snowflake_oauth,
    "device_code": auth_device_code,
}


def authenticate(config: Dict[str, Any], state: AuthStateManager) -> AuthResult:
    """Authenticate using the method specified in config."""
    auth_type = config.get("auth", {}).get("type", "pat")
    provider = AUTH_PROVIDERS.get(auth_type)
    if not provider:
        raise ValueError(f"Unknown auth type: {auth_type}. Supported: {list(AUTH_PROVIDERS)}")
    return provider(config, state)


def verify_token(account: str, token: str) -> bool:
    """Quick check that the token works against Snowflake."""
    url = f"https://{account}.snowflakecomputing.com/api/v2/cortex/v1/chat/completions"
    data = json.dumps({
        "model": "llama3.1-8b",
        "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 1,
    }).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        urllib.request.urlopen(req, timeout=15)
        return True
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False
        return True  # other errors (rate limit, etc.) mean auth is fine
    except Exception:
        return False
