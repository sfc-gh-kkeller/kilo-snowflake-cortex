#!/usr/bin/env python3
"""
Snowflake Cortex Agent Proxy for Kilo Code

Reads configuration from kilo.json and supports multiple auth methods:
- PAT (Programmatic Access Token)
- OAuth Token
- Private Key / JWT
- External Browser OAuth (with local callback)

Usage:
    python snowflake-cortex-proxy.py

Configuration in ~/.config/kilo/kilo.json:
{
  "provider": {
    "snowflake-cortex": {
      "account": "myorg-myaccount",
      "user": "myuser",
      "auth": {
        "type": "pat",  // or "oauth", "privatekey", "externalbrowser"
        "pat": "your_token",  // for type=pat
        "oauth_token": "token",  // for type=oauth
        "private_key_path": "~/.snowflake/key.p8",  // for type=privatekey
        "private_key_passphrase": "optional"  // for privatekey
      },
      "warehouse": "COMPUTE_WH",
      "role": "ACCOUNTADMIN",
      "database": "optional",
      "schema": "optional",
      "models": {
        "claude-sonnet-4-6": {
          "name": "Claude Sonnet 4.6",
          "tool_call": true,
          "limit": {"context": 200000, "output": 8192}
        }
      }
    }
  }
}
"""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Any, Iterator, Optional
import uuid
import time
import hashlib
import base64
import webbrowser
import threading

# Try to import cryptography for private key auth (optional)
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

# ANSI colors for output
RESET = "\033[0m"
BOLD = "\033[1m"
BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"

# Built-in tools we expose to Cortex.
# Keep this aligned to what Kilo can execute locally.
BUILTIN_TOOLS = ["read", "write", "edit", "glob", "grep", "bash"]

# Full request payloads are appended here as JSONL. Set PROXY_RAW_LOG="" to
# disable. Invaluable for seeing the exact tool-call shapes Kilo sends.
RAW_LOG = os.environ.get("PROXY_RAW_LOG", "/tmp/kilo-raw.jsonl")

# Hard ceiling on tool calls per conversation, so a misbehaving upstream can
# never keep a Kilo session spinning forever.
MAX_TOOL_STEPS = int(os.environ.get("PROXY_MAX_TOOL_STEPS", "30"))

# How many times the same tool call may run in one conversation before the proxy
# refuses to forward it again. 2 leaves room for a deliberate "run that again"
# while still killing a runaway re-plan.
REPEAT_TOOL_LIMIT = int(os.environ.get("PROXY_REPEAT_TOOL_LIMIT", "2"))

# Shown to the user when the only thing upstream wanted was to re-run a command
# it had already run. Without a body the client just asks again.
REPEAT_BLOCKED_MESSAGE = (
    "I already ran that exact command earlier in this conversation and have its "
    "output above, so I stopped instead of repeating it. Tell me what you'd like "
    "me to do with those results."
)

# How long to wait on an upstream response. Long-context requests are slow, so
# the old 180s was too tight once the 1M window is in play.
UPSTREAM_TIMEOUT = int(os.environ.get("PROXY_UPSTREAM_TIMEOUT", "600"))

# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------
# Single source of truth for everything model-specific: the context window we
# advertise to Kilo, whether to ask Cortex for the 1M window, which upstream
# serves the model, and whether tool calling is possible. kilo.json's `models`
# block is generated from this (see --print-kilo-models) so the two can't drift.

# POST /api/v2/cortex/agent:run -- the coding-agent flow, full tool calling.
BACKEND_AGENT = "agent"
# POST /api/v2/cortex/v1/chat/completions -- OpenAI-compatible. Serves models
# that agent:run rejects with "is not an allowed model for Agent requests".
BACKEND_CHAT = "chat"


def _model(label: str, context: int, max_output: int, *,
           backend: str = BACKEND_AGENT, tools: bool = True,
           long_context: bool = False, available: bool = True,
           measured: bool = True, note: str = "") -> Dict[str, Any]:
    return {
        "label": label,
        "context": context,
        "max_output": max_output,
        "backend": backend,
        "supports_tools": tools,
        # Sets experimental.Enable1MContextModel. Undocumented internal flag, so
        # only turn it on for models the docs give a 1M window.
        "long_context": long_context,
        "available": available,
        # False => context/max_output are inferred, not doc-backed or measured.
        "measured": measured,
        "note": note,
    }


MODEL_CATALOG: Dict[str, Dict[str, Any]] = {
    # --- Claude, 1M context (docs: aisql-regional-availability) -------------
    "claude-opus-5":     _model("Claude Opus 5",     1_000_000, 128_000, long_context=True),
    "claude-opus-4-8":   _model("Claude Opus 4.8",   1_000_000, 128_000, long_context=True),
    "claude-opus-4-7":   _model("Claude Opus 4.7",   1_000_000, 128_000, long_context=True),
    "claude-opus-4-6":   _model("Claude Opus 4.6",   1_000_000, 128_000, long_context=True),
    "claude-sonnet-5":   _model("Claude Sonnet 5",   1_000_000,  64_000, long_context=True),
    "claude-sonnet-4-6": _model("Claude Sonnet 4.6", 1_000_000,  64_000, long_context=True),

    # --- Claude, 200K context ----------------------------------------------
    "claude-opus-4-5":   _model("Claude Opus 4.5",     200_000, 64_000),
    "claude-sonnet-4-5": _model("Claude Sonnet 4.5",   200_000, 64_000),
    # Not on the original request list, but agent:run reports it as available
    # and it is the right choice for Kilo's `small_model` (title generation).
    "claude-haiku-4-5":  _model("Claude Haiku 4.5",    200_000, 64_000),

    # --- OpenAI. Absent from the AI_COMPLETE limits table, so these are
    #     measured against the live endpoint rather than guessed. -----------
    # Both are absent from the published limits table, so these are the
    # documented family values, deliberately set conservatively: under-declaring
    # a window is safe, over-declaring makes Kilo overpack and fail mid-session.
    # Probing agrees with the ordering -- 5.4 accepted a payload ~2x the size
    # 5.2 rejected -- but upstream surfaces oversize input as a generic
    # "internal error", so it can't pin an exact ceiling.
    "openai-gpt-5.4": _model("OpenAI GPT 5.4", 400_000, 128_000,
                             note="docs omit this model; 400K per the gpt-5.4 family "
                                  "(mini/nano); probe accepted more, value kept conservative"),
    "openai-gpt-5.2": _model("OpenAI GPT 5.2", 272_000,   8_192,
                             note="docs omit this model; 272K per the gpt-5/gpt-5.1 family; "
                                  "probe failed well below where 5.4 still succeeded"),

    # --- Unreachable on this account ----------------------------------------
    # Settled by agent:run's own diagnostics, which enumerate what this account
    # can use: "Available models: claude-haiku-4-5, claude-opus-4-5,
    # claude-opus-4-6, claude-opus-4-7, claude-opus-4-8, claude-opus-5,
    # claude-sonnet-4-5, claude-sonnet-4-6, claude-sonnet-5, openai-gpt-5.2,
    # openai-gpt-5.4 / Cross-region setting: no region restriction / Model
    # allowlist: No model RBAC/allowlist restriction".
    # So nothing is left to configure -- these are absent from the account's
    # model set, and the REST inference endpoint returns "unknown model" for
    # Gemini/DeepSeek too. Kept here to record the reason; omitted from the
    # generated Kilo config, because a picker entry that always errors is worse
    # than no entry.
    "gemini-3.1-pro": _model("Gemini Pro 3.1", 1_000_000, 64_000, available=False,
                             long_context=True,
                             note="not in this account's Agent model set (preview access "
                                  "enabled, no region/RBAC restriction); REST inference "
                                  "endpoint reports unknown model"),
    "deepseek-v4-flash": _model("DeepSeek V4 Flash", 128_000, 8_192, available=False,
                                tools=False, measured=False,
                                note="not in this account's Agent model set; REST inference "
                                     "endpoint reports unknown model"),
    "openai-gpt-5.5":       _model("OpenAI GPT 5.5",       272_000, 8_192, available=False,
                                   measured=False,
                                   note="REST inference: 'openai-gpt-5.5-global not allowed: "
                                        "this account is not allowed to access this model'"),
    "openai-gpt-5.6-sol":   _model("OpenAI GPT 5.6 Sol",   272_000, 8_192, available=False,
                                   measured=False,
                                   note="REST inference: 'openai-gpt-5.6-sol-global is unavailable'"),
    "openai-gpt-5.6-terra": _model("OpenAI GPT 5.6 Terra", 272_000, 8_192, available=False,
                                   measured=False,
                                   note="REST inference: 'openai-gpt-5.6-terra-global is unavailable'"),
    "openai-gpt-5.6-luna":  _model("OpenAI GPT 5.6 Luna",  272_000, 8_192, available=False,
                                   measured=False,
                                   note="REST inference: 'openai-gpt-5.6-luna-global is unavailable'"),
}

DEFAULT_MODEL = "claude-opus-5"

# Floor for how much of a single tool result we keep, in characters. Scaled up
# for big-context models -- a flat 60K is absurd against a 1M-token window.
TOOL_RESULT_CHAR_FLOOR = 60_000


def model_entry(model: str) -> Optional[Dict[str, Any]]:
    return MODEL_CATALOG.get(model)


def tool_result_char_limit(model: Optional[str]) -> int:
    """How much of one tool result to keep, scaled to the model's window."""
    entry = MODEL_CATALOG.get(model or "")
    if not entry:
        return TOOL_RESULT_CHAR_FLOOR
    return max(TOOL_RESULT_CHAR_FLOOR, entry["context"] // 8)



def normalize_tool(tool: Any) -> Optional[Dict[str, Any]]:
    """Flatten either OpenAI tool encoding into {name, description, parameters}.

    Chat Completions nests the definition under "function"; the Responses API
    (what Kilo actually sends) puts name/description/parameters at the top
    level. Only accepting the nested form silently dropped all 14 of Kilo's
    tools, which is what let Cortex fall back to its own internal tool names
    and spin forever.
    """
    if not isinstance(tool, dict):
        return None
    func = tool.get("function")
    src = func if isinstance(func, dict) else tool
    name = src.get("name")
    if not name:
        return None
    params = src.get("parameters")
    if not isinstance(params, dict):
        params = src.get("input_schema") if isinstance(src.get("input_schema"), dict) else {}
    return {
        "name": name,
        "description": src.get("description") or "",
        "parameters": params or {"type": "object", "properties": {}},
    }


def find_kilo_config() -> Optional[Path]:
    """Find kilo.json in standard locations."""
    locations = [
        Path("~/.config/kilo/kilo.json").expanduser(),
        Path("~/.kilo/kilo.json").expanduser(),
        Path.cwd() / "kilo.json",
    ]
    
    for loc in locations:
        if loc.exists():
            return loc
    return None


def load_kilo_config() -> Dict[str, Any]:
    """Load and parse kilo.json configuration."""
    config_path = find_kilo_config()
    if not config_path:
        raise FileNotFoundError(
            "kilo.json not found. Checked:\n"
            "  - ~/.config/kilo/kilo.json\n"
            "  - ~/.kilo/kilo.json\n"
            "  - ./kilo.json"
        )
    
    print(f"{GREEN}✓{RESET} Found config: {config_path}", file=sys.stderr)
    
    with open(config_path) as f:
        config = json.load(f)
    
    # Extract snowflake-cortex provider config
    providers = config.get("provider", {})
    sf_config = providers.get("snowflake-cortex")
    
    if not sf_config:
        raise ValueError(
            "No 'snowflake-cortex' provider found in kilo.json.\n"
            "Add configuration under provider.snowflake-cortex"
        )
    
    return sf_config


class SnowflakeCortexClient:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.account = config["account"]
        self.user = config["user"]
        self.warehouse = config.get("warehouse")
        self.role = config.get("role")
        self.database = config.get("database")
        self.schema = config.get("schema")
        self.base_url = f"https://{self.account}.snowflakecomputing.com"
        self.token = None
        self.authenticated_at = None
        self.auth_config = config.get("auth", {})
        self.auth_type = self.auth_config.get("type", "pat")
    
    def authenticate(self) -> bool:
        """Authenticate using the configured method."""
        print(f"{BLUE}→{RESET} Authenticating via {self.auth_type}...", file=sys.stderr)
        
        if self.auth_type == "pat":
            result = self._auth_pat()
        elif self.auth_type == "oauth":
            result = self._auth_oauth_token()
        elif self.auth_type == "privatekey":
            result = self._auth_private_key()
        elif self.auth_type == "externalbrowser":
            result = self._auth_external_browser()
        elif self.auth_type == "snowflake_oauth":
            result = self._auth_snowflake_oauth()
        elif self.auth_type == "device_code":
            result = self._auth_device_code()
        else:
            raise ValueError(f"Unsupported auth type: {self.auth_type}")
        if result:
            self.authenticated_at = time.time()
        return result

    # A Snowflake session token outlives its usefulness quietly: once it goes
    # stale, agent:run answers 200 with an *empty* SSE stream rather than 401,
    # which surfaces to the user as a model that returns nothing in ~500ms.
    # Refresh well inside the (undocumented, ~1h) idle window so a long-lived
    # proxy keeps working across an afternoon of intermittent use.
    TOKEN_MAX_AGE = int(os.environ.get("PROXY_TOKEN_MAX_AGE", "2400"))  # 40 min

    def token_age(self) -> Optional[float]:
        return None if not self.authenticated_at else time.time() - self.authenticated_at

    def ensure_fresh_token(self) -> None:
        """Re-authenticate if the session token is missing or getting old.

        Only PAT and private-key auth can be renewed unattended; an
        externalbrowser or pre-supplied OAuth token can't be re-minted without
        the user, so those are left alone.
        """
        if not self.token:
            self.authenticate()
            return
        if self.auth_type not in ("pat", "privatekey", "snowflake_oauth", "device_code"):
            return
        age = self.token_age()
        if age is not None and age > self.TOKEN_MAX_AGE:
            print(f"{BLUE}→{RESET} session token is {int(age)}s old; refreshing",
                  file=sys.stderr)
            self.authenticate()
    
    def _auth_pat(self) -> bool:
        """Authenticate using Programmatic Access Token."""
        pat = self.auth_config.get("pat")
        if not pat:
            raise ValueError("PAT token not found in config.auth.pat")
        
        req = urllib.request.Request(
            f"{self.base_url}/session/v1/login-request",
            data=json.dumps({
                "data": {
                    "AUTHENTICATOR": "PROGRAMMATIC_ACCESS_TOKEN",
                    "TOKEN": pat,
                    "ACCOUNT_NAME": self.account,
                    "LOGIN_NAME": self.user,
                    "CLIENT_APP_ID": "SnowflakeCortexProxy",
                    "CLIENT_APP_VERSION": "2.0",
                    "CLIENT_ENVIRONMENT": {"APPLICATION": "KiloProxy"},
                    "SESSION_PARAMETERS": {"CLIENT_REQUEST_MFA_TOKEN": False}
                }
            }).encode(),
            headers={"Content-Type": "application/json"}
        )
        
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
            self.token = data["data"]["token"]
            
            # Note: Role and warehouse from connection are already in the token context
            # No need to set them explicitly via SQL
            
            print(f"{GREEN}✓{RESET} Authenticated as {self.user}", file=sys.stderr)
            return True
        except Exception as e:
            print(f"{RED}✗{RESET} PAT authentication failed: {e}", file=sys.stderr)
            raise
    
    def _set_session_context(self):
        """Set role and warehouse using SQL API."""
        statements = []
        if self.role:
            statements.append(f"USE ROLE {self.role}")
        if self.warehouse:
            statements.append(f"USE WAREHOUSE {self.warehouse}")
        
        for stmt in statements:
            try:
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
                headers.update(self._auth_headers())
                req = urllib.request.Request(
                    f"{self.base_url}/api/v2/statements",
                    data=json.dumps({
                        "statement": stmt,
                        "timeout": 60,
                        "resultSetMetaData": {"format": "json"}
                    }).encode(),
                    headers=headers
                )
                resp = urllib.request.urlopen(req, timeout=30)
                print(f"{GREEN}✓{RESET} {stmt}", file=sys.stderr)
            except Exception as e:
                print(f"{YELLOW}⚠{RESET} {stmt} failed: {e}", file=sys.stderr)
    
    def _auth_oauth_token(self) -> bool:
        """Authenticate using existing OAuth token."""
        oauth_token = self.auth_config.get("oauth_token")
        if not oauth_token:
            raise ValueError("OAuth token not found in config.auth.oauth_token")
        
        # Use OAuth token directly as session token
        self.token = oauth_token
        print(f"{GREEN}✓{RESET} Using OAuth token", file=sys.stderr)
        return True
    
    def _auth_private_key(self) -> bool:
        """Authenticate using private key / JWT."""
        if not HAS_CRYPTO:
            raise RuntimeError(
                "Private key auth requires cryptography library.\n"
                "Install: pip install cryptography"
            )
        
        key_path = Path(self.auth_config.get("private_key_path", "")).expanduser()
        if not key_path.exists():
            raise FileNotFoundError(f"Private key not found: {key_path}")
        
        passphrase = self.auth_config.get("private_key_passphrase")
        
        # Load private key
        with open(key_path, "rb") as key_file:
            private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=passphrase.encode() if passphrase else None,
                backend=default_backend()
            )
        
        # Get public key fingerprint
        public_key = private_key.public_key()
        public_key_der = public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        sha256_hash = hashlib.sha256(public_key_der).digest()
        public_key_fp = f"SHA256:{base64.b64encode(sha256_hash).decode()}"
        
        # Create JWT — iss/sub must use uppercase ORG-ACCOUNT.USER format
        import jwt
        qualified_account = self.account.upper().replace(".", "-")
        qualified_user = self.user.upper()
        payload = {
            "iss": f"{qualified_account}.{qualified_user}.{public_key_fp}",
            "sub": f"{qualified_account}.{qualified_user}",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3540  # 59 min (max 60)
        }
        token = jwt.encode(payload, private_key, algorithm="RS256")
        
        # Authenticate with JWT
        req = urllib.request.Request(
            f"{self.base_url}/session/v1/login-request",
            data=json.dumps({
                "data": {
                    "AUTHENTICATOR": "SNOWFLAKE_JWT",
                    "TOKEN": token,
                    "ACCOUNT_NAME": self.account,
                    "LOGIN_NAME": self.user,
                }
            }).encode(),
            headers={"Content-Type": "application/json"}
        )
        
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
            self.token = data["data"]["token"]
            print(f"{GREEN}✓{RESET} Authenticated with private key", file=sys.stderr)
            return True
        except Exception as e:
            print(f"{RED}✗{RESET} Private key auth failed: {e}", file=sys.stderr)
            raise
    
    def _auth_external_browser(self) -> bool:
        """Authenticate using external browser OAuth flow."""
        print(f"{YELLOW}→{RESET} Starting OAuth flow...", file=sys.stderr)
        
        # Step 1: Get authentication URL
        proof_key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')
        
        req = urllib.request.Request(
            f"{self.base_url}/session/authenticator-request",
            data=json.dumps({
                "data": {
                    "ACCOUNT_NAME": self.account,
                    "LOGIN_NAME": self.user,
                    "CLIENT_APP_ID": "SnowflakeCortexProxy",
                    "CLIENT_APP_VERSION": "2.0",
                    "AUTHENTICATOR": "EXTERNALBROWSER",
                    "BROWSER_MODE_REDIRECT_PORT": "8765",
                    "PROOF_KEY": proof_key
                }
            }).encode(),
            headers={"Content-Type": "application/json"}
        )
        
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
            sso_url = data["data"]["ssoUrl"]
            token_url = data["data"]["tokenUrl"]
            
            print(f"{BLUE}→{RESET} Opening browser for authentication...", file=sys.stderr)
            print(f"{BLUE}→{RESET} URL: {sso_url}", file=sys.stderr)
            
            # Start local callback server
            callback_result = {}
            
            class CallbackHandler(BaseHTTPRequestHandler):
                def log_message(self, format, *args):
                    pass
                
                def do_GET(self):
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    token = query.get('token', [None])[0]
                    
                    if token:
                        callback_result['token'] = token
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html')
                        self.end_headers()
                        self.wfile.write(b"""
                            <html><body>
                            <h2>Authentication Successful!</h2>
                            <p>You can close this window and return to the terminal.</p>
                            </body></html>
                        """)
                    else:
                        self.send_error(400, "No token received")
            
            # Start callback server in background
            callback_server = HTTPServer(('localhost', 8765), CallbackHandler)
            server_thread = threading.Thread(target=callback_server.handle_request)
            server_thread.daemon = True
            server_thread.start()
            
            # Open browser
            webbrowser.open(sso_url)
            
            print(f"{YELLOW}→{RESET} Waiting for authentication...", file=sys.stderr)
            print(f"{YELLOW}→{RESET} If browser doesn't open, visit: {sso_url}", file=sys.stderr)
            
            # Wait for callback (timeout after 2 minutes)
            server_thread.join(timeout=120)
            
            if 'token' not in callback_result:
                raise TimeoutError("Authentication timeout - no response from browser")
            
            # Exchange token
            req = urllib.request.Request(
                f"{self.base_url}{token_url}",
                data=json.dumps({
                    "data": {
                        "TOKEN": callback_result['token'],
                        "PROOF_KEY": proof_key
                    }
                }).encode(),
                headers={"Content-Type": "application/json"}
            )
            
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
            self.token = data["data"]["token"]
            
            print(f"{GREEN}✓{RESET} Authenticated via browser", file=sys.stderr)
            return True
            
        except Exception as e:
            print(f"{RED}✗{RESET} Browser auth failed: {e}", file=sys.stderr)
            raise
    
    # ------------------------------------------------------------------
    # Snowflake OAuth (authorization code + PKCE)
    # ------------------------------------------------------------------

    def _auth_snowflake_oauth(self) -> bool:
        """Authenticate using Snowflake OAuth (authorization code + PKCE).

        Requires a Snowflake OAuth security integration:
            CREATE SECURITY INTEGRATION kilo_cortex_oauth
              TYPE = OAUTH  OAUTH_CLIENT = CUSTOM
              OAUTH_CLIENT_TYPE = 'CONFIDENTIAL'
              OAUTH_REDIRECT_URI = 'http://localhost:8765'
              ENABLED = TRUE  OAUTH_ENFORCE_PKCE = TRUE
              OAUTH_ISSUE_REFRESH_TOKENS = TRUE;
        """
        client_id = self.auth_config.get("client_id")
        client_secret = self.auth_config.get("client_secret")
        if not client_id or not client_secret:
            raise ValueError("snowflake_oauth requires auth.client_id and auth.client_secret")

        redirect_port = int(self.auth_config.get("redirect_port", 8765))
        redirect_uri = f"http://localhost:{redirect_port}"
        scope = self.auth_config.get("scope", "")

        # If we have a refresh token from a previous run, try refreshing first
        if hasattr(self, "_sf_oauth_refresh_token") and self._sf_oauth_refresh_token:
            try:
                return self._refresh_snowflake_oauth_token(client_id, client_secret, redirect_uri)
            except Exception:
                pass  # fall through to full auth

        # PKCE: generate code_verifier and code_challenge
        code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        challenge_digest = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = base64.urlsafe_b64encode(challenge_digest).decode().rstrip("=")

        # Start local callback server
        auth_result = {}

        class OAuthCallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                code = query.get("code", [None])[0]
                error = query.get("error", [None])[0]
                if code:
                    auth_result["code"] = code
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h2>Authenticated.</h2>"
                                    b"<p>You can close this window.</p></body></html>")
                else:
                    auth_result["error"] = error or "no code received"
                    self.send_error(400, auth_result["error"])

        callback_server = HTTPServer(("localhost", redirect_port), OAuthCallbackHandler)
        server_thread = threading.Thread(target=callback_server.handle_request, daemon=True)
        server_thread.start()

        # Build authorization URL
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "scope": scope,
        })
        auth_url = f"{self.base_url}/oauth/authorize?{params}"

        print(f"{BLUE}->>{RESET} Opening browser for Snowflake OAuth...", file=sys.stderr)
        print(f"{BLUE}->>{RESET} URL: {auth_url}", file=sys.stderr)
        webbrowser.open(auth_url)

        server_thread.join(timeout=120)
        callback_server.server_close()

        if "error" in auth_result:
            raise RuntimeError(f"OAuth authorization failed: {auth_result['error']}")
        if "code" not in auth_result:
            raise TimeoutError("OAuth authorization timeout (120s)")

        # Exchange code for token
        token_data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": auth_result["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }).encode()

        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req = urllib.request.Request(
            f"{self.base_url}/oauth/token-request",
            data=token_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {credentials}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        token_resp = json.loads(resp.read())

        self.token = token_resp.get("access_token")
        self._sf_oauth_refresh_token = token_resp.get("refresh_token")
        if not self.token:
            raise RuntimeError(f"No access_token in OAuth response: {token_resp}")

        print(f"{GREEN}v{RESET} Authenticated via Snowflake OAuth", file=sys.stderr)
        return True

    def _refresh_snowflake_oauth_token(self, client_id: str, client_secret: str,
                                        redirect_uri: str) -> bool:
        """Refresh a Snowflake OAuth access token using the stored refresh token."""
        token_data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self._sf_oauth_refresh_token,
            "redirect_uri": redirect_uri,
        }).encode()
        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req = urllib.request.Request(
            f"{self.base_url}/oauth/token-request",
            data=token_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {credentials}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        token_resp = json.loads(resp.read())
        self.token = token_resp.get("access_token")
        new_refresh = token_resp.get("refresh_token")
        if new_refresh:
            self._sf_oauth_refresh_token = new_refresh
        if not self.token:
            raise RuntimeError("Refresh failed: no access_token")
        print(f"{GREEN}v{RESET} Refreshed Snowflake OAuth token", file=sys.stderr)
        return True

    # ------------------------------------------------------------------
    # External OAuth device code flow
    # ------------------------------------------------------------------

    def _auth_device_code(self) -> bool:
        """Authenticate using an external IdP's device code flow.

        Requires an external OAuth integration in Snowflake:
            CREATE SECURITY INTEGRATION kilo_external_oauth
              TYPE = EXTERNAL_OAUTH  ENABLED = TRUE
              EXTERNAL_OAUTH_TYPE = CUSTOM
              EXTERNAL_OAUTH_ISSUER = 'https://idp.example.com'
              EXTERNAL_OAUTH_JWS_KEYS_URL = 'https://idp.example.com/.well-known/jwks.json'
              EXTERNAL_OAUTH_AUDIENCE_LIST = ('https://ACCOUNT.snowflakecomputing.com')
              EXTERNAL_OAUTH_TOKEN_USER_MAPPING_CLAIM = 'sub'
              EXTERNAL_OAUTH_SNOWFLAKE_USER_MAPPING_ATTRIBUTE = 'LOGIN_NAME'
              EXTERNAL_OAUTH_ANY_ROLE_MODE = 'ENABLE';
        """
        client_id = self.auth_config.get("client_id")
        device_auth_endpoint = self.auth_config.get("device_authorization_endpoint")
        token_endpoint = self.auth_config.get("token_endpoint")
        if not all([client_id, device_auth_endpoint, token_endpoint]):
            raise ValueError(
                "device_code requires auth.client_id, "
                "auth.device_authorization_endpoint, and auth.token_endpoint"
            )

        scope = self.auth_config.get("scope", "")
        poll_interval = int(self.auth_config.get("poll_interval", 5))

        # If we have a refresh token, try refreshing first
        if hasattr(self, "_dc_refresh_token") and self._dc_refresh_token:
            try:
                return self._refresh_device_code_token(token_endpoint, client_id)
            except Exception:
                pass

        # Step 1: Request device code
        req_data = urllib.parse.urlencode({
            "client_id": client_id,
            "scope": scope,
        }).encode()
        req = urllib.request.Request(
            device_auth_endpoint,
            data=req_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        device_resp = json.loads(resp.read())

        device_code = device_resp["device_code"]
        user_code = device_resp["user_code"]
        verification_uri = device_resp.get("verification_uri") or device_resp.get("verification_url", "")
        interval = device_resp.get("interval", poll_interval)
        expires_in = device_resp.get("expires_in", 600)

        print(f"\n{BOLD}{YELLOW}  Device code authentication{RESET}", file=sys.stderr)
        print(f"  Go to: {BOLD}{verification_uri}{RESET}", file=sys.stderr)
        print(f"  Enter code: {BOLD}{user_code}{RESET}\n", file=sys.stderr)

        # Try to open browser automatically
        if verification_uri:
            try:
                complete_uri = device_resp.get("verification_uri_complete")
                webbrowser.open(complete_uri or verification_uri)
            except Exception:
                pass

        # Step 2: Poll for token
        deadline = time.time() + expires_in
        while time.time() < deadline:
            time.sleep(interval)
            poll_data = urllib.parse.urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            }).encode()
            poll_req = urllib.request.Request(
                token_endpoint,
                data=poll_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                poll_resp = urllib.request.urlopen(poll_req, timeout=30)
                token_resp = json.loads(poll_resp.read())
                self.token = token_resp.get("access_token")
                self._dc_refresh_token = token_resp.get("refresh_token")
                if self.token:
                    print(f"{GREEN}v{RESET} Authenticated via device code", file=sys.stderr)
                    return True
            except urllib.error.HTTPError as e:
                body = json.loads(e.read())
                error = body.get("error", "")
                if error == "authorization_pending":
                    continue
                elif error == "slow_down":
                    interval = min(interval + 5, 30)
                    continue
                elif error == "expired_token":
                    raise TimeoutError("Device code expired before approval")
                else:
                    raise RuntimeError(f"Device code poll error: {error} - {body.get('error_description','')}")

        raise TimeoutError(f"Device code flow timed out after {expires_in}s")

    def _refresh_device_code_token(self, token_endpoint: str, client_id: str) -> bool:
        """Refresh an external OAuth token using the stored refresh token."""
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": self._dc_refresh_token,
            "client_id": client_id,
        }).encode()
        req = urllib.request.Request(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp = urllib.request.urlopen(req, timeout=30)
        token_resp = json.loads(resp.read())
        self.token = token_resp.get("access_token")
        new_refresh = token_resp.get("refresh_token")
        if new_refresh:
            self._dc_refresh_token = new_refresh
        if not self.token:
            raise RuntimeError("Refresh failed: no access_token")
        print(f"{GREEN}v{RESET} Refreshed external OAuth token", file=sys.stderr)
        return True

    def _convert_tools_to_cortex_format(self, tools: list) -> list:
        """Convert client tool definitions into Cortex `generic` tool specs.

        Declaring the client's own schemas (rather than Cortex's built-in
        coding tools) means every tool_use Cortex emits is both executable by
        the client and shaped for the client's parameters -- verified: Kilo's
        `read` schema yields input {"filePath": ...}.
        """
        cortex_tools = []
        seen = set()
        for tool in tools or []:
            norm = normalize_tool(tool)
            if not norm or norm["name"] in seen:
                continue
            seen.add(norm["name"])
            cortex_tools.append({
                "tool_spec": {
                    "type": "generic",
                    "name": norm["name"],
                    # Cortex rejects very long descriptions on some paths.
                    "description": norm["description"][:2000],
                    "input_schema": norm["parameters"],
                }
            })
        return cortex_tools
    
    def _get_default_tools(self) -> list:
        """Return built-in tool specs that Agent API recognizes."""
        return [{"tool_spec": {"type": name, "name": name}} for name in BUILTIN_TOOLS]
    
    def _build_coding_agent_config(self) -> dict:
        """Build CodingAgent experimental config matching nanocortex."""
        account_locator = self.account.upper().replace("-", "_")
        return {
            "UserID": self.user.split("@")[0],
            "SessionID": str(uuid.uuid4()),
            "Temperature": 1,
            "SystemPromptInternal": {
                "Prompt": "",
                "Attributes": {
                    "WorkingDirectory": os.getcwd(),
                    "IsGitRepo": os.path.isdir(os.path.join(os.getcwd(), ".git")),
                    "Platform": sys.platform,
                    "ArtifactDirectory": os.getcwd(),
                    "CanCreateFiles": True,
                    "OSVersion": sys.platform,
                    "AgentVersion": "1.0.0",
                    "AgentVersionLabel": "kilo-proxy",
                    "SnovaVersion": "1.0.0",
                    "AgentIdentity": "kilo-proxy",
                    "AgentDescription": "kilo cortex proxy",
                },
                "Version": "v2",
                "FullOverride": False,
            },
            "UseWebSearchPassthrough": False,
            "PrivateMode": False,
            "OriginApplication": "snova",
            "OriginApplicationVersion": "1.0.0",
            "SessionAccountLocators": [account_locator],
            "CurrentSqlAccountLocator": account_locator,
        }
    
    def stream_chat(self, messages: list, model: str = DEFAULT_MODEL,
                    tools: list = None, enable_tools: bool = True) -> Iterator[Dict]:
        """Stream a completion, routing to whichever upstream serves `model`.

        Both backends yield the same normalized event dicts
        ({"event": "response.text.delta"|"response.tool_use"|"response"|"error",
        "data": {...}}) so the SSE translation layer never needs to know which
        upstream answered.
        """
        entry = model_entry(model) or {}
        if entry.get("backend") == BACKEND_CHAT:
            yield from self.stream_via_chat_completions(
                messages, model, tools, enable_tools=enable_tools)
            return
        yield from self._stream_via_agent(
            messages, model, tools, enable_tools=enable_tools)

    def _stream_via_agent(self, messages: list, model: str,
                          tools: list = None, enable_tools: bool = True) -> Iterator[Dict]:
        """Stream from the Cortex Agent API (POST /api/v2/cortex/agent:run)."""
        self.ensure_fresh_token()
        
        # Convert OpenAI-style messages to Cortex Agent format with IDs and content arrays
        cortex_messages = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            elif isinstance(content, list):
                # Already in array format
                pass
            else:
                content = [{"type": "text", "text": str(content)}]
            
            cortex_messages.append({
                "role": msg.get("role", "user"),
                "id": f"msg_{uuid.uuid4()}",
                "content": content
            })
        
        # Build request body matching the Agent API contract. The Agent API is
        # fairly strict about required/expected experimental fields; omitting
        # them can produce opaque 400s.
        body = {
            "messages": cortex_messages,
            "model": model,
            "stream": True,
            "origin_application": "coding_agent",
            # Prefer the client's own tool definitions; fall back to the
            # built-in coding tools only when the client advertised none.
            "tools": ((self._convert_tools_to_cortex_format(tools) or self._get_default_tools())
                      if enable_tools else []),
            "tool_choice": {"type": "auto"},
            "experimental": {
                "UseLegacyAnswersToolNames": False,
                "ResponseSchemaVersion": "v2",
                "EnableSingleAgentOrchestration": True,
                "EnableFunctionCallAPIForPlanning": True,
                "ReasoningAgentFlowType": "simple",
                "StopCondition": {"NumSteps": 15},
                "Canary": False,
                "ThinkingEffort": "medium",
                "UseAdaptiveThinking": True,
                # Ungated per the docs, but this internal flag still has to be
                # asked for -- leaving it False capped every model at the
                # standard window regardless of what the model supports.
                "Enable1MContextModel": bool((model_entry(model) or {}).get("long_context")),
                "EnableStepTrace": True,
                "CodingAgent": self._build_coding_agent_config(),
            },
        }

        entry = model_entry(model) or {}
        print(f"{BLUE}→{RESET} agent:run model={model} "
              f"context={entry.get('context', 'unknown')} "
              f"1M={bool(entry.get('long_context'))}", file=sys.stderr)

        if enable_tools:
            names = [(t.get("tool_spec") or {}).get("name") for t in body["tools"]]
            print(f"{BLUE}→{RESET} Cortex tools: {names}", file=sys.stderr)

        
        agent_headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        agent_headers.update(self._auth_headers())
        req = urllib.request.Request(
            f"{self.base_url}/api/v2/cortex/agent:run",
            data=json.dumps(body).encode(),
            headers=agent_headers
        )
        
        def issue(request):
            """Run one agent:run attempt, yielding normalized events.

            Also reports how many events it produced, because a stale session
            token manifests as 200-with-no-events rather than a 401, so the
            caller retries when this comes back zero.
            """
            count = 0
            resp = urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT)
            event_type = None

            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue

                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                    continue

                if line.startswith("data:"):
                    data_str = line.split(":", 1)[1].strip()
                    if data_str and data_str != "[DONE]":
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        count += 1
                        yield {"event": event_type, "data": data}
            issue.events = count

        def rebuild():
            """A fresh request carrying the newly minted token."""
            h = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            }
            h.update(self._auth_headers())
            return urllib.request.Request(
                f"{self.base_url}/api/v2/cortex/agent:run",
                data=json.dumps(body).encode(),
                headers=h
            )

        renewable = self.auth_type in ("pat", "privatekey", "snowflake_oauth", "device_code")

        try:
            issue.events = 0
            yield from issue(req)
            if issue.events == 0 and renewable:
                # Nothing came back. The overwhelmingly common cause is an
                # expired session token, which Cortex signals with an empty
                # 200 stream instead of an auth error. Re-authenticate and try
                # once more — safe precisely because no events were emitted, so
                # the client has seen nothing to duplicate.
                print(f"{YELLOW}!{RESET} empty stream; re-authenticating and "
                      f"retrying once", file=sys.stderr)
                self.authenticate()
                yield from issue(rebuild())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else "No error body"
            if not error_body.strip():
                # Some 400s return an empty body; at least surface headers.
                try:
                    hdrs = dict(e.headers.items()) if getattr(e, 'headers', None) else {}
                except Exception:
                    hdrs = {}
                error_body = f"<empty body> headers={json.dumps(hdrs)}"
            print(f"{RED}✗{RESET} Snowflake API Error {e.code}: {error_body}", file=sys.stderr)
            if e.code in (401, 403) and renewable:
                print(f"{YELLOW}!{RESET} auth rejected; re-authenticating and "
                      f"retrying once", file=sys.stderr)
                self.authenticate()
                yield from issue(rebuild())
                return
            raise
        except Exception as e:
            print(f"{RED}✗{RESET} Request failed: {e}", file=sys.stderr)
            raise


    # ------------------------------------------------------------------
    # Backend 2: OpenAI-compatible chat completions
    # ------------------------------------------------------------------
    # POST /api/v2/cortex/v1/chat/completions serves models that agent:run
    # rejects with "is not an allowed model for Agent requests". It takes the
    # raw PAT as a bearer token (NOT the session token agent:run uses) and runs
    # as the user's default role, which needs SNOWFLAKE.CORTEX_USER.

    def _bearer_credentials(self):
        """Return (token, token_type_header_value) for the REST inference API."""
        if self.auth_type == "pat":
            pat = self.auth_config.get("pat")
            if pat:
                return pat, "PROGRAMMATIC_ACCESS_TOKEN"
        if self.auth_type == "oauth":
            tok = self.auth_config.get("token") or self.auth_config.get("oauth_token")
            if tok:
                return tok, "OAUTH"
        if self.auth_type == "privatekey" and self.token:
            return self.token, "KEYPAIR_JWT"
        if self.auth_type in ("snowflake_oauth", "device_code") and self.token:
            return self.token, "OAUTH"
        return None, None

    def _auth_headers(self):
        """Return auth headers dict for any Snowflake REST API call.

        PAT session tokens use ``Snowflake Token="..."`` format.
        Bearer-based auth (keypair, OAuth, device code) uses ``Bearer ...``
        with the ``X-Snowflake-Authorization-Token-Type`` header.
        """
        if self.auth_type == "pat":
            return {"Authorization": f'Snowflake Token="{self.token}"'}
        token, token_type = self._bearer_credentials()
        if token and token_type:
            return {
                "Authorization": f"Bearer {token}",
                "X-Snowflake-Authorization-Token-Type": token_type,
            }
        # Fallback: use session token if available
        if self.token:
            return {"Authorization": f'Snowflake Token="{self.token}"'}
        return {}

    @staticmethod
    def _cortex_messages_to_openai(messages: list) -> list:
        """Cortex message format -> OpenAI chat messages.

        Our internal format merges a tool call and its result onto one assistant
        message (that pairing is what Cortex needs). OpenAI wants them split:
        an assistant message carrying `tool_calls`, then one `role:"tool"`
        message per result, immediately after.
        """
        out = []
        for msg in messages or []:
            role = msg.get("role", "user")
            content = msg.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                content = []

            texts, tool_calls, tool_results, images = [], [], [], []
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    if part.get("text"):
                        texts.append(part["text"])
                elif ptype == "image_url":
                    url = part.get("image_url")
                    if isinstance(url, dict):
                        url = url.get("url")
                    if url:
                        images.append({"type": "image_url", "image_url": {"url": url}})
                elif ptype == "tool_use":
                    tu = part.get("tool_use") or {}
                    try:
                        args = json.dumps(tu.get("input") or {})
                    except Exception:
                        args = "{}"
                    tool_calls.append({
                        "id": tu.get("tool_use_id") or f"call_{uuid.uuid4().hex}",
                        "type": "function",
                        "function": {"name": tu.get("name") or "tool", "arguments": args},
                    })
                elif ptype == "tool_result":
                    tr = part.get("tool_result") or {}
                    chunks = []
                    for c in tr.get("content") or []:
                        if isinstance(c, dict) and c.get("text"):
                            chunks.append(c["text"])
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id") or "",
                        "content": "\n".join(chunks) or "(no output)",
                    })

            text = "\n".join(texts)

            if role == "system":
                if text:
                    out.append({"role": "system", "content": text})
                continue

            if role == "assistant":
                entry = {"role": "assistant", "content": text or None}
                if tool_calls:
                    entry["tool_calls"] = tool_calls
                if text or tool_calls:
                    out.append(entry)
                # Results must directly follow the call that produced them.
                out.extend(tool_results)
                continue

            # user
            if images:
                parts = ([{"type": "text", "text": text}] if text else []) + images
                out.append({"role": "user", "content": parts})
            elif text:
                out.append({"role": "user", "content": text})
            out.extend(tool_results)

        return out

    def stream_via_chat_completions(self, messages: list, model: str,
                                    tools: list = None,
                                    enable_tools: bool = True) -> Iterator[Dict]:
        """Stream from /api/v2/cortex/v1/chat/completions.

        Yields the same normalized events as the agent backend, so the SSE
        translation layer is identical for both.
        """
        token, token_type = self._bearer_credentials()
        if not token:
            yield {"event": "error", "data": {"message":
                   f"{model} needs the Cortex REST API, which requires a PAT or OAuth "
                   f"token; current auth type is {self.auth_type!r}."}}
            return

        entry = model_entry(model) or {}
        body = {
            "model": model,
            "messages": self._cortex_messages_to_openai(messages),
            "stream": True,
            # Default is only 4096, which truncates real coding answers.
            "max_completion_tokens": min(int(entry.get("max_output") or 8192), 16384),
        }

        if enable_tools and tools and entry.get("supports_tools", True):
            fns = []
            for t in tools:
                norm = normalize_tool(t)
                if norm:
                    fns.append({"type": "function", "function": {
                        "name": norm["name"],
                        "description": norm["description"][:2000],
                        "parameters": norm["parameters"],
                    }})
            if fns:
                body["tools"] = fns
                body["tool_choice"] = "auto"

        print(f"{BLUE}→{RESET} chat/completions model={model} "
              f"context={entry.get('context', 'unknown')} "
              f"msgs={len(body['messages'])} tools={len(body.get('tools') or [])}",
              file=sys.stderr)

        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        if token_type:
            headers["X-Snowflake-Authorization-Token-Type"] = token_type

        req = urllib.request.Request(
            f"{self.base_url}/api/v2/cortex/v1/chat/completions",
            data=json.dumps(body).encode(), headers=headers)

        # OpenAI streams tool calls as fragments keyed by index.
        pending: Dict[int, Dict[str, str]] = {}
        emitted_any = False

        def finished_tool_events():
            for idx in sorted(pending):
                call = pending[idx]
                try:
                    args = json.loads(call["arguments"]) if call["arguments"] else {}
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {"value": args}
                yield {"event": "response.tool_use", "data": {
                    "tool_use_id": call["id"] or f"call_{uuid.uuid4().hex}",
                    "name": call["name"],
                    "input": args,
                    "client_side_execute": True,
                }}

        try:
            resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if isinstance(chunk.get("error"), dict):
                    yield {"event": "error", "data": {
                        "message": chunk["error"].get("message") or json.dumps(chunk["error"])}}
                    return

                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}

                    text = delta.get("content")
                    if text:
                        emitted_any = True
                        yield {"event": "response.text.delta", "data": {"text": text}}

                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = pending.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

                    if choice.get("finish_reason"):
                        for event in finished_tool_events():
                            emitted_any = True
                            yield event
                        pending.clear()

            # Flush anything the upstream left open.
            for event in finished_tool_events():
                emitted_any = True
                yield event
            pending.clear()

            if not emitted_any:
                yield {"event": "error", "data": {"message": "Upstream returned no content"}}
                return

            yield {"event": "response", "data": {}}

        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            message = f"Cortex REST API {e.code}"
            try:
                parsed = json.loads(detail)
                message = (parsed.get("message")
                           or (parsed.get("error") or {}).get("message")
                           or message)
            except Exception:
                if detail:
                    message = f"{message}: {detail[:300]}"
            print(f"{RED}✗{RESET} chat/completions error: {message}", file=sys.stderr)
            yield {"event": "error", "data": {"message": message}}
        except Exception as e:
            print(f"{RED}✗{RESET} chat/completions request failed: {e}", file=sys.stderr)
            yield {"event": "error", "data": {"message": str(e)}}


class ProxyHandler(BaseHTTPRequestHandler):
    cortex_client: SnowflakeCortexClient = None
    # Model for the request currently being served; drives context-scaled
    # tool-result truncation during message conversion.
    _active_model: Optional[str] = None
    # Track session state so we can distinguish "first tool call" vs
    # "post-tool output followup" even when Kilo's payload shape varies.
    session_last_was_tool_call: Dict[str, bool] = {}

    def _log_request_line(self):
        try:
            print(f"{BLUE}→{RESET} HTTP {self.command} {self.path}", file=sys.stderr)
        except Exception:
            pass

    def send_error(self, code, message=None, explain=None):
        # Make it obvious in proxy logs when the client saw an error.
        try:
            print(f"{RED}✗{RESET} HTTP {code} for {self.command} {self.path}: {message}", file=sys.stderr)
        except Exception:
            pass
        return super().send_error(code, message, explain)
    
    def log_message(self, format, *args):
        """Custom logging."""
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")
    
    def do_POST(self):
        self._log_request_line()
        # Kilo uses OpenAI-compatible endpoints. The TUI and CLI can hit
        # different ones depending on workflow.
        if self.path in ("/v1/chat/completions", "/v1/responses"):
            self.handle_chat_completion()
            return

        self.send_error(404, "Not Found")
    
    def do_GET(self):
        self._log_request_line()
        if self.path in ("/health", "/"):
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            age = self.cortex_client.token_age()
            self.wfile.write(json.dumps({
                "status": "ok",
                "service": "Snowflake Cortex Proxy",
                "account": self.cortex_client.account,
                "user": self.cortex_client.user,
                "auth_type": self.cortex_client.auth_type,
                "authenticated": self.cortex_client.token is not None,
                # `authenticated` only says a token was once minted. Age is the
                # useful signal, since a stale session token still looks
                # authenticated while every completion comes back empty.
                "token_age_seconds": None if age is None else int(age),
                "token_max_age_seconds": self.cortex_client.TOKEN_MAX_AGE,
            }).encode())
            return

        if self.path == "/v1/models":
            # Served from MODEL_CATALOG so the proxy is authoritative about what
            # it can actually route, including each model's real window.
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            data = [{
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "snowflake-cortex",
                "context_window": entry["context"],
                "max_output_tokens": entry["max_output"],
                "backend": entry["backend"],
                "supports_tools": entry["supports_tools"],
                "long_context": entry["long_context"],
            } for model_id, entry in MODEL_CATALOG.items() if entry["available"]]
            self.wfile.write(json.dumps({"object": "list", "data": data}).encode())
            return
        else:
            self.send_error(404, "Not Found")
    
    BUILTIN_TOOL_SET = set(BUILTIN_TOOLS)

    def _cortex_tool_type(self, name: str) -> str:
        """Cortex tool `type` for a tool name.

        The built-in coding tools are declared as {"type": name, "name": name}
        by _get_default_tools(), so the type mirrors the name. Anything else is
        a generic client-side function.
        """
        return name if name in self.BUILTIN_TOOL_SET else "generic"

    # Args the model writes for human consumption. They vary run to run and
    # must not make two otherwise-identical calls look different, or the repeat
    # detector never fires.
    COSMETIC_ARG_KEYS = frozenset({"description", "explanation", "reason",
                                   "thought", "why", "purpose"})

    @classmethod
    def _tool_signature(cls, name: str, arguments: Any) -> str:
        """Stable semantic identity for a tool invocation."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                pass
        if isinstance(arguments, dict):
            arguments = {k: v for k, v in arguments.items()
                         if k not in cls.COSMETIC_ARG_KEYS}
        try:
            args = json.dumps(arguments, sort_keys=True)
        except Exception:
            args = str(arguments)
        return f"{name}::{args}"

    @staticmethod
    def _text_of(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value)
            except Exception:
                return str(value)
        return str(value)

    def _build_tool_use_part(self, call_id: str, name: str, arguments: Any) -> Dict[str, Any]:
        """Assistant content part telling Cortex "I called this tool"."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {"value": arguments} if arguments else {}
        if not isinstance(arguments, dict):
            arguments = {"value": arguments}
        return {
            "type": "tool_use",
            "tool_use": {
                "tool_use_id": call_id,
                "type": self._cortex_tool_type(name),
                "name": name,
                "input": arguments,
                "client_side_execute": True,
            },
        }

    def _build_tool_result_part(self, call_id: str, name: str, output: Any,
                                is_error: bool = False) -> Dict[str, Any]:
        """Assistant content part carrying the client-executed tool's output."""
        text = self._text_of(output)
        # Scale with the model's window instead of a flat cap: a 1M-context
        # model can comfortably absorb a large file or directory listing.
        limit = tool_result_char_limit(getattr(self, "_active_model", None))
        if len(text) > limit:
            text = text[:limit] + f"\n... [truncated {len(text) - limit} chars]"
        return {
            "type": "tool_result",
            "tool_result": {
                "tool_use_id": call_id,
                "type": self._cortex_tool_type(name),
                "name": name,
                "status": "error" if is_error else "success",
                "content": [{"type": "text", "text": text or "(no output)"}],
            },
        }

    @staticmethod
    def _tool_output_is_error(d: Dict[str, Any]) -> bool:
        if d.get("is_error") is True:
            return True
        status = d.get("status")
        return isinstance(status, str) and status.lower() in ("error", "failed", "failure")

    def _tool_output_text(self, d: Dict[str, Any]) -> str:
        out = d.get("output")
        if out is None:
            out = d.get("result")
        if out is None:
            out = d.get("content")
        if isinstance(out, list):
            chunks = []
            for p in out:
                if isinstance(p, dict):
                    chunks.append(self._text_of(p.get("text", p.get("output", p))))
                else:
                    chunks.append(self._text_of(p))
            return "\n".join(c for c in chunks if c)
        return self._text_of(out)

    TOOL_CALL_TYPES = ("function_call", "tool_call", "tool_use")
    TOOL_OUTPUT_TYPES = ("function_call_output", "tool_result", "tool_output")

    def _convert_kilo_input_to_messages(self, input_items):
        """Convert Kilo's Responses-API `input` array into Cortex messages.

        Kilo replays the assistant's `function_call` items together with the
        matching `function_call_output` items on every follow-up turn. Cortex
        only treats a tool as *already executed* when it sees a `tool_use`
        content part paired with a `tool_result` part on the assistant message.
        Flattening tool output into plain assistant text (the previous
        behaviour) makes the agent re-plan from scratch and re-issue the same
        command every turn -- that is what produced the endless `find ...` loop.
        """
        messages: list = []
        saw_tool_output = False
        # How many times each tool call already ran in this history, so the
        # caller can allow a deliberate re-run but kill a runaway loop.
        executed: Dict[str, int] = {}
        calls_by_id: Dict[str, Dict[str, Any]] = {}

        def append_or_merge(role: str, parts: list):
            # Cortex requires strict user/assistant alternation, so fold
            # consecutive same-role messages together.
            if not parts:
                return
            if messages and messages[-1].get("role") == role:
                prev = messages[-1].get("content")
                if isinstance(prev, list):
                    prev.extend(parts)
                    return
            messages.append({"role": role, "content": list(parts)})

        def emit_parts(role: str, parts: list):
            """Route tool_use/tool_result parts onto an assistant message."""
            buf_role = role
            buf: list = []
            for p in parts:
                target = "assistant" if p.get("type") in ("tool_use", "tool_result") else role
                if target != buf_role:
                    append_or_merge(buf_role, buf)
                    buf = []
                    buf_role = target
                buf.append(p)
            append_or_merge(buf_role, buf)

        def register_call(call_id: Optional[str], name: str, arguments: Any) -> str:
            if not call_id:
                call_id = f"toolu_{uuid.uuid4().hex}"
            calls_by_id[call_id] = {"name": name, "arguments": arguments}
            return call_id

        def tool_use_from(d: Dict[str, Any]) -> Dict[str, Any]:
            call_id = d.get("call_id") or d.get("tool_use_id") or d.get("id")
            name = d.get("name") or d.get("tool_name") or "bash"
            args = d.get("arguments")
            if args is None:
                args = d.get("input")
            if args is None:
                args = d.get("args") or {}
            call_id = register_call(call_id, name, args)
            return self._build_tool_use_part(call_id, name, args)

        def tool_result_from(d: Dict[str, Any]) -> list:
            nonlocal saw_tool_output
            saw_tool_output = True
            call_id = d.get("call_id") or d.get("tool_use_id") or d.get("id")
            prior = calls_by_id.get(call_id) if call_id else None
            name = d.get("name") or d.get("tool_name") or (prior or {}).get("name") or "bash"
            parts = []
            if not call_id or call_id not in calls_by_id:
                # A tool_result with no preceding tool_use is rejected, so
                # synthesise the call we never saw replayed.
                call_id = register_call(call_id, name, {})
                parts.append(self._build_tool_use_part(call_id, name, {}))
            args = (calls_by_id.get(call_id) or {}).get("arguments")
            sig = self._tool_signature(name, args)
            executed[sig] = executed.get(sig, 0) + 1
            parts.append(self._build_tool_result_part(
                call_id, name, self._tool_output_text(d), self._tool_output_is_error(d)))
            return parts

        def convert_content(content: Any) -> list:
            """Normalize Kilo/Vercel parts into Cortex content parts."""
            if isinstance(content, str):
                return [{"type": "text", "text": content}] if content else []
            if not isinstance(content, list):
                text = self._text_of(content)
                return [{"type": "text", "text": text}] if text else []

            converted = []
            for part in content:
                if not isinstance(part, dict):
                    text = self._text_of(part)
                    if text:
                        converted.append({"type": "text", "text": text})
                    continue

                ptype = part.get("type")

                if ptype in ("input_text", "text", "output_text"):
                    text = part.get("text", "")
                    if text:
                        converted.append({"type": "text", "text": text})
                    continue

                if ptype in ("input_image", "image_url"):
                    image_url = part.get("image_url", "")
                    if isinstance(image_url, dict):
                        image_url = image_url.get("url")
                    image_url = self._text_of(image_url)
                    if image_url:
                        converted.append({"type": "image_url", "image_url": image_url})
                    continue

                # Tool traffic can also arrive nested inside content arrays.
                if ptype in self.TOOL_CALL_TYPES:
                    converted.append(tool_use_from(part))
                    continue

                if ptype in self.TOOL_OUTPUT_TYPES:
                    converted.extend(tool_result_from(part))
                    continue

                if ptype == "reasoning":
                    # Cortex has no equivalent inbound part; drop it.
                    continue

                text = part.get("text")
                if text:
                    converted.append({"type": "text", "text": self._text_of(text)})
                else:
                    converted.append({"type": "text", "text": json.dumps(part)})

            return converted

        for item in input_items or []:
            if not isinstance(item, dict):
                continue

            itype = item.get("type")

            # Top-level tool traffic (the shape Kilo/the AI SDK actually uses).
            if itype in self.TOOL_CALL_TYPES:
                emit_parts("assistant", [tool_use_from(item)])
                continue

            if itype in self.TOOL_OUTPUT_TYPES:
                emit_parts("assistant", tool_result_from(item))
                continue

            if itype == "reasoning":
                continue

            role = item.get("role")

            if role == "system":
                converted = convert_content(item.get("content", ""))
                if converted:
                    emit_parts("system", converted)

            elif role in ("user", "assistant"):
                converted = convert_content(item.get("content", []))
                if converted:
                    emit_parts(role, converted)

            elif role == "tool":
                # role=tool is not accepted by Cortex; the payload is really a
                # tool result belonging to the assistant turn.
                content = item.get("content")
                if isinstance(content, list) and any(
                        isinstance(p, dict) and p.get("type") in self.TOOL_OUTPUT_TYPES
                        for p in content):
                    emit_parts("assistant", convert_content(content))
                else:
                    emit_parts("assistant", tool_result_from(item))

        # Cortex requires the first non-system message to be from the user...
        if messages:
            first = 0
            while first < len(messages) and messages[first].get("role") == "system":
                first += 1
            if first < len(messages) and messages[first].get("role") != "user":
                messages.insert(first, {"role": "user",
                                        "content": [{"type": "text", "text": "Continue."}]})

        # ...and the last message to be from the user. Verified against the
        # Agent API: omitting this yields "Last message must be from user".
        if messages and messages[-1].get("role") != "user":
            messages.append({"role": "user",
                             "content": [{"type": "text", "text": "Continue."}]})

        self._executed_tool_signatures = executed
        self._saw_tool_output = saw_tool_output
        return messages

    def _convert_openai_messages_to_cortex_messages(self, chat_messages):
        """Convert OpenAI Chat Completions `messages` into Cortex messages.

        Same contract as _convert_kilo_input_to_messages: assistant tool_calls
        and role=tool results are rebuilt as paired tool_use/tool_result parts
        so Cortex knows the tool already ran.
        """
        messages: list = []
        saw_tool_output = False
        executed: Dict[str, int] = {}
        calls_by_id: Dict[str, Dict[str, Any]] = {}

        def append_or_merge(role: str, parts: list):
            if not parts:
                return
            if messages and messages[-1].get("role") == role:
                prev = messages[-1].get("content")
                if isinstance(prev, list):
                    prev.extend(parts)
                    return
            messages.append({"role": role, "content": list(parts)})

        def convert_parts(content: Any) -> list:
            if isinstance(content, str):
                return [{"type": "text", "text": content}] if content else []
            if not isinstance(content, list):
                text = self._text_of(content)
                return [{"type": "text", "text": text}] if text else []

            out = []
            for part in content:
                if not isinstance(part, dict):
                    text = self._text_of(part)
                    if text:
                        out.append({"type": "text", "text": text})
                    continue

                ptype = part.get("type")
                if ptype in ("text", "input_text", "output_text"):
                    text = part.get("text", "")
                    if text:
                        out.append({"type": "text", "text": text})
                    continue

                if ptype in ("image_url", "input_image"):
                    image_url = part.get("image_url")
                    if isinstance(image_url, dict):
                        image_url = image_url.get("url")
                    image_url = self._text_of(image_url)
                    if image_url:
                        out.append({"type": "image_url", "image_url": image_url})
                    continue

                text = part.get("text")
                out.append({"type": "text",
                            "text": self._text_of(text) if text else json.dumps(part)})

            return out

        for msg in chat_messages or []:
            if not isinstance(msg, dict):
                continue

            role = msg.get("role") or "user"

            if role == "tool":
                saw_tool_output = True
                call_id = msg.get("tool_call_id") or msg.get("call_id") or msg.get("id")
                prior = calls_by_id.get(call_id) if call_id else None
                name = msg.get("name") or msg.get("tool_name") or (prior or {}).get("name") or "bash"
                parts = []
                if not call_id or call_id not in calls_by_id:
                    call_id = call_id or f"toolu_{uuid.uuid4().hex}"
                    calls_by_id[call_id] = {"name": name, "arguments": {}}
                    parts.append(self._build_tool_use_part(call_id, name, {}))
                sig = self._tool_signature(
                    name, (calls_by_id.get(call_id) or {}).get("arguments"))
                executed[sig] = executed.get(sig, 0) + 1
                parts.append(self._build_tool_result_part(
                    call_id, name, msg.get("content", ""), self._tool_output_is_error(msg)))
                append_or_merge("assistant", parts)
                continue

            parts = convert_parts(msg.get("content", ""))

            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name") or "bash"
                    args = fn.get("arguments")
                    if args is None:
                        args = tc.get("arguments") or {}
                    call_id = tc.get("id") or tc.get("call_id") or f"toolu_{uuid.uuid4().hex}"
                    calls_by_id[call_id] = {"name": name, "arguments": args}
                    parts.append(self._build_tool_use_part(call_id, name, args))

            if parts:
                append_or_merge(role, parts)

        if messages:
            first = 0
            while first < len(messages) and messages[first].get("role") == "system":
                first += 1
            if first < len(messages) and messages[first].get("role") != "user":
                messages.insert(first, {"role": "user",
                                        "content": [{"type": "text", "text": "Continue."}]})

        if messages and messages[-1].get("role") != "user":
            messages.append({"role": "user",
                             "content": [{"type": "text", "text": "Continue."}]})

        self._executed_tool_signatures = executed
        self._saw_tool_output = saw_tool_output
        return messages

    
    def send_model_error(self, message: str):
        """Report a model problem in whichever dialect the caller is speaking.

        Must be a well-formed stream, not an HTTP error: Kilo renders a stream
        error but treats a bare 4xx as "bad request" with no detail.
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()

        def sse(payload):
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())

        if self.path == "/v1/chat/completions":
            chat_id = f"chatcmpl_{uuid.uuid4().hex}"
            created = int(time.time())
            for delta, finish in (({"role": "assistant", "content": message}, None),
                                  ({}, "stop")):
                sse({"id": chat_id, "object": "chat.completion.chunk",
                     "created": created, "model": self._active_model or "",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
        else:
            item_id = f"msg_{uuid.uuid4().hex}"
            response_id = f"resp_{uuid.uuid4().hex}"
            part = {"type": "output_text", "id": item_id, "text": message, "annotations": []}
            sse({"type": "response.created",
                 "response": {"id": response_id, "object": "response",
                              "status": "in_progress", "output": []}})
            sse({"type": "response.output_item.added", "output_index": 0,
                 "item": {"id": item_id, "type": "message", "role": "assistant",
                          "content": [dict(part, text="")]}})
            sse({"type": "response.output_text.delta", "item_id": item_id,
                 "output_index": 0, "content_index": 0, "delta": message})
            sse({"type": "response.output_text.done", "item_id": item_id,
                 "output_index": 0, "content_index": 0, "text": message})
            sse({"type": "response.output_item.done", "output_index": 0,
                 "item": {"id": item_id, "type": "message", "role": "assistant",
                          "content": [part]}})
            sse({"type": "error", "error": {"message": message, "type": "invalid_request_error"}})
            sse({"type": "response.completed",
                 "response": {"id": response_id, "object": "response",
                              "status": "completed", "stop_reason": "stop",
                              "output": [{"id": item_id, "type": "message",
                                          "role": "assistant", "content": [part]}]}})

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True

    def handle_chat_completion(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(content_length))
            
            # DEBUG: Print EVERYTHING about the request
            print(f"{BLUE}{'='*60}{RESET}", file=sys.stderr)
            print(f"{BLUE}→{RESET} REQUEST PATH: {self.path}", file=sys.stderr)
            print(f"{BLUE}→{RESET} REQUEST HEADERS:", file=sys.stderr)
            for header, value in self.headers.items():
                print(f"{BLUE}→{RESET}   {header}: {value}", file=sys.stderr)
            print(f"{BLUE}→{RESET} REQUEST BODY: {json.dumps(body, indent=2)[:500]}...", file=sys.stderr)
            print(f"{BLUE}{'='*60}{RESET}", file=sys.stderr)

            # Full payload capture: the truncated stderr dump hides exactly the
            # tool-call/tool-output shapes we need when debugging loops.
            if RAW_LOG:
                try:
                    with open(RAW_LOG, "a") as raw_fh:
                        raw_fh.write(json.dumps({
                            "ts": time.time(),
                            "path": self.path,
                            "headers": dict(self.headers.items()),
                            "body": body,
                        }) + "\n")
                except Exception:
                    pass
            
            # Resolve the model BEFORE converting: tool-result truncation is
            # scaled to the model's context window.
            model = body.get("model", DEFAULT_MODEL).replace("openai/", "")
            entry = model_entry(model)
            self._active_model = model

            if entry is None:
                # Kilo merges its own built-in `openai` catalog (gpt-4o, gpt-4.1,
                # gpt-5-pro, chatgpt-image-latest, ...) into the picker, and those
                # ids reach us even though Cortex has never heard of them. Fail
                # here with something actionable instead of relaying an opaque
                # upstream error.
                known = ", ".join(sorted(m for m, e in MODEL_CATALOG.items() if e["available"]))
                msg = (f"Unknown model {model!r}. This proxy serves Snowflake Cortex "
                       f"models only. Available: {known}")
                print(f"{RED}✗{RESET} {msg}", file=sys.stderr)
                self.send_model_error(msg)
                return

            if not entry["available"]:
                msg = (f"Model {model!r} is not available on this Snowflake account "
                       f"({entry['note']}).")
                print(f"{RED}✗{RESET} {msg}", file=sys.stderr)
                self.send_model_error(msg)
                return

            # Handle both Kilo Responses API format (input array) and Chat API format (messages array)
            if "input" in body:
                messages = self._convert_kilo_input_to_messages(body["input"])
                print(f"{BLUE}→{RESET} Converted {len(body['input'])} input items to {len(messages)} messages", file=sys.stderr)
                for i, msg in enumerate(messages):
                    content_preview = str(msg.get("content", ""))[:100]
                    print(f"{BLUE}→{RESET}   Message {i}: role={msg.get('role')}, content={content_preview}", file=sys.stderr)
            else:
                messages = self._convert_openai_messages_to_cortex_messages(body.get("messages", []))
            
            # Decide whether to allow upstream tool invocation. Kilo's follow-up
            # containing tool output is not consistently encoded as role=tool;
            # sometimes it's a top-level `input` item with type=function_call_output.
            # We detect both and also consult per-session state.
            session_id = (
                self.headers.get("x-session-id")
                or self.headers.get("session-id")
                or self.headers.get("x-session-affinity")
                or body.get("session_id")
                or ""
            )

            # The conversion pass already tells us whether this turn carries
            # tool output and which tool calls have results in the history.
            has_tool_output = bool(getattr(self, "_saw_tool_output", False))
            executed_sigs = getattr(self, "_executed_tool_signatures", None) or {}

            stream = body.get("stream", True)  # Kilo defaults to streaming
            tools = body.get("tools", [])

            # Keep every tool with a usable name, in either encoding.
            valid_tools = [t for t in (tools or []) if normalize_tool(t)] or None

            # Cortex rejects a `tools` array outright for models that don't
            # support tool calling, so never forward them.
            if not entry["supports_tools"]:
                if valid_tools:
                    print(f"{YELLOW}!{RESET} {model} does not support tool calling; "
                          f"dropping {len(valid_tools)} tool definition(s)", file=sys.stderr)
                valid_tools = None

            # Tools stay enabled on follow-up turns: multi-step work legitimately
            # needs a second, *different* tool call. Repeats are blocked by
            # signature instead (see stream_response), which is what actually
            # broke the loop -- blanket-disabling tools also broke real work.
            enable_tools = bool(valid_tools)

            # Runaway guard: if a conversation has already executed this many
            # tool calls, force a text answer.
            if sum(executed_sigs.values()) >= MAX_TOOL_STEPS:
                print(f"{YELLOW}!{RESET} tool step cap reached ({len(executed_sigs)}); forcing text answer",
                      file=sys.stderr)
                enable_tools = False

            print(f"{BLUE}→{RESET} enable_tools={enable_tools} has_tool_output={has_tool_output} "
                  f"executed_tools={sum(executed_sigs.values())}", file=sys.stderr)
            
            print(f"{BLUE}→{RESET} {model} | {len(messages)} msg | stream={stream}", file=sys.stderr)
            
            if stream:
                if self.path == "/v1/chat/completions":
                    self.stream_chat_completions(messages, model, valid_tools, enable_tools=enable_tools,
                                                executed_sigs=executed_sigs)
                else:
                    self.stream_response(messages, model, valid_tools, enable_tools=enable_tools,
                                        executed_sigs=executed_sigs)
            else:
                if self.path == "/v1/responses":
                    self.non_stream_responses(messages, model, valid_tools)
                else:
                    self.non_stream_response(messages, model, valid_tools)
                
        except Exception as e:
            import traceback
            print(f"{RED}✗{RESET} Error in handle_chat_completion: {e}", file=sys.stderr)
            print(f"{RED}✗{RESET} Traceback:\n{traceback.format_exc()}", file=sys.stderr)
            self.send_error(500, str(e))
    
    def stream_response(self, messages, model, tools, enable_tools: bool = True,
                        executed_sigs=None):
        """Stream OpenAI Responses-API SSE for Kilo.

        Wire-format rules learned from Kilo's bundled AI SDK:

        * A text part is registered lazily on the FIRST `response.output_text.delta`.
          Emitting `response.output_text.done` (or any delta) for an item that
          never received a delta raises "text part <id> not found", so the
          assistant message item is opened lazily too.
        * `output_index` is resolved positionally against items in ARRIVAL
          order (the SDK does `output.push(item)` then `output[output_index]`),
          so indices must be handed out sequentially as items are emitted --
          never reserved ahead of time.
        * The `output_text` part must be present inline in
          `response.output_item.added`, because we don't emit
          `response.content_part.added`.
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        # Many clients won't consider the stream complete until the server
        # closes the connection after emitting [DONE].
        self.send_header('Connection', 'close')
        self.end_headers()

        # IDs must be unique per request. Kilo associates output_text deltas by
        # ID, and collisions across requests can drop tool-output followups.
        item_id = f"msg_{uuid.uuid4().hex}"
        response_id = f"resp_{uuid.uuid4().hex}"
        text_part_id = item_id
        content_index = 0

        # {signature: times already executed in this conversation}
        executed_counts = dict(executed_sigs or {})
        emitted_sigs = set()
        blocked_repeats = []

        # Only forward tool calls the client actually advertised.
        allowed_tool_names = {n["name"] for n in
                              (normalize_tool(t) for t in (tools or []))
                              if n}

        def sse(payload):
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        print(f"{BLUE}→{RESET} Stream starting", file=sys.stderr)

        sse({
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "output": [],
            },
        })

        # --- streaming state ---
        next_output_index = 0     # next index to hand out, in arrival order
        msg_index = None          # index of the assistant message item, if opened
        msg_closed = False
        full_text = ""
        output_items = []         # completed items, in arrival order
        tool_calls_emitted = 0
        sent_response_completed = False

        def message_item():
            return {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "content": [{
                    "type": "output_text",
                    "id": text_part_id,
                    "text": full_text,
                    "annotations": [],
                }],
            }

        def open_message_item():
            """Open the assistant message item on first text, not before."""
            nonlocal msg_index, next_output_index
            if msg_index is not None:
                return
            msg_index = next_output_index
            next_output_index += 1
            sse({
                "type": "response.output_item.added",
                "output_index": msg_index,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "role": "assistant",
                    # Inline part: we don't send content_part.added, so the
                    # client needs the part to exist from the start.
                    "content": [{
                        "type": "output_text",
                        "id": text_part_id,
                        "text": "",
                        "annotations": [],
                    }],
                },
            })

        def emit_text(text):
            nonlocal full_text
            if not text:
                return
            open_message_item()
            full_text += text
            sse({
                "type": "response.output_text.delta",
                "item_id": text_part_id,
                "output_index": msg_index,
                "content_index": content_index,
                "delta": text,
            })

        def close_message_item():
            """Close the message item. Safe no-op if it was never opened."""
            nonlocal msg_closed
            if msg_index is None or msg_closed:
                return
            msg_closed = True
            sse({
                "type": "response.output_text.done",
                "item_id": text_part_id,
                "output_index": msg_index,
                "content_index": content_index,
                "text": full_text,
            })
            item = message_item()
            sse({
                "type": "response.output_item.done",
                "output_index": msg_index,
                "item": item,
            })
            output_items.append(item)

        def finish(stop_reason):
            nonlocal sent_response_completed
            if sent_response_completed:
                return
            close_message_item()
            sse({
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "status": "completed",
                    "stop_reason": stop_reason,
                    "output": list(output_items),
                },
            })
            sent_response_completed = True

        def emit_tool_call(tool_id, tool_name, tool_input):
            """Emit a function_call item; returns True if it was forwarded."""
            nonlocal next_output_index, tool_calls_emitted

            sig = self._tool_signature(tool_name, tool_input)
            # LOOP BREAKER. A user may legitimately ask to re-run something, so
            # one repeat is allowed; beyond that it is a runaway re-plan (this is
            # what turned a single `find ...` into an endless cycle).
            already = executed_counts.get(sig, 0)
            if sig in emitted_sigs or already >= REPEAT_TOOL_LIMIT:
                blocked_repeats.append(sig)
                print(f"{YELLOW}!{RESET} blocked repeat tool call "
                      f"(ran {already}x already): {sig[:160]}", file=sys.stderr)
                return False
            emitted_sigs.add(sig)

            try:
                tool_args = json.dumps(tool_input)
            except Exception:
                tool_args = json.dumps({"value": str(tool_input)})

            # Any open text must be closed first so indices stay in order.
            close_message_item()

            idx = next_output_index
            next_output_index += 1
            tool_calls_emitted += 1

            print(f"{GREEN}✓{RESET} Emitting function_call: {tool_name} id={tool_id}", file=sys.stderr)

            base = {
                "type": "function_call",
                "id": tool_id,
                "call_id": tool_id,
                "name": tool_name,
            }
            sse({"type": "response.output_item.added", "output_index": idx,
                 "item": dict(base, arguments="")})
            sse({"type": "response.function_call_arguments.delta",
                 "item_id": tool_id, "output_index": idx, "delta": tool_args})
            sse({"type": "response.function_call_arguments.done",
                 "item_id": tool_id, "output_index": idx, "arguments": tool_args})

            done_item = dict(base, arguments=tool_args, status="completed")
            sse({"type": "response.output_item.done", "output_index": idx,
                 "item": done_item})
            output_items.append(done_item)
            return True

        try:
            event_count = 0
            error_message = None

            for event in self.cortex_client.stream_chat(messages, model, tools,
                                                        enable_tools=enable_tools):
                event_count += 1
                event_type = event.get("event")
                data = event.get("data", {})

                print(f"{BLUE}→{RESET} Event #{event_count}: {event_type}", file=sys.stderr)

                if event_type == "response.text.delta":
                    emit_text(data.get("text", ""))

                elif event_type == "response.text":
                    # Snowflake's assembled text. If we somehow received no
                    # deltas, stream it now so the client has something.
                    text = data.get("text") or ""
                    if text and not full_text:
                        emit_text(text)

                elif event_type == "response":
                    print(f"{GREEN}✓{RESET} Sending terminal events", file=sys.stderr)
                    finish("tool_call" if tool_calls_emitted else "stop")

                elif event_type == "response.tool_use":
                    tool_id = (data.get("tool_use_id") or data.get("id")
                               or f"call_{uuid.uuid4().hex}")
                    tool_name = (data.get("name") or data.get("tool_name")
                                 or data.get("tool") or "")
                    tool_input = data.get("input") or data.get("arguments") or {}
                    if not isinstance(tool_input, dict):
                        tool_input = {"value": tool_input}
                    print(f"{BLUE}→{RESET} tool_use raw: name={tool_name!r} "
                          f"keys={sorted(data.keys())} input={json.dumps(tool_input)[:200]}",
                          file=sys.stderr)

                    # Drop anything the client can't run. Rewriting it to a
                    # dummy `bash true` (the old behaviour) fed the client
                    # empty output and guaranteed another identical round trip.
                    if allowed_tool_names and tool_name not in allowed_tool_names:
                        print(f"{YELLOW}!{RESET} dropping tool the client can't run: "
                              f"{tool_name!r}", file=sys.stderr)
                        continue

                    emit_tool_call(tool_id, tool_name, tool_input)

                elif event_type == "error":
                    msg = data.get("message")
                    if not msg and isinstance(data.get("error"), dict):
                        msg = data["error"].get("message")
                    error_message = msg or json.dumps(data)
                    break

            if error_message is None and event_count == 0:
                error_message = "Upstream returned no events"

            # If the only thing upstream wanted was to repeat a command, give
            # the client a real answer -- an empty turn just makes it ask again.
            if not error_message and blocked_repeats and not tool_calls_emitted \
                    and not full_text.strip():
                emit_text(REPEAT_BLOCKED_MESSAGE)

            if error_message:
                print(f"{RED}✗{RESET} Upstream error: {error_message}", file=sys.stderr)
                sse({"type": "error",
                     "error": {"message": error_message, "type": "server_error"}})

            # Always emit terminal events, even on early end or error, or the
            # client hangs with no answer.
            finish("tool_call" if tool_calls_emitted else "stop")

            # Send final [DONE] marker
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

            print(f"{GREEN}✓{RESET} Stream complete. {event_count} events, "
                  f"{tool_calls_emitted} tool call(s), "
                  f"{len(blocked_repeats)} blocked repeat(s)", file=sys.stderr)

            self.close_connection = True
            return

        except Exception as e:
            import traceback
            print(f"{RED}✗{RESET} Error: {e}\n{traceback.format_exc()}", file=sys.stderr)
            try:
                sse({"type": "error", "error": {"message": str(e), "type": "server_error"}})
                finish("stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True
            except Exception:
                pass

    
    def non_stream_response(self, messages, model, tools):
        content = ""
        tool_calls = []
        
        try:
            for event in self.cortex_client.stream_chat(messages, model, tools):
                event_type = event.get("event")
                data = event.get("data", {})
                
                if event_type == "response.text.delta":
                    # Cortex returns "text" field, not "delta"
                    content += data.get("text", "")
                elif event_type == "response.tool_use":
                    tool_calls.append({
                        "id": data.get("id", str(uuid.uuid4())),
                        "type": "function",
                        "function": {
                            "name": data.get("name"),
                            "arguments": json.dumps(data.get("input", {}))
                        }
                    })
            
            message = {"role": "assistant", "content": content}
            if tool_calls:
                message["tool_calls"] = tool_calls
            
            response = {
                "id": str(uuid.uuid4()),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop"
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
            
        except Exception as e:
            import traceback
            print(f"{RED}✗{RESET} Non-stream error: {e}", file=sys.stderr)
            print(f"{RED}✗{RESET} Traceback:\n{traceback.format_exc()}", file=sys.stderr)
            self.send_error(500, str(e))


    def stream_chat_completions(self, messages, model, tools, enable_tools: bool = True,
                                executed_sigs=None):
        """Stream OpenAI Chat Completions compatible SSE.

        Kilo's TUI can hit `/v1/chat/completions` while the CLI tends to hit
        `/v1/responses`. If we respond with Responses-style events on this
        endpoint, Kilo surfaces it as a generic "bad request".
        """
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.end_headers()

        chat_id = f"chatcmpl_{uuid.uuid4().hex}"
        created = int(time.time())

        def send_chunk(delta: Dict[str, Any], finish_reason: Optional[str] = None):
            chunk = {
                "id": chat_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }],
            }
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
            self.wfile.flush()

        # Initial chunk sets role
        send_chunk({"role": "assistant"}, None)

        # Only allow tool calls the client advertised.
        allowed_tool_names = {n["name"] for n in
                              (normalize_tool(t) for t in (tools or []))
                              if n}

        executed_counts = dict(executed_sigs or {})
        emitted_sigs = set()
        blocked_repeats = []
        sent_tool_calls = False
        sent_finish = False
        saw_text = False

        try:
            for event in self.cortex_client.stream_chat(messages, model, tools, enable_tools=enable_tools):
                event_type = event.get("event")
                data = event.get("data", {})

                if event_type == "response.text.delta":
                    text = data.get("text", "")
                    if text:
                        saw_text = True
                        send_chunk({"content": text}, None)

                elif event_type == "response.tool_use":
                    tool_id = data.get("tool_use_id") or data.get("id") or f"call_{uuid.uuid4().hex}"
                    tool_name = data.get("name") or data.get("tool_name") or data.get("tool") or "tool"
                    tool_input = data.get("input") or data.get("arguments") or {}

                    if allowed_tool_names and tool_name not in allowed_tool_names:
                        print(f"{YELLOW}!{RESET} dropping unsupported tool: {tool_name}", file=sys.stderr)
                        continue

                    # Same loop breaker as the Responses path: never hand the
                    # client a call it already executed in this conversation.
                    sig = self._tool_signature(tool_name, tool_input)
                    already = executed_counts.get(sig, 0)
                    if sig in emitted_sigs or already >= REPEAT_TOOL_LIMIT:
                        blocked_repeats.append(sig)
                        print(f"{YELLOW}!{RESET} blocked repeat tool call "
                              f"(ran {already}x already): {sig[:160]}", file=sys.stderr)
                        continue
                    emitted_sigs.add(sig)

                    try:
                        tool_args = json.dumps(tool_input)
                    except Exception:
                        tool_args = json.dumps({"value": str(tool_input)})

                    send_chunk({
                        "tool_calls": [{
                            "index": len(emitted_sigs) - 1,
                            "id": tool_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": tool_args},
                        }]
                    }, None)
                    sent_tool_calls = True

                elif event_type == "response":
                    if sent_tool_calls:
                        send_chunk({}, "tool_calls")
                    else:
                        if blocked_repeats and not saw_text:
                            send_chunk({"content": REPEAT_BLOCKED_MESSAGE}, None)
                        send_chunk({}, "stop")
                    sent_finish = True

                elif event_type == "error":
                    msg = data.get("message")
                    if not msg and isinstance(data.get("error"), dict):
                        msg = data["error"].get("message")
                    send_chunk({"content": f"Upstream error: {msg or json.dumps(data)}"}, "stop")
                    sent_finish = True
                    break

            # Never leave the client without a finish_reason.
            if not sent_finish:
                if sent_tool_calls:
                    send_chunk({}, "tool_calls")
                else:
                    if blocked_repeats and not saw_text:
                        send_chunk({"content": REPEAT_BLOCKED_MESSAGE}, None)
                    send_chunk({}, "stop")

            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
            return

        except Exception as e:
            print(f"{RED}✗{RESET} Chat-completions stream error: {e}", file=sys.stderr)
            try:
                err = {"error": {"message": str(e), "type": "server_error"}}
                self.wfile.write(f"data: {json.dumps(err)}\n\n".encode())
                self.wfile.flush()
            except Exception:
                pass


    def non_stream_responses(self, messages, model, tools):
        """Return a non-stream OpenAI Responses API JSON payload."""
        content = ""

        try:
            for event in self.cortex_client.stream_chat(messages, model, tools):
                event_type = event.get("event")
                data = event.get("data", {})
                if event_type == "response.text.delta":
                    content += data.get("text", "")

            response = {
                "id": f"resp_{uuid.uuid4().hex}",
                "object": "response",
                "created_at": int(time.time()),
                "status": "completed",
                "model": model,
                "output": [{
                    "id": f"msg_{uuid.uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content, "annotations": []}],
                }],
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            }

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        except Exception as e:
            import traceback
            print(f"{RED}✗{RESET} Non-stream /v1/responses error: {e}", file=sys.stderr)
            print(f"{RED}✗{RESET} Traceback:\n{traceback.format_exc()}", file=sys.stderr)
            self.send_error(500, str(e))


def print_kilo_models():
    """Emit the kilo.json provider `models` block straight from the catalog.

    Keeps Kilo's advertised limits and the proxy's routing from drifting apart.
    """
    models = {}
    for model_id, entry in MODEL_CATALOG.items():
        if not entry["available"]:
            continue
        models[model_id] = {
            "name": f"\u2744\ufe0f Snowflake Cortex | {entry['label']}",
            "tool_call": entry["supports_tools"],
            "limit": {"context": entry["context"], "output": entry["max_output"]},
        }
    print(json.dumps(models, indent=2, ensure_ascii=False))


def main():
    if "--print-kilo-models" in sys.argv:
        print_kilo_models()
        return

    print("=" * 70, file=sys.stderr)
    print(f"{BOLD}🚀 Snowflake Cortex Proxy for Kilo Code{RESET}", file=sys.stderr)
    print("=" * 70, file=sys.stderr)
    
    try:
        # Load configuration from kilo.json
        config = load_kilo_config()
        
        # Initialize client
        ProxyHandler.cortex_client = SnowflakeCortexClient(config)
        
        # Test authentication
        ProxyHandler.cortex_client.authenticate()
        
        port = int(os.getenv("PORT", "8080"))
        
        print("=" * 70, file=sys.stderr)
        print(f"{GREEN}✓{RESET} Configuration loaded from kilo.json", file=sys.stderr)
        print(f"{GREEN}✓{RESET} Listening on http://localhost:{port}", file=sys.stderr)
        print(f"   Account:   {config['account']}", file=sys.stderr)
        print(f"   User:      {config['user']}", file=sys.stderr)
        print(f"   Auth:      {config.get('auth', {}).get('type', 'pat')}", file=sys.stderr)
        print(f"   Warehouse: {config.get('warehouse', '(default)')}", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        print(f"\n{YELLOW}Press Ctrl+C to stop{RESET}\n", file=sys.stderr)
        
        # Bind explicitly to IPv4 localhost. Some clients resolve "localhost" to
        # ::1 first; binding to 127.0.0.1 avoids intermittent connect issues.
        server = ThreadingHTTPServer(('127.0.0.1', port), ProxyHandler)
        server.serve_forever()
        
    except KeyboardInterrupt:
        print(f"\n{YELLOW}👋 Shutting down{RESET}", file=sys.stderr)
    except Exception as e:
        print(f"\n{RED}✗ Error: {e}{RESET}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
