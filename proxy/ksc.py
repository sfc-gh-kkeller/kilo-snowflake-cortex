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
    ksc status                # show proxy/auth health
    ksc stop                  # stop running proxy

    ksc start <profile>          # start proxy only (no kilo)
    ksc <profile> --force-reauth   # force full re-authentication
"""
from __future__ import annotations

import getpass
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# -- ANSI --
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
CYAN = "\033[96m"
RESET = "\033[0m"

# -- Paths --
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "kilo"
KILO_CONFIG = CONFIG_DIR / "kilo.json"
PROFILES_FILE = CONFIG_DIR / "ksc-profiles.json"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "kilo-snowflake-cortex"
PIDFILE = STATE_DIR / "proxy.pid"
LOGFILE = STATE_DIR / "proxy.log"

SIDECAR = Path(__file__).resolve().parent / "snowflake-auth-sidecar.py"
DEFAULT_PORT = 8080


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
    """Read current snowflake-cortex config from kilo.json for defaults."""
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
    """Write the profile's auth config into kilo.json provider.snowflake-cortex
    and point openai-compatible at the local proxy."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    cfg: Dict[str, Any] = {}
    if KILO_CONFIG.exists():
        with open(KILO_CONFIG) as f:
            cfg = json.load(f)

    provider = cfg.setdefault("provider", {})

    # Write snowflake-cortex auth
    provider["snowflake-cortex"] = {
        "account": profile["account"],
        "user": profile["user"],
        "auth": profile["auth"],
        "warehouse": profile.get("warehouse", ""),
        "role": profile.get("role", ""),
    }

    # Point openai-compatible at local proxy
    proxy_url = f"http://127.0.0.1:{port}/v1"
    oc = provider.setdefault("openai-compatible", {})
    oc["api"] = proxy_url
    oc.setdefault("options", {})["baseURL"] = proxy_url

    tmp = str(KILO_CONFIG) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(KILO_CONFIG))


# =========================================================================
# Proxy management
# =========================================================================

def proxy_pid() -> Optional[int]:
    """Find the PID of a proxy listening on the expected port."""
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{DEFAULT_PORT}", "-sTCP:LISTEN", "-t"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return int(out.split("\n")[0]) if out else None
    except (subprocess.CalledProcessError, ValueError):
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
    if interactive:
        # Show stderr so user sees device code / browser URL
        log = open(LOGFILE, "w")
        proc = subprocess.Popen(cmd, stdout=log, stderr=sys.stderr)
        timeout = 300  # 5 min for user to approve
    else:
        log = open(LOGFILE, "w")
        proc = subprocess.Popen(cmd, stdout=log, stderr=log)
        timeout = 60

    PIDFILE.write_text(str(proc.pid))

    polls = timeout * 2  # 0.5s intervals
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
    return 0  # unreachable


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
        auth["private_key_path"] = _prompt("Private key path", "~/.snowflake/rsa_key.p8")
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
    """Return health JSON from a running proxy, or None."""
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

    # Check if there's already a healthy proxy matching this profile
    started_proxy = False
    health = _get_running_health(port)
    if health and not force_reauth:
        running_type = health.get("auth_type", "")
        running_account = health.get("account", "")
        expires_in = health.get("expires_in")
        # Reuse if same account and auth type, and token isn't about to expire
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
            health = None  # fall through to start

    if force_reauth:
        info("--force-reauth: restarting proxy")
        stop_proxy(quiet=True)
        health = None

    if not health:
        # Write profile into kilo.json and start proxy
        info("writing auth config to kilo.json")
        write_profile_to_kilo(profile, port)
        info(f"starting proxy on port {port}")
        pid = start_proxy(profile, port, persist_refresh)
        ok(f"proxy up (PID {pid})")
        started_proxy = True
    else:
        # Ensure kilo.json points at the running proxy
        write_profile_to_kilo(profile, port)

    # Find kilo
    kilo_bin = _find_kilo()
    if not kilo_bin:
        ok("proxy is running — install Kilo then run: kilo")
        print(f"\n  Install:  npm install -g @kilocode/cli")
        print(f"  Stop:     ksc stop\n")
        return

    # Launch kilo
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
    """Read a setting from the profiles file's top-level 'settings' object."""
    data = load_profiles()
    return data.get("settings", {}).get(key)


def _set_setting(key: str, value: Any) -> None:
    """Write a setting to the profiles file."""
    data = load_profiles()
    data.setdefault("settings", {})[key] = value
    save_profiles(data)


def _handle_proxy_on_exit() -> None:
    """Decide whether to stop the proxy when kilo exits."""
    pref = _get_setting("on_exit")  # "stop", "keep", or None (ask)

    if pref == "keep":
        info("kilo exited, proxy still running (on_exit=keep)")
        info("stop later: ksc stop")
        return
    if pref == "stop":
        info("kilo exited, stopping proxy (on_exit=stop)")
        stop_proxy(quiet=True)
        return

    # No preference set — ask
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
    kilo_home = Path.home() / ".kilo" / "bin" / "kilo"
    if kilo_home.exists() and os.access(str(kilo_home), os.X_OK):
        return str(kilo_home)
    from shutil import which
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

    if not args or args[0] == "--help" or args[0] == "-h":
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
    elif cmd == "start":
        # ksc start [profile] [--persist-refresh-token] [--force-reauth]
        rest = args[1:]
        profile_name = rest[0] if rest and not rest[0].startswith("--") else None
        persist = "--persist-refresh-token" in rest
        force = "--force-reauth" in rest
        cmd_start(profile_name, persist_refresh=persist, force_reauth=force)
    elif cmd == "status":
        cmd_status()
    elif cmd == "stop":
        cmd_stop()
    else:
        # ksc <profile> [-- <kilo args>]
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
