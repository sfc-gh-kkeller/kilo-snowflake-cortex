#!/usr/bin/env python3
"""ksc — Kilo x Snowflake Cortex launcher.

Manages named auth profiles and launches Kilo with a running proxy.

    ksc                       # launch with default profile
    ksc <profile>             # launch with named profile
    ksc <profile> -- <args>   # launch kilo with extra args
    ksc list                  # list profiles
    ksc add <name>            # add profile interactively
    ksc remove <name>         # remove profile
    ksc default <name>        # set default profile
    ksc setup                 # write model catalog + defaults into kilo.json
    ksc start <profile>       # start proxy only (no kilo)
    ksc status                # show proxy/auth health
    ksc stop                  # stop running proxy
    ksc version               # show version

    ksc add-mcp <name>        # add MCP server connection
    ksc remove-mcp <name>     # remove MCP server connection
    ksc list-mcp              # list MCP server connections

    ksc <profile> --force-reauth   # force full re-authentication
"""
from __future__ import annotations

import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

VERSION = "0.3.0"
IS_WINDOWS = sys.platform == "win32"

# -- ANSI (disabled on Windows unless WT/modern terminal) --
if IS_WINDOWS and "WT_SESSION" not in os.environ:
    BOLD = DIM = RED = GREEN = YELLOW = BLUE = CYAN = RESET = ""
else:
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    RESET = "\033[0m"

# -- Paths (platform-aware) --
if IS_WINDOWS:
    _appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    CONFIG_DIR = _appdata / "kilo"
    STATE_DIR = Path(os.environ.get("LOCALAPPDATA",
                     Path.home() / "AppData" / "Local")) / "kilo-snowflake-cortex"
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME",
                      Path.home() / ".config")) / "kilo"
    STATE_DIR = Path(os.environ.get("XDG_STATE_HOME",
                     Path.home() / ".local" / "state")) / "kilo-snowflake-cortex"

KILO_CONFIG = CONFIG_DIR / "kilo.json"
PROFILES_FILE = CONFIG_DIR / "ksc-profiles.json"
PIDFILE = STATE_DIR / "proxy.pid"
LOGFILE = STATE_DIR / "proxy.log"

SIDECAR = Path(__file__).resolve().parent / "snowflake-auth-sidecar.py"
DEFAULT_PORT = 8080


# =========================================================================
# Model catalog — available Cortex models for kilo.json
# =========================================================================

CORTEX_MODELS = {
    "claude-opus-5": {
        "name": "Snowflake Cortex | Claude Opus 5",
        "tool_call": True,
        "limit": {"context": 1000000, "output": 128000},
    },
    "claude-opus-4-8": {
        "name": "Snowflake Cortex | Claude Opus 4.8",
        "tool_call": True,
        "limit": {"context": 1000000, "output": 128000},
    },
    "claude-opus-4-7": {
        "name": "Snowflake Cortex | Claude Opus 4.7",
        "tool_call": True,
        "limit": {"context": 1000000, "output": 128000},
    },
    "claude-opus-4-6": {
        "name": "Snowflake Cortex | Claude Opus 4.6",
        "tool_call": True,
        "limit": {"context": 1000000, "output": 128000},
    },
    "claude-sonnet-5": {
        "name": "Snowflake Cortex | Claude Sonnet 5",
        "tool_call": True,
        "limit": {"context": 1000000, "output": 64000},
    },
    "claude-sonnet-4-6": {
        "name": "Snowflake Cortex | Claude Sonnet 4.6",
        "tool_call": True,
        "limit": {"context": 1000000, "output": 64000},
    },
    "claude-opus-4-5": {
        "name": "Snowflake Cortex | Claude Opus 4.5",
        "tool_call": True,
        "limit": {"context": 200000, "output": 64000},
    },
    "claude-sonnet-4-5": {
        "name": "Snowflake Cortex | Claude Sonnet 4.5",
        "tool_call": True,
        "limit": {"context": 200000, "output": 64000},
    },
    "claude-haiku-4-5": {
        "name": "Snowflake Cortex | Claude Haiku 4.5",
        "tool_call": True,
        "limit": {"context": 200000, "output": 64000},
    },
    "openai-gpt-5.4": {
        "name": "Snowflake Cortex | OpenAI GPT 5.4",
        "tool_call": True,
        "limit": {"context": 400000, "output": 128000},
    },
    "openai-gpt-5.2": {
        "name": "Snowflake Cortex | OpenAI GPT 5.2",
        "tool_call": True,
        "limit": {"context": 272000, "output": 8192},
    },
}

DEFAULT_MODEL = "openai-compatible/claude-opus-5"
DEFAULT_SMALL_MODEL = "openai-compatible/claude-haiku-4-5"


# =========================================================================
# Profile manager
# =========================================================================

def load_profiles() -> Dict[str, Any]:
    if PROFILES_FILE.exists():
        with open(PROFILES_FILE) as f:
            return json.load(f)
    return {"default": None, "profiles": {}}


def save_profiles(data: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = str(PROFILES_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(PROFILES_FILE))
    if not IS_WINDOWS:
        os.chmod(str(PROFILES_FILE), 0o600)


def get_profile(name: Optional[str]) -> Dict[str, Any]:
    data = load_profiles()
    profiles = data.get("profiles", {})

    if not name:
        name = data.get("default")
    if not name:
        if len(profiles) == 1:
            name = next(iter(profiles))
        elif len(profiles) == 0:
            die("No profiles. Run: ksc add <name>")
        else:
            die(f"Multiple profiles exist. Specify one or set a default.\n"
                f"  Profiles: {', '.join(profiles)}\n"
                f"  Set default: ksc default <name>")

    if name not in profiles:
        die(f"Profile '{name}' not found. Available: {', '.join(profiles) or '(none)'}")

    return profiles[name]


def read_kilo_snowflake_defaults() -> Dict[str, str]:
    defaults = {"account": "", "user": "", "warehouse": "", "role": ""}
    if KILO_CONFIG.exists():
        try:
            with open(KILO_CONFIG) as f:
                cfg = json.load(f)
            sf = cfg.get("provider", {}).get("snowflake-cortex", {})
            defaults["account"] = sf.get("account", "")
            defaults["user"] = sf.get("user", "")
            defaults["warehouse"] = sf.get("warehouse", "")
            defaults["role"] = sf.get("role", "")
        except Exception:
            pass
    return defaults


# =========================================================================
# kilo.json manipulation
# =========================================================================

def write_profile_to_kilo(profile: Dict[str, Any], port: int) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    cfg: Dict[str, Any] = {}
    if KILO_CONFIG.exists():
        with open(KILO_CONFIG) as f:
            cfg = json.load(f)

    provider = cfg.setdefault("provider", {})

    provider["snowflake-cortex"] = {
        "account": profile["account"],
        "user": profile["user"],
        "auth": profile["auth"],
        "warehouse": profile.get("warehouse", ""),
        "role": profile.get("role", ""),
    }

    proxy_url = f"http://127.0.0.1:{port}/v1"
    oc = provider.setdefault("openai-compatible", {})
    oc["api"] = proxy_url
    oc.setdefault("options", {})["baseURL"] = proxy_url

    _write_kilo_config(cfg)


def write_models_to_kilo(port: int = DEFAULT_PORT) -> None:
    """Write the Cortex model catalog and defaults into kilo.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    cfg: Dict[str, Any] = {}
    if KILO_CONFIG.exists():
        with open(KILO_CONFIG) as f:
            cfg = json.load(f)

    cfg.setdefault("$schema", "https://app.kilo.ai/config.json")
    cfg["model"] = DEFAULT_MODEL
    cfg["small_model"] = DEFAULT_SMALL_MODEL

    provider = cfg.setdefault("provider", {})
    proxy_url = f"http://127.0.0.1:{port}/v1"
    oc = provider.setdefault("openai-compatible", {})
    oc.setdefault("name", "Snowflake Cortex")
    oc["api"] = proxy_url
    opts = oc.setdefault("options", {})
    opts["baseURL"] = proxy_url
    oc["models"] = CORTEX_MODELS

    _write_kilo_config(cfg)


def write_mcp_to_kilo(mcp_servers: Dict[str, Any], port: int = DEFAULT_PORT) -> None:
    """Write MCP server entries into kilo.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    cfg: Dict[str, Any] = {}
    if KILO_CONFIG.exists():
        with open(KILO_CONFIG) as f:
            cfg = json.load(f)

    data = load_profiles()
    profiles = data.get("profiles", {})
    mcp = cfg.setdefault("mcp", {})

    for name, srv in mcp_servers.items():
        mtype = srv.get("type", "managed")
        profile_name = srv.get("profile")
        profile = profiles.get(profile_name, {}) if profile_name else {}

        if mtype == "managed":
            mcp[name] = {
                "type": "url",
                "url": f"http://127.0.0.1:{port}/mcp/{name}",
                "enabled": True,
                "timeout": 120000,
            }
        elif mtype == "community":
            command = _build_community_mcp_command(profile, srv)
            mcp[name] = {
                "type": "local",
                "command": command,
                "enabled": True,
                "timeout": 60000,
            }

    _write_kilo_config(cfg)


def _build_community_mcp_command(profile: Dict[str, Any],
                                  srv: Dict[str, Any]) -> List[str]:
    """Build the CLI args for the community snowflake-labs-mcp server."""
    cmd = ["uvx", "snowflake-labs-mcp"]
    if profile.get("account"):
        cmd += ["--account", profile["account"]]
    if profile.get("user"):
        cmd += ["--user", profile["user"]]
    auth = profile.get("auth", {})
    if auth.get("type") == "pat" and auth.get("pat"):
        cmd += ["--pat", auth["pat"]]
    if profile.get("role"):
        cmd += ["--role", profile["role"]]
    if profile.get("warehouse"):
        cmd += ["--warehouse", profile["warehouse"]]
    svc_config = srv.get("service_config")
    if svc_config:
        cmd += ["--service-config-file", str(Path(svc_config).expanduser())]
    return cmd


def _write_kilo_config(cfg: Dict[str, Any]) -> None:
    tmp = str(KILO_CONFIG) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(KILO_CONFIG))


# =========================================================================
# Proxy management (cross-platform)
# =========================================================================

def proxy_pid() -> Optional[int]:
    """Find the PID of a proxy listening on the expected port."""
    if IS_WINDOWS:
        return _proxy_pid_windows()
    return _proxy_pid_unix()


def _proxy_pid_unix() -> Optional[int]:
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{DEFAULT_PORT}", "-sTCP:LISTEN", "-t"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return int(out.split("\n")[0]) if out else None
    except (subprocess.CalledProcessError, ValueError):
        return None


def _proxy_pid_windows() -> Optional[int]:
    try:
        out = subprocess.check_output(
            ["netstat", "-aon"], stderr=subprocess.DEVNULL, text=True,
        )
        for line in out.splitlines():
            if f":{DEFAULT_PORT}" in line and "LISTENING" in line:
                parts = line.strip().split()
                return int(parts[-1])
    except (subprocess.CalledProcessError, ValueError):
        pass
    return None


def is_healthy(port: int = DEFAULT_PORT) -> bool:
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
        return resp.status == 200
    except Exception:
        return False


def stop_proxy(quiet: bool = False) -> None:
    pid = proxy_pid()
    if not pid:
        if not quiet:
            warn("proxy not running")
        return

    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    PIDFILE.unlink(missing_ok=True)
    if not quiet:
        ok(f"stopped proxy (PID {pid})")


def _is_interactive_auth(profile: Dict[str, Any]) -> bool:
    auth_type = profile.get("auth", {}).get("type", "")
    return auth_type in ("device_code", "snowflake_oauth")


def start_proxy(profile: Dict[str, Any], port: int, persist_refresh: bool = False) -> int:
    """Start the auth sidecar in proxy mode. Returns the PID."""
    if is_healthy(port):
        stop_proxy(quiet=True)

    occupied = proxy_pid()
    if occupied:
        die(f"port {port} in use by PID {occupied}. Stop it first or set KSC_PORT.")

    if not SIDECAR.exists():
        die(f"sidecar not found at {SIDECAR}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [sys.executable, str(SIDECAR), "--proxy", "--proxy-port", str(port)]
    if persist_refresh:
        cmd.append("--persist-refresh-token")

    interactive = _is_interactive_auth(profile)
    log = open(LOGFILE, "w")

    kwargs: Dict[str, Any] = {"stdout": log}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    if interactive:
        kwargs["stderr"] = sys.stderr
        timeout = 300
    else:
        kwargs["stderr"] = log
        timeout = 60

    proc = subprocess.Popen(cmd, **kwargs)
    PIDFILE.write_text(str(proc.pid))

    polls = timeout * 2
    for _ in range(polls):
        if is_healthy(port):
            return proc.pid
        if proc.poll() is not None:
            log.close()
            if not interactive:
                lines = LOGFILE.read_text().splitlines()[-20:]
                die(f"proxy exited during startup:\n" + "\n".join(lines))
            else:
                die("proxy exited during auth — was the device code approved?")
        time.sleep(0.5)

    log.close()
    proc.kill()
    if not interactive:
        lines = LOGFILE.read_text().splitlines()[-10:]
        die(f"proxy didn't become healthy in {timeout}s:\n" + "\n".join(lines))
    else:
        die(f"proxy didn't become healthy in {timeout}s — auth may have timed out")
    return 0


# =========================================================================
# Commands
# =========================================================================

def cmd_list() -> None:
    data = load_profiles()
    profiles = data.get("profiles", {})
    default = data.get("default")

    if not profiles:
        print("No profiles. Run: ksc add <name>")
        return

    for name, prof in profiles.items():
        marker = f" {GREEN}(default){RESET}" if name == default else ""
        auth_type = prof.get("auth", {}).get("type", "?")
        account = prof.get("account", "?")
        print(f"  {BOLD}{name}{RESET}{marker}  {DIM}{auth_type} @ {account}{RESET}")


def cmd_add(name: str) -> None:
    data = load_profiles()
    profiles = data.setdefault("profiles", {})

    if name in profiles:
        die(f"Profile '{name}' already exists. Remove it first: ksc remove {name}")

    defaults = read_kilo_snowflake_defaults()

    print(f"\n{BOLD}{BLUE}Add profile: {name}{RESET}\n")

    account = _prompt("Account", defaults.get("account", ""))
    user = _prompt("User", defaults.get("user", ""))
    warehouse = _prompt("Warehouse", defaults.get("warehouse", ""))
    role = _prompt("Role", defaults.get("role", ""))

    auth_type = _prompt("Auth type (pat/privatekey/snowflake_oauth/device_code)", "pat")

    auth: Dict[str, Any] = {"type": auth_type}

    if auth_type == "pat":
        pat = getpass.getpass("PAT (hidden): ")
        if not pat:
            die("PAT is required")
        auth["pat"] = pat

    elif auth_type == "privatekey":
        default_key = "~\\.snowflake\\rsa_key.p8" if IS_WINDOWS else "~/.snowflake/rsa_key.p8"
        auth["private_key_path"] = _prompt("Private key path", default_key)
        pp = getpass.getpass("Passphrase (blank for none): ")
        if pp:
            auth["private_key_passphrase"] = pp

    elif auth_type == "snowflake_oauth":
        auth["client_id"] = _prompt("Client ID", "")
        auth["client_secret"] = getpass.getpass("Client secret: ")
        auth["redirect_port"] = int(_prompt("Redirect port", "8765"))

    elif auth_type == "device_code":
        auth["client_id"] = _prompt("Client ID", "")
        auth["device_authorization_endpoint"] = _prompt("Device auth endpoint", "")
        auth["token_endpoint"] = _prompt("Token endpoint", "")
        auth["scope"] = _prompt("Scope", f"session:role:{role}" if role else "")

    else:
        die(f"Unknown auth type: {auth_type}")

    profile = {
        "account": account,
        "user": user,
        "warehouse": warehouse,
        "role": role,
        "auth": auth,
    }

    profiles[name] = profile
    if data.get("default") is None:
        data["default"] = name
    save_profiles(data)
    ok(f"Profile '{name}' saved")
    if data["default"] == name:
        print(f"  {DIM}(set as default){RESET}")


def cmd_add_json(name: str, json_str: str) -> None:
    data = load_profiles()
    profiles = data.setdefault("profiles", {})
    if name in profiles:
        die(f"Profile '{name}' already exists. Remove it first: ksc remove {name}")
    try:
        profile = json.loads(json_str)
    except json.JSONDecodeError as e:
        die(f"Invalid JSON: {e}")
    if "account" not in profile or "auth" not in profile:
        die("Profile JSON must have at least 'account' and 'auth' fields")
    profiles[name] = profile
    if data.get("default") is None:
        data["default"] = name
    save_profiles(data)
    ok(f"Profile '{name}' saved")


def cmd_remove(name: str) -> None:
    data = load_profiles()
    profiles = data.get("profiles", {})
    if name not in profiles:
        die(f"Profile '{name}' not found")
    del profiles[name]
    if data.get("default") == name:
        data["default"] = next(iter(profiles), None)
    save_profiles(data)
    ok(f"Profile '{name}' removed")


def cmd_default(name: str) -> None:
    data = load_profiles()
    if name not in data.get("profiles", {}):
        die(f"Profile '{name}' not found")
    data["default"] = name
    save_profiles(data)
    ok(f"Default profile set to '{name}'")


def cmd_setup() -> None:
    """Write model catalog, MCP entries, and defaults into kilo.json."""
    port = int(os.environ.get("KSC_PORT", str(DEFAULT_PORT)))
    write_models_to_kilo(port)
    ok(f"wrote {len(CORTEX_MODELS)} models to kilo.json")
    ok(f"default model: {DEFAULT_MODEL}")
    ok(f"small model:   {DEFAULT_SMALL_MODEL}")
    ok(f"proxy URL:     http://127.0.0.1:{port}/v1")

    # Write MCP entries
    data = load_profiles()
    mcp_servers = data.get("mcp_servers", {})
    if mcp_servers:
        write_mcp_to_kilo(mcp_servers, port)
        ok(f"wrote {len(mcp_servers)} MCP server(s) to kilo.json")

    if not PROFILES_FILE.exists() or not data.get("profiles"):
        print(f"\n  Next: ksc add <name>  (create an auth profile)")
    else:
        default = data.get("default", next(iter(data.get("profiles", {})), None))
        print(f"\n  Next: ksc {default}")


def cmd_status() -> None:
    port = int(os.environ.get("KSC_PORT", str(DEFAULT_PORT)))
    if is_healthy(port):
        import urllib.request
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
        health = json.loads(resp.read())
        ok(f"proxy healthy on port {port}")
        for k, v in health.items():
            print(f"  {k}: {v}")
    else:
        pid = proxy_pid()
        if pid:
            warn(f"port {port} occupied (PID {pid}) but /health fails")
        else:
            warn("proxy not running")


def cmd_stop() -> None:
    stop_proxy()


# =========================================================================
# MCP server management
# =========================================================================

def cmd_add_mcp(name: str) -> None:
    data = load_profiles()
    mcp_servers = data.setdefault("mcp_servers", {})
    profiles = data.get("profiles", {})

    if name in mcp_servers:
        die(f"MCP server '{name}' already exists. Remove it first: ksc remove-mcp {name}")

    print(f"\n{BOLD}{BLUE}Add MCP server: {name}{RESET}\n")

    mcp_type = _prompt("Type (managed/community)", "managed")
    if mcp_type not in ("managed", "community"):
        die(f"Unknown type: {mcp_type}. Use 'managed' or 'community'")

    # Pick auth profile
    if not profiles:
        die("No auth profiles. Run: ksc add <name>")
    profile_names = list(profiles.keys())
    default_profile = data.get("default") or profile_names[0]
    profile = _prompt(f"Auth profile ({', '.join(profile_names)})", default_profile)
    if profile not in profiles:
        die(f"Profile '{profile}' not found")

    entry: Dict[str, Any] = {"type": mcp_type, "profile": profile}

    if mcp_type == "managed":
        account = profiles[profile]["account"]
        default_base = f"https://{account}.snowflakecomputing.com"
        print(f"\n  {DIM}URL format: https://<account>.snowflakecomputing.com"
              f"/api/v2/databases/<DB>/schemas/<SCHEMA>/mcp-servers/<NAME>{RESET}")
        url = _prompt("MCP server URL", "")
        if not url:
            db = _prompt("Database", "")
            schema = _prompt("Schema", "")
            server = _prompt("MCP server name", "")
            url = f"{default_base}/api/v2/databases/{db}/schemas/{schema}/mcp-servers/{server}"
        entry["url"] = url

    elif mcp_type == "community":
        entry["service_config"] = _prompt(
            "Service config YAML path",
            str(CONFIG_DIR / "mcp-services.yaml"),
        )

    mcp_servers[name] = entry
    save_profiles(data)
    ok(f"MCP server '{name}' saved (type={mcp_type}, profile={profile})")
    print(f"  {DIM}Run 'ksc setup' to update kilo.json{RESET}")


def cmd_remove_mcp(name: str) -> None:
    data = load_profiles()
    mcp_servers = data.get("mcp_servers", {})
    if name not in mcp_servers:
        die(f"MCP server '{name}' not found")
    del mcp_servers[name]
    save_profiles(data)
    ok(f"MCP server '{name}' removed")
    print(f"  {DIM}Run 'ksc setup' to update kilo.json{RESET}")


def cmd_list_mcp() -> None:
    data = load_profiles()
    mcp_servers = data.get("mcp_servers", {})

    if not mcp_servers:
        print("No MCP servers configured. Run: ksc add-mcp <name>")
        return

    port = int(os.environ.get("KSC_PORT", str(DEFAULT_PORT)))
    for name, cfg in mcp_servers.items():
        mtype = cfg.get("type", "?")
        profile = cfg.get("profile", "?")
        if mtype == "managed":
            url = cfg.get("url", "?")
            proxy_url = f"http://127.0.0.1:{port}/mcp/{name}"
            print(f"  {BOLD}{name}{RESET}  {DIM}managed, profile={profile}{RESET}")
            print(f"    upstream: {url}")
            print(f"    proxy:    {proxy_url}")
        else:
            svc = cfg.get("service_config", "?")
            print(f"  {BOLD}{name}{RESET}  {DIM}community, profile={profile}{RESET}")
            print(f"    service config: {svc}")


def cmd_start(profile_name: Optional[str],
              persist_refresh: bool = False,
              force_reauth: bool = False) -> None:
    """Start proxy only, no kilo."""
    profile = get_profile(profile_name)
    port = int(os.environ.get("KSC_PORT", str(DEFAULT_PORT)))

    auth_type = profile.get("auth", {}).get("type", "?")
    account = profile.get("account", "?")
    pname = profile_name or load_profiles().get("default", "?")

    print(f"\n{BOLD}ksc start{RESET} {CYAN}{pname}{RESET}  "
          f"{DIM}{auth_type} @ {account}{RESET}\n")

    health = _get_running_health(port)
    if health and not force_reauth:
        running_type = health.get("auth_type", "")
        running_account = health.get("account", "")
        expires_in = health.get("expires_in")
        if (running_account == account
                and running_type == auth_type
                and (expires_in is None or expires_in > 60)):
            pid = proxy_pid() or "?"
            ok(f"proxy already running (PID {pid})")
            return

    if force_reauth or health:
        stop_proxy(quiet=True)

    info("writing auth config to kilo.json")
    write_profile_to_kilo(profile, port)
    info(f"starting proxy on port {port}")
    pid = start_proxy(profile, port, persist_refresh)
    ok(f"proxy up (PID {pid})")
    info(f"stop later: ksc stop")


def _get_running_health(port: int) -> Optional[Dict[str, Any]]:
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
        return json.loads(resp.read())
    except Exception:
        return None


def cmd_launch(profile_name: Optional[str], kilo_args: List[str],
               persist_refresh: bool = False,
               force_reauth: bool = False) -> None:
    """Main flow: load profile, reuse or start proxy, launch kilo."""
    profile = get_profile(profile_name)
    port = int(os.environ.get("KSC_PORT", str(DEFAULT_PORT)))

    auth_type = profile.get("auth", {}).get("type", "?")
    account = profile.get("account", "?")
    pname = profile_name or load_profiles().get("default", "?")

    print(f"\n{BOLD}ksc{RESET} {CYAN}{pname}{RESET}  "
          f"{DIM}{auth_type} @ {account}{RESET}\n")

    started_proxy = False
    health = _get_running_health(port)
    if health and not force_reauth:
        running_type = health.get("auth_type", "")
        running_account = health.get("account", "")
        expires_in = health.get("expires_in")
        if (running_account == account
                and running_type == auth_type
                and (expires_in is None or expires_in > 60)):
            pid = proxy_pid() or "?"
            ok(f"reusing running proxy (PID {pid}, {running_type}, "
               f"expires in {expires_in}s)" if expires_in
               else f"reusing running proxy (PID {pid}, {running_type})")
        else:
            reason = []
            if running_account != account:
                reason.append(f"account mismatch ({running_account})")
            if running_type != auth_type:
                reason.append(f"auth type mismatch ({running_type})")
            if expires_in is not None and expires_in <= 60:
                reason.append(f"token expiring ({expires_in}s)")
            info(f"restarting proxy: {', '.join(reason)}")
            health = None

    if force_reauth:
        info("--force-reauth: restarting proxy")
        stop_proxy(quiet=True)
        health = None

    if not health:
        info("writing auth config to kilo.json")
        write_profile_to_kilo(profile, port)
        info(f"starting proxy on port {port}")
        pid = start_proxy(profile, port, persist_refresh)
        ok(f"proxy up (PID {pid})")
        started_proxy = True
    else:
        write_profile_to_kilo(profile, port)

    kilo_bin = _find_kilo()
    if not kilo_bin:
        ok("proxy is running — install Kilo then run: kilo")
        print(f"\n  Install:  npm install -g @kilocode/cli")
        print(f"  Stop:     ksc stop\n")
        return

    ok(f"launching kilo")
    print()

    try:
        cmd = [kilo_bin] + kilo_args
        proc = subprocess.Popen(cmd)
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if started_proxy:
            _handle_proxy_on_exit()
        ok("done")


# =========================================================================
# Helpers
# =========================================================================

def _get_setting(key: str) -> Optional[str]:
    data = load_profiles()
    return data.get("settings", {}).get(key)


def _set_setting(key: str, value: Any) -> None:
    data = load_profiles()
    data.setdefault("settings", {})[key] = value
    save_profiles(data)


def _handle_proxy_on_exit() -> None:
    pref = _get_setting("on_exit")

    if pref == "keep":
        info("kilo exited, proxy still running (on_exit=keep)")
        info("stop later: ksc stop")
        return
    if pref == "stop":
        info("kilo exited, stopping proxy (on_exit=stop)")
        stop_proxy(quiet=True)
        return

    print()
    try:
        answer = input(
            f"{YELLOW}?{RESET} Kilo exited. Stop the proxy? "
            f"[Y]es / [n]o / [a]lways stop / n[e]ver stop: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "y"

    if answer in ("a", "always"):
        _set_setting("on_exit", "stop")
        ok("saved preference: on_exit=stop")
        stop_proxy(quiet=True)
    elif answer in ("e", "never"):
        _set_setting("on_exit", "keep")
        ok("saved preference: on_exit=keep")
        info("proxy still running. Stop later: ksc stop")
    elif answer in ("n", "no"):
        info("proxy still running. Stop later: ksc stop")
    else:
        stop_proxy(quiet=True)


def _prompt(label: str, default: str) -> str:
    if default:
        val = input(f"  {label} [{default}]: ").strip()
        return val if val else default
    else:
        val = input(f"  {label}: ").strip()
        return val


def _find_kilo() -> Optional[str]:
    from shutil import which
    if IS_WINDOWS:
        kilo_home = Path.home() / ".kilo" / "bin" / "kilo.exe"
        if kilo_home.exists():
            return str(kilo_home)
        return which("kilo") or which("kilo.exe")
    else:
        kilo_home = Path.home() / ".kilo" / "bin" / "kilo"
        if kilo_home.exists() and os.access(str(kilo_home), os.X_OK):
            return str(kilo_home)
        return which("kilo")


def ok(msg: str) -> None:
    print(f"{GREEN}ok{RESET} {msg}", file=sys.stderr)

def info(msg: str) -> None:
    print(f"{BLUE}->{RESET} {msg}", file=sys.stderr)

def warn(msg: str) -> None:
    print(f"{YELLOW}!{RESET} {msg}", file=sys.stderr)

def die(msg: str) -> None:
    print(f"{RED}error:{RESET} {msg}", file=sys.stderr)
    sys.exit(1)


# =========================================================================
# CLI entry point
# =========================================================================

def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("--help", "-h"):
        print(__doc__)
        return

    cmd = args[0]

    if cmd == "list":
        cmd_list()
    elif cmd == "add":
        if len(args) < 2:
            die("Usage: ksc add <name> [--json '{...}']")
        name = args[1]
        if "--json" in args:
            idx = args.index("--json")
            if idx + 1 >= len(args):
                die("--json requires a value")
            cmd_add_json(name, args[idx + 1])
        else:
            cmd_add(name)
    elif cmd == "remove":
        if len(args) < 2:
            die("Usage: ksc remove <name>")
        cmd_remove(args[1])
    elif cmd == "default":
        if len(args) < 2:
            die("Usage: ksc default <name>")
        cmd_default(args[1])
    elif cmd == "add-mcp":
        if len(args) < 2:
            die("Usage: ksc add-mcp <name>")
        cmd_add_mcp(args[1])
    elif cmd == "remove-mcp":
        if len(args) < 2:
            die("Usage: ksc remove-mcp <name>")
        cmd_remove_mcp(args[1])
    elif cmd == "list-mcp":
        cmd_list_mcp()
    elif cmd == "setup":
        cmd_setup()
    elif cmd == "start":
        rest = args[1:]
        profile_name = rest[0] if rest and not rest[0].startswith("--") else None
        persist = "--persist-refresh-token" in rest
        force = "--force-reauth" in rest
        cmd_start(profile_name, persist_refresh=persist, force_reauth=force)
    elif cmd == "status":
        cmd_status()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "version":
        print(f"ksc {VERSION}")
    else:
        profile_name = cmd
        kilo_args: List[str] = []
        persist = False

        rest = args[1:]
        if "--persist-refresh-token" in rest:
            persist = True
            rest.remove("--persist-refresh-token")
        if "--force-reauth" in rest:
            force = True
            rest.remove("--force-reauth")
        else:
            force = False

        if "--" in rest:
            idx = rest.index("--")
            kilo_args = rest[idx + 1:]
        elif rest:
            kilo_args = rest

        cmd_launch(profile_name, kilo_args, persist_refresh=persist,
                   force_reauth=force)


if __name__ == "__main__":
    main()
