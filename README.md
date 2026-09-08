# Kilo x Snowflake Cortex

Run [Kilo Code](https://kilocode.ai) against **Snowflake Cortex** models -- Claude Opus 5, Sonnet 5, GPT 5.4 -- with full agentic tool calling.

Your code and prompts stay inside your Snowflake perimeter. No third-party model API keys required.

## How it works

Snowflake Cortex natively supports the **OpenAI Chat Completions API** at `/api/v2/cortex/v1/chat/completions`. The **`ksc` launcher** manages auth profiles, starts a lightweight proxy (fixes `max_tokens` compatibility), and launches Kilo in one command:

```
ksc <profile>
    |  loads auth profile (PAT / keypair / OAuth / device code)
    |  starts proxy on 127.0.0.1:8080
    |  launches kilo
    v
Kilo  -->  proxy  -->  Snowflake Cortex
           fixes max_tokens,
           injects auth token
```

## Requirements

- Python 3.9+
- [Kilo Code CLI](https://kilocode.ai) -- `npm install -g @kilocode/cli`
- A Snowflake account with Cortex enabled
- A Snowflake **programmatic access token (PAT)**, keypair, or OAuth credentials
- `pip install PyJWT cryptography` (only for keypair or OAuth auth)

## Quick start

```bash
git clone https://github.com/sfc-gh-kkeller/kilo-snowflake-cortex.git
cd kilo-snowflake-cortex

# Install ksc onto PATH
./install-ksc.sh

# Add your first profile
ksc add myaccount

# Launch
ksc myaccount
```

`ksc add` prompts for account, user, auth type, and credentials. Profiles are stored in `~/.config/kilo/ksc-profiles.json`.

### Legacy setup (without ksc)

```bash
# macOS / Linux
./run.sh

# Windows
run.bat
```

On first run, `setup.sh` will prompt for your Snowflake account, user, and PAT, then generate `~/.config/kilo/kilo.json`.

## ksc CLI

```bash
ksc                              # launch with default profile
ksc <profile>                    # launch with named profile
ksc <profile> -- run -m "..."    # launch kilo CLI with args
ksc list                         # list profiles
ksc add <name>                   # add profile interactively
ksc remove <name>                # remove profile
ksc default <name>               # set default profile
ksc status                       # show proxy health
ksc stop                         # stop running proxy
```

### Profile examples

Each profile stores a complete auth configuration:

**PAT** (simplest):
```bash
ksc add prod
# Account: MYORG-MYACCOUNT
# User: me@example.com
# Auth type: pat
# PAT: <hidden>
```

**Keypair JWT**:
```bash
ksc add dev-keypair
# Auth type: privatekey
# Private key path: ~/.snowflake/rsa_key.p8
```

**Snowflake OAuth** (browser-based):
```bash
ksc add staging-oauth
# Auth type: snowflake_oauth
# Client ID: from DESCRIBE INTEGRATION
# Client secret: <hidden>
```

**External OAuth device code**:
```bash
ksc add corp-sso
# Auth type: device_code
# Client ID: my-app-client-id
# Device auth endpoint: https://idp.example.com/device/authorize
# Token endpoint: https://idp.example.com/oauth/token
```

### Persisting refresh tokens

By default, refresh tokens are kept in memory only (most secure). To persist them across restarts:

```bash
ksc myprofile --persist-refresh-token
```

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `KSC_PORT` | `8080` | Proxy listen port |
| `XDG_CONFIG_HOME` | `~/.config` | Config directory |

## Authentication

Set `auth.type` in the profile:

| Type | Fields | Notes |
|---|---|---|
| `pat` | `auth.pat` | Static token. Simplest option |
| `privatekey` | `auth.private_key_path` | Keypair JWT. Re-minted every 55 min. Requires `cryptography` + `PyJWT` |
| `snowflake_oauth` | `auth.client_id`, `auth.client_secret` | Authorization code + PKCE. Opens browser |
| `device_code` | `auth.client_id`, `auth.device_authorization_endpoint`, `auth.token_endpoint`, `auth.scope` | External OAuth. Displays code for browser |

### Auth sidecar (standalone)

The proxy/sidecar can also be used directly without `ksc`:

```bash
python3 proxy/snowflake-auth-sidecar.py --proxy     # proxy mode (recommended)
python3 proxy/snowflake-auth-sidecar.py --once       # authenticate once and exit
python3 proxy/snowflake-auth-sidecar.py --status     # show auth state
python3 proxy/snowflake-auth-sidecar.py --verify     # verify current token
```

## Models

Available models (depends on account/region):

| Model | Context | Max output |
|---|---:|---:|
| `claude-opus-5` | 1,000,000 | 128,000 |
| `claude-opus-4-8` / `4-7` / `4-6` | 1,000,000 | 128,000 |
| `claude-sonnet-5` / `4-6` | 1,000,000 | 64,000 |
| `claude-opus-4-5` / `sonnet-4-5` / `haiku-4-5` | 200,000 | 64,000 |
| `openai-gpt-5.4` | 400,000 | 128,000 |
| `openai-gpt-5.2` | 272,000 | 8,192 |

## Querying Snowflake data via MCP

The auth sidecar handles **inference credentials**. To give Kilo the ability to **query Snowflake data**, register a Snowflake managed MCP server in `kilo.json`:

```json
{
  "mcp": {
    "snowflake": {
      "type": "local",
      "command": ["your-mcp-server", "--args"],
      "enabled": true,
      "timeout": 60000
    }
  }
}
```

## Translating proxy (advanced)

For server-side Cortex Agent tools (Cortex Search, Cortex Analyst, SQL execution), the translating proxy translates between Kilo's OpenAI Responses API and Snowflake's Cortex Agent API:

```bash
python3 proxy/snowflake-cortex-proxy.py
```

This is only needed if you want Snowflake to orchestrate tools server-side. For most use cases, the native OpenAI API with client-side MCP is sufficient.

## Layout

```
proxy/ksc.py                    ksc launcher (profiles + proxy + kilo)
proxy/snowflake-auth-sidecar.py auth token manager and lightweight proxy
proxy/snowflake-cortex-proxy.py translating proxy for Cortex Agent API (advanced)
test/idp.py                     throwaway OIDC IdP for testing device code flow
install-ksc.sh                  symlink ksc onto PATH
run.sh / run.bat                legacy launch scripts
setup.sh / setup.bat            generate kilo.json interactively
LICENSE                         MIT
```
