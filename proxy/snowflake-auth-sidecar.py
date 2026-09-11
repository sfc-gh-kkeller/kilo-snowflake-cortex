#!/usr/bin/env python3
"""Snowflake Auth Sidecar for Kilo Code.

Manages Snowflake authentication tokens and writes them into kilo.json
so Kilo Code can call Snowflake's native OpenAI-compatible API directly.

Supports: PAT, keypair JWT, Snowflake OAuth (authorization code + PKCE),
external OAuth (device code flow).

Also proxies Snowflake managed MCP server connections, injecting auth
tokens for each configured MCP server.

Usage:
    python3 snowflake-auth-sidecar.py              # auth sidecar (default)
    python3 snowflake-auth-sidecar.py --once        # authenticate once and exit
    python3 snowflake-auth-sidecar.py --status      # show current auth status
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

# -- ANSI colors --
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"

# -- Constants --
DEFAULT_REFRESH_MARGIN = 60        # seconds before expiry to trigger refresh
KEYPAIR_RENEWAL_INTERVAL = 3300    # 55 min (JWT max life is 60 min)
DEFAULT_HEALTH_PORT = 8079
AUTH_STATE_FILE = "snowflake-auth-state.json"


# =========================================================================
# Config management
# =========================================================================

def find_kilo_config() -> Optional[Path]:
    """Find kilo.json in standard locations."""
    for loc in [
        Path("~/.config/kilo/kilo.json").expanduser(),
        Path("~/.kilo/kilo.json").expanduser(),
        Path.cwd() / "kilo.json",
    ]:
        if loc.exists():
            return loc
    return None


def auth_state_path(config_path: Path) -> Path:
    """Path to the auth state file alongside kilo.json."""
    return config_path.parent / AUTH_STATE_FILE


class KiloConfigManager:
    """Thread-safe reader/writer for kilo.json."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self._lock = threading.Lock()

    def read(self) -> Dict[str, Any]:
        with self._lock:
            with open(self.config_path) as f:
                return json.load(f)

    def read_snowflake_config(self) -> Dict[str, Any]:
        config = self.read()
        sf = config.get("provider", {}).get("snowflake-cortex")
        if not sf:
            raise ValueError("No 'snowflake-cortex' provider in kilo.json")
        return sf

    def update_openai_provider(self, api_key: str, base_url: str,
                               models: Optional[Dict] = None,
                               provider_name: str = "openai") -> None:
        """Atomically update the OpenAI provider block in kilo.json."""
        with self._lock:
            with open(self.config_path) as f:
                config = json.load(f)

            prov = config.setdefault("provider", {}).setdefault(provider_name, {})
            prov["api"] = base_url
            opts = prov.setdefault("options", {})
            opts["apiKey"] = api_key
            opts["baseURL"] = base_url
            if models is not None:
                prov["models"] = models

            tmp = str(self.config_path) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
            os.replace(tmp, str(self.config_path))


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
# Auth providers
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


def auth_pat(config: Dict[str, Any]) -> AuthResult:
    """PAT is static — just return it."""
    pat = config.get("auth", {}).get("pat")
    if not pat:
        raise ValueError("PAT not found in config.auth.pat")
    return AuthResult(token=pat, token_type="PROGRAMMATIC_ACCESS_TOKEN")


def auth_keypair(config: Dict[str, Any]) -> AuthResult:
    """Mint a JWT from the private key."""
    if not HAS_CRYPTO:
        raise RuntimeError("Keypair auth requires: pip install cryptography PyJWT")

    auth = config.get("auth", {})
    key_path = Path(auth.get("private_key_path", "")).expanduser()
    if not key_path.exists():
        raise FileNotFoundError(f"Private key not found: {key_path}")

    passphrase = auth.get("private_key_passphrase")
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

    account = config["account"].upper().replace(".", "-")
    user = config["user"].upper()
    now = int(time.time())
    payload = {
        "iss": f"{account}.{user}.{fp}",
        "sub": f"{account}.{user}",
        "iat": now,
        "exp": now + 3540,  # 59 min
    }
    token = pyjwt.encode(payload, private_key, algorithm="RS256")
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


# =========================================================================
# Sidecar main loop
# =========================================================================

class AuthSidecar:
    """Manages token lifecycle and writes to kilo.json."""

    def __init__(self, config_mgr: KiloConfigManager, state: AuthStateManager,
                 provider_name: str = "openai"):
        self.config_mgr = config_mgr
        self.state = state
        self.provider_name = provider_name
        self.current: Optional[AuthResult] = None
        self.sf_config = config_mgr.read_snowflake_config()
        self.account = self.sf_config["account"]
        self.auth_type = self.sf_config.get("auth", {}).get("type", "pat")
        self.base_url = f"https://{self.account}.snowflakecomputing.com/api/v2/cortex/v1"
        self._stop = threading.Event()

    def do_auth(self) -> AuthResult:
        """Run authentication and update kilo.json."""
        result = authenticate(self.sf_config, self.state)
        self.current = result

        # Write token into kilo.json for Kilo to pick up
        self.config_mgr.update_openai_provider(
            api_key=result.token,
            base_url=self.base_url,
            provider_name=self.provider_name,
        )
        return result

    def refresh_interval(self) -> float:
        """How long to sleep before next refresh."""
        if self.auth_type == "pat":
            return 3600 * 24  # PAT is static, check once a day
        if self.auth_type == "privatekey":
            return KEYPAIR_RENEWAL_INTERVAL  # 55 min
        # OAuth: refresh before expiry
        if self.current and self.current.expires_at:
            remaining = self.current.expires_at - time.time() - DEFAULT_REFRESH_MARGIN
            return max(remaining, 30)
        return 2400  # default 40 min

    def run_forever(self) -> None:
        """Auth loop: authenticate, then refresh on schedule."""
        print(f"\n{BOLD}Snowflake Auth Sidecar{RESET}", file=sys.stderr)
        print(f"  Account:  {self.account}", file=sys.stderr)
        print(f"  Auth:     {self.auth_type}", file=sys.stderr)
        print(f"  Provider: {self.provider_name}", file=sys.stderr)
        print(f"  Target:   {self.base_url}", file=sys.stderr)

        # Initial auth
        result = self.do_auth()
        self._log_token(result, "Authenticated")

        while not self._stop.is_set():
            wait = self.refresh_interval()
            print(f"\n  Next refresh in {int(wait)}s", file=sys.stderr)
            if self._stop.wait(timeout=wait):
                break

            try:
                # Re-read config in case it changed on disk
                self.sf_config = self.config_mgr.read_snowflake_config()
                result = self.do_auth()
                self._log_token(result, "Refreshed")
            except Exception as e:
                print(f"{RED}!{RESET} Refresh failed: {e}", file=sys.stderr)
                print(f"  Will retry in 60s", file=sys.stderr)
                self._stop.wait(timeout=60)

    def stop(self) -> None:
        self._stop.set()

    def _log_token(self, result: AuthResult, action: str) -> None:
        exp_str = ""
        if result.expires_at:
            remaining = int(result.expires_at - time.time())
            exp_str = f", expires in {remaining}s"
        print(f"{GREEN}ok{RESET} {action} ({self.auth_type}{exp_str})", file=sys.stderr)
        print(f"  Token: {result.token[:20]}...{result.token[-10:]}", file=sys.stderr)
        print(f"  Written to: kilo.json -> provider.{self.provider_name}.options.apiKey",
              file=sys.stderr)


# =========================================================================
# Multi-session auth manager (for MCP proxy)
# =========================================================================

class AuthSession:
    """Manages token lifecycle for a single auth profile."""

    def __init__(self, profile_name: str, config: Dict[str, Any],
                 state: AuthStateManager):
        self.profile_name = profile_name
        self.config = config
        self.state = state
        self.current: Optional[AuthResult] = None
        self.account = config["account"]
        self.auth_type = config.get("auth", {}).get("type", "pat")
        self._lock = threading.Lock()

    def do_auth(self) -> AuthResult:
        result = authenticate(self.config, self.state)
        with self._lock:
            self.current = result
        return result

    def get_token(self) -> Optional[AuthResult]:
        with self._lock:
            return self.current

    def refresh_interval(self) -> float:
        if self.auth_type == "pat":
            return 3600 * 24
        if self.auth_type == "privatekey":
            return KEYPAIR_RENEWAL_INTERVAL
        cur = self.get_token()
        if cur and cur.expires_at:
            remaining = cur.expires_at - time.time() - DEFAULT_REFRESH_MARGIN
            return max(remaining, 30)
        return 2400


class AuthSessionManager:
    """Manages multiple concurrent auth sessions for different profiles."""

    def __init__(self):
        self.sessions: Dict[str, AuthSession] = {}
        self._threads: list = []
        self._stop = threading.Event()

    def add_session(self, profile_name: str, config: Dict[str, Any],
                    state: AuthStateManager) -> AuthSession:
        if profile_name in self.sessions:
            return self.sessions[profile_name]
        session = AuthSession(profile_name, config, state)
        self.sessions[profile_name] = session
        return session

    def start_all(self) -> None:
        """Authenticate all sessions and start refresh threads."""
        for name, session in self.sessions.items():
            try:
                result = session.do_auth()
                exp_str = ""
                if result.expires_at:
                    remaining = int(result.expires_at - time.time())
                    exp_str = f", expires in {remaining}s"
                print(f"{GREEN}ok{RESET} [{name}] Authenticated "
                      f"({session.auth_type}{exp_str})", file=sys.stderr)
            except Exception as e:
                print(f"{RED}!{RESET} [{name}] Auth failed: {e}", file=sys.stderr)

            t = threading.Thread(target=self._refresh_loop, args=(session,),
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def _refresh_loop(self, session: AuthSession) -> None:
        while not self._stop.is_set():
            wait = session.refresh_interval()
            if self._stop.wait(timeout=wait):
                break
            try:
                result = session.do_auth()
                exp_str = ""
                if result.expires_at:
                    remaining = int(result.expires_at - time.time())
                    exp_str = f", expires in {remaining}s"
                print(f"{GREEN}ok{RESET} [{session.profile_name}] Refreshed "
                      f"({session.auth_type}{exp_str})", file=sys.stderr)
            except Exception as e:
                print(f"{RED}!{RESET} [{session.profile_name}] Refresh failed: {e}",
                      file=sys.stderr)
                self._stop.wait(timeout=60)

    def stop(self) -> None:
        self._stop.set()


# =========================================================================
# MCP server registry (maps MCP names to auth sessions + upstream URLs)
# =========================================================================

class McpRegistry:
    """Maps MCP server names to upstream URLs and auth sessions."""

    def __init__(self):
        self.entries: Dict[str, Dict[str, Any]] = {}

    def add(self, name: str, upstream_url: str, session: AuthSession) -> None:
        self.entries[name] = {
            "url": upstream_url,
            "session": session,
        }

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        return self.entries.get(name)

    def names(self) -> list:
        return list(self.entries.keys())


# =========================================================================
# Lightweight proxy (fixes max_tokens -> max_completion_tokens)
# =========================================================================

class ProxyHandler(BaseHTTPRequestHandler):
    """Thin proxy: auth injection + parameter fixing for Snowflake Cortex.
    Also handles MCP proxy routes at /mcp/<name>."""
    sidecar: Optional[AuthSidecar] = None
    mcp_registry: Optional[McpRegistry] = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        sc = self.sidecar
        if self.path in ("/health", "/"):
            status = {
                "status": "ok",
                "auth_type": sc.auth_type if sc else "unknown",
                "account": sc.account if sc else "unknown",
                "token_age": int(sc.current.age) if sc and sc.current else None,
                "expires_in": int(sc.current.expires_at - time.time())
                    if sc and sc.current and sc.current.expires_at else None,
                "mode": "proxy",
            }
            # Add MCP status
            reg = self.mcp_registry
            if reg and reg.entries:
                mcp_status = {}
                for name, entry in reg.entries.items():
                    sess = entry["session"]
                    tok = sess.get_token()
                    mcp_status[name] = {
                        "profile": sess.profile_name,
                        "auth_type": sess.auth_type,
                        "account": sess.account,
                        "has_token": tok is not None,
                        "expires_in": int(tok.expires_at - time.time())
                            if tok and tok.expires_at else None,
                    }
                status["mcp_servers"] = mcp_status
            self._json_response(200, status)
        elif self.path == "/v1/models":
            self._proxy_get()
        else:
            self.send_error(404)

    def do_POST(self):
        # MCP proxy routes: /mcp/<name>
        if self.path.startswith("/mcp/"):
            self._proxy_mcp()
            return

        sc = self.sidecar
        if not sc or not sc.current:
            self._json_response(503, {"error": "not authenticated yet"})
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            body = json.loads(raw)
        except Exception:
            body = None

        # Fix max_tokens -> max_completion_tokens (Snowflake rejects max_tokens for Claude)
        if body and "max_tokens" in body and "max_completion_tokens" not in body:
            body["max_completion_tokens"] = body.pop("max_tokens")
            raw = json.dumps(body).encode()

        # Forward to Snowflake with auth
        # Kilo sends /v1/chat/completions; base_url already ends with /v1
        api_path = self.path
        if api_path.startswith("/v1/"):
            api_path = "/" + api_path[4:]  # strip /v1 prefix
        target_url = f"{sc.base_url}{api_path}"
        headers = {
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept", "text/event-stream"),
            "Authorization": f"Bearer {sc.current.token}",
        }
        if sc.current.token_type:
            headers["X-Snowflake-Authorization-Token-Type"] = sc.current.token_type

        req = urllib.request.Request(target_url, data=raw, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=600)
            content_type = resp.headers.get("Content-Type", "application/json")
            is_stream = "event-stream" in content_type or (body and body.get("stream"))

            self.send_response(resp.status)
            self.send_header("Content-Type", content_type)
            if is_stream:
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
            else:
                # Non-streaming: read full body, send with Content-Length
                full_body = resp.read()
                self.send_header("Content-Length", str(len(full_body)))
                self.end_headers()
                self.wfile.write(full_body)
                return
            self.end_headers()

            # Stream using chunked transfer encoding
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):x}\r\n".encode())
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            # Final chunk
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body_bytes)
        except Exception as e:
            self._json_response(502, {"error": str(e)})

    def _proxy_mcp(self):
        """Proxy a JSON-RPC request to a Snowflake managed MCP server."""
        reg = self.mcp_registry
        if not reg:
            self._json_response(503, {"error": "no MCP servers configured"})
            return

        # Parse /mcp/<name> or /mcp/<name>/extra/path
        parts = self.path.split("/", 3)  # ['', 'mcp', '<name>', ...]
        if len(parts) < 3:
            self._json_response(404, {"error": "missing MCP server name"})
            return
        mcp_name = parts[2]

        entry = reg.get(mcp_name)
        if not entry:
            available = ", ".join(reg.names()) or "(none)"
            self._json_response(404, {
                "error": f"MCP server '{mcp_name}' not found",
                "available": available,
            })
            return

        session: AuthSession = entry["session"]
        upstream_url = entry["url"]
        token_result = session.get_token()
        if not token_result:
            self._json_response(503, {
                "error": f"MCP server '{mcp_name}': not authenticated yet",
            })
            return

        # Read request body
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        # Forward to upstream with auth
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token_result.token}",
        }
        if token_result.token_type:
            headers["X-Snowflake-Authorization-Token-Type"] = token_result.token_type

        req = urllib.request.Request(
            upstream_url, data=raw, headers=headers, method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=600)
            content_type = resp.headers.get("Content-Type", "application/json")
            is_stream = "event-stream" in content_type

            self.send_response(resp.status)
            self.send_header("Content-Type", content_type)

            if is_stream:
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):x}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                full_body = resp.read()
                self.send_header("Content-Length", str(len(full_body)))
                self.end_headers()
                self.wfile.write(full_body)

        except urllib.error.HTTPError as e:
            body_bytes = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body_bytes)
        except Exception as e:
            self._json_response(502, {"error": f"MCP proxy error: {e}"})

    def _proxy_get(self):
        sc = self.sidecar
        api_path = self.path
        if api_path.startswith("/v1/"):
            api_path = "/" + api_path[4:]
        target_url = f"{sc.base_url}{api_path}"
        headers = {"Authorization": f"Bearer {sc.current.token}"}
        req = urllib.request.Request(target_url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.headers.get("Content-Type", "application/json"))
            self.end_headers()
            self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())

    def _json_response(self, status, body):
        data = json.dumps(body, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# =========================================================================
# Health endpoint
# =========================================================================

class HealthHandler(BaseHTTPRequestHandler):
    sidecar: Optional[AuthSidecar] = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        sc = self.sidecar
        status = {
            "status": "ok",
            "auth_type": sc.auth_type if sc else "unknown",
            "account": sc.account if sc else "unknown",
            "token_age": int(sc.current.age) if sc and sc.current else None,
            "expires_in": int(sc.current.expires_at - time.time())
                if sc and sc.current and sc.current.expires_at else None,
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status, indent=2).encode())


# =========================================================================
# CLI
# =========================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Snowflake Auth Sidecar for Kilo Code",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--once", action="store_true",
                        help="Authenticate once, update kilo.json, and exit")
    parser.add_argument("--status", action="store_true",
                        help="Show current auth status and exit")
    parser.add_argument("--verify", action="store_true",
                        help="Verify the current token works against Snowflake")
    parser.add_argument("--provider", default="openai",
                        help="kilo.json provider name to write tokens into (default: openai)")
    parser.add_argument("--health-port", type=int,
                        default=int(os.environ.get("AUTH_HEALTH_PORT", str(DEFAULT_HEALTH_PORT))),
                        help=f"Health endpoint port (default: {DEFAULT_HEALTH_PORT})")
    parser.add_argument("--no-health", action="store_true",
                        help="Disable health endpoint")
    parser.add_argument("--proxy", action="store_true",
                        help="Run as a lightweight proxy (fixes max_tokens, injects auth)")
    parser.add_argument("--proxy-port", type=int,
                        default=int(os.environ.get("PROXY_PORT", "8080")),
                        help="Proxy listen port (default: 8080)")
    parser.add_argument("--persist-refresh-token", action="store_true",
                        help="Store refresh tokens on disk so browser auth survives restarts")
    args = parser.parse_args()

    # Find config
    config_path = find_kilo_config()
    if not config_path:
        print(f"{RED}Error:{RESET} kilo.json not found", file=sys.stderr)
        sys.exit(1)

    print(f"{GREEN}ok{RESET} Config: {config_path}", file=sys.stderr)

    config_mgr = KiloConfigManager(config_path)
    if args.persist_refresh_token:
        state = AuthStateManager(auth_state_path(config_path))
        print(f"  Refresh tokens: persisted to {auth_state_path(config_path)}", file=sys.stderr)
    else:
        state = InMemoryAuthStateManager()
        print(f"  Refresh tokens: in-memory only", file=sys.stderr)

    if args.status:
        sf = config_mgr.read_snowflake_config()
        saved = state.load()
        print(f"Account:       {sf['account']}")
        print(f"User:          {sf['user']}")
        print(f"Auth type:     {sf.get('auth', {}).get('type', 'pat')}")
        print(f"Refresh token: {'yes' if saved.get('refresh_token') else 'no'}")
        if saved.get("expires_at"):
            remaining = saved["expires_at"] - time.time()
            print(f"Token expires:  {int(remaining)}s {'(expired)' if remaining < 0 else ''}")
        return

    if args.verify:
        sf = config_mgr.read_snowflake_config()
        full = config_mgr.read()
        token = full.get("provider", {}).get(args.provider, {}).get("options", {}).get("apiKey", "")
        if not token:
            print(f"{RED}No token{RESET} in provider.{args.provider}.options.apiKey")
            sys.exit(1)
        ok = verify_token(sf["account"], token)
        print(f"Token: {'VALID' if ok else 'INVALID'}")
        sys.exit(0 if ok else 1)

    # Create sidecar
    sidecar = AuthSidecar(config_mgr, state, provider_name=args.provider)

    if args.once:
        result = sidecar.do_auth()
        sidecar._log_token(result, "Authenticated")
        return

    # Start health endpoint
    if not args.no_health and not args.proxy:
        HealthHandler.sidecar = sidecar
        health_server = HTTPServer(("127.0.0.1", args.health_port), HealthHandler)
        ht = threading.Thread(target=health_server.serve_forever, daemon=True)
        ht.start()
        print(f"  Health:   http://127.0.0.1:{args.health_port}/", file=sys.stderr)

    if args.proxy:
        # Proxy mode: authenticate once, then serve as a lightweight proxy
        # Do NOT write to kilo.json — the proxy manages auth internally
        from socketserver import ThreadingMixIn
        class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
            daemon_threads = True

        result = authenticate(sidecar.sf_config, state)
        sidecar.current = result
        sidecar._log_token(result, "Authenticated")

        # Start background auth refresh for the main (Cortex inference) session
        def refresh_loop():
            while not sidecar._stop.is_set():
                wait = sidecar.refresh_interval()
                if sidecar._stop.wait(timeout=wait):
                    break
                try:
                    sidecar.sf_config = sidecar.config_mgr.read_snowflake_config()
                    r = authenticate(sidecar.sf_config, state)
                    sidecar.current = r
                    sidecar._log_token(r, "Refreshed")
                except Exception as e:
                    print(f"{RED}!{RESET} Refresh failed: {e}", file=sys.stderr)
                    sidecar._stop.wait(timeout=60)

        rt = threading.Thread(target=refresh_loop, daemon=True)
        rt.start()

        # Initialize MCP proxy sessions from ksc-profiles.json
        mcp_registry = McpRegistry()
        session_mgr = AuthSessionManager()
        profiles_file = config_path.parent / "ksc-profiles.json"
        if profiles_file.exists():
            try:
                with open(profiles_file) as f:
                    profiles_data = json.load(f)
                mcp_servers = profiles_data.get("mcp_servers", {})
                profiles = profiles_data.get("profiles", {})
                for mcp_name, mcp_cfg in mcp_servers.items():
                    if mcp_cfg.get("type") != "managed":
                        continue
                    profile_name = mcp_cfg.get("profile")
                    upstream_url = mcp_cfg.get("url")
                    if not profile_name or not upstream_url:
                        continue
                    profile_cfg = profiles.get(profile_name)
                    if not profile_cfg:
                        print(f"{YELLOW}!{RESET} MCP '{mcp_name}': "
                              f"profile '{profile_name}' not found, skipping",
                              file=sys.stderr)
                        continue
                    mcp_state = InMemoryAuthStateManager()
                    session = session_mgr.add_session(
                        profile_name, profile_cfg, mcp_state,
                    )
                    mcp_registry.add(mcp_name, upstream_url, session)
                    print(f"  MCP: {mcp_name} -> {profile_name} "
                          f"-> {upstream_url[:60]}...", file=sys.stderr)

                if session_mgr.sessions:
                    session_mgr.start_all()
            except Exception as e:
                print(f"{YELLOW}!{RESET} Failed to load MCP config: {e}",
                      file=sys.stderr)

        ProxyHandler.sidecar = sidecar
        ProxyHandler.mcp_registry = mcp_registry
        proxy_server = ThreadingHTTPServer(("127.0.0.1", args.proxy_port), ProxyHandler)
        print(f"\n  Proxy:    http://127.0.0.1:{args.proxy_port}/v1", file=sys.stderr)
        print(f"  Target:   {sidecar.base_url}", file=sys.stderr)
        if mcp_registry.entries:
            print(f"  MCP:      {len(mcp_registry.entries)} server(s) at "
                  f"/mcp/<name>", file=sys.stderr)
        print(f"\n  Point Kilo at: http://127.0.0.1:{args.proxy_port}/v1", file=sys.stderr)
        print(f"  {YELLOW}Press Ctrl+C to stop{RESET}\n", file=sys.stderr)

        try:
            proxy_server.serve_forever()
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Stopping{RESET}", file=sys.stderr)
            sidecar.stop()
            session_mgr.stop()
        return

    try:
        sidecar.run_forever()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}Stopping{RESET}", file=sys.stderr)
        sidecar.stop()


if __name__ == "__main__":
    main()
