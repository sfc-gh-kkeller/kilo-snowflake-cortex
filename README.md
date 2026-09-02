# Kilo x Snowflake Cortex

Run [Kilo Code](https://kilocode.ai) against **Snowflake Cortex** models -- Claude Opus 5, Sonnet 5, GPT 5.4 -- with full agentic tool calling, through a local OpenAI-compatible proxy.

Your code and prompts stay inside your Snowflake perimeter. No third-party model API keys required.

## How it works

Kilo Code speaks the OpenAI **Responses API**. Snowflake Cortex speaks the **Cortex Agent API**. The proxy sits between them and translates both directions, including the tool-calling round trip:

```
Kilo Code (Responses API)
    |
    |  POST /v1/responses
    |  SSE: output_text.delta / function_call
    v
snowflake-cortex-proxy.py  (localhost:8080)
    |
    |  POST /api/v2/cortex/agent:run
    |  SSE: response.text.delta / response.tool_use
    v
Snowflake Cortex  (Claude / GPT)
```

Kilo executes tools locally (`bash`, `read`, `edit`, `grep`, ...) and the proxy replays each result back to Cortex so the model knows its commands already ran.

## Requirements

- Python 3.9+ (3.11+ for `~/.snowflake/config.toml` support)
- [Kilo Code CLI](https://kilocode.ai) -- `npm install -g @kilocode/cli`
- A Snowflake account with Cortex enabled
- A Snowflake **programmatic access token (PAT)**, keypair, or OAuth credentials

## Quick start

```bash
git clone https://github.com/sfc-gh-kkeller/kilo-snowflake-cortex.git
cd kilo-snowflake-cortex

# macOS / Linux
./run.sh

# Windows
run.bat
```

On first run, `setup.sh` / `setup.bat` will prompt for your Snowflake account, user, and PAT, then generate `~/.config/kilo/kilo.json` with the proxy config and model catalog. After setup, it starts the proxy and launches Kilo.

Subsequent runs skip setup and start instantly (the proxy survives Kilo exiting).

```bash
./run.sh --no-kilo      # start proxy only
./run.sh --stop         # stop the proxy
./run.sh --status       # check proxy health
```

For CI or non-interactive use:

```bash
SNOWFLAKE_PAT=... ./setup.sh --account MYORG-MYACCOUNT --user me@example.com \
    --warehouse MY_WH --role MY_ROLE --non-interactive
./run.sh
```

### Manual setup

If you prefer to configure everything by hand:

#### 1. Configure Kilo

Create or edit `~/.config/kilo/kilo.json` (on Windows: `%APPDATA%\kilo\kilo.json`):

```json
{
  "$schema": "https://app.kilo.ai/config.json",
  "model": "openai/claude-opus-5",
  "small_model": "openai/claude-haiku-4-5",

  "provider": {
    "snowflake-cortex": {
      "account": "MYORG-MYACCOUNT",
      "user": "me@example.com",
      "auth": {
        "type": "pat",
        "pat": "YOUR_PAT_HERE"
      },
      "warehouse": "MY_WH",
      "role": "MY_ROLE"
    },

    "openai": {
      "name": "Snowflake Cortex",
      "api": "http://127.0.0.1:8080/v1",
      "options": {
        "baseURL": "http://127.0.0.1:8080/v1",
        "apiKey": "sk-local-proxy"
      },
      "models": {
        "claude-opus-5": {
          "name": "Snowflake Cortex | Claude Opus 5",
          "tool_call": true,
          "limit": { "context": 1000000, "output": 128000 }
        },
        "claude-sonnet-5": {
          "name": "Snowflake Cortex | Claude Sonnet 5",
          "tool_call": true,
          "limit": { "context": 1000000, "output": 64000 }
        }
      }
    }
  }
}
```

> Run `python3 proxy/snowflake-cortex-proxy.py --print-kilo-models` to generate the full model block with all available models and correct context windows.

#### 2. Start the proxy

```bash
python3 proxy/snowflake-cortex-proxy.py
```

#### 3. Launch Kilo

```bash
kilo
```

Select a Snowflake Cortex model from the picker and start coding.

## Models

The proxy's `MODEL_CATALOG` is the single source of truth for context windows, output limits, and routing. Available models (depends on account/region):

| Model | Context | Max output |
|---|---:|---:|
| `claude-opus-5` | 1,000,000 | 128,000 |
| `claude-opus-4-8` / `4-7` / `4-6` | 1,000,000 | 128,000 |
| `claude-sonnet-5` / `4-6` | 1,000,000 | 64,000 |
| `claude-opus-4-5` / `sonnet-4-5` / `haiku-4-5` | 200,000 | 64,000 |
| `openai-gpt-5.4` | 400,000 | 128,000 |
| `openai-gpt-5.2` | 272,000 | 8,192 |

`small_model` (title generation) defaults to `claude-haiku-4-5` to avoid burning Opus credits on housekeeping.

## Querying Snowflake data via MCP

The proxy handles **inference** (the model). To give Kilo the ability to **query Snowflake data**, register a Snowflake managed MCP server in your `kilo.json`. Snowflake hosts managed MCP servers that provide SQL query execution, object management, semantic views, and Cortex AI services. See [Snowflake MCP documentation](https://docs.snowflake.com) for available options.

To register any MCP server in Kilo, add an `mcp` block to `kilo.json`:

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

## Authentication

The proxy supports multiple auth methods. Set the `auth.type` field in `provider.snowflake-cortex`:

| Type | Fields | Notes |
|---|---|---|
| `pat` | `auth.pat` | Programmatic access token (recommended for getting started) |
| `privatekey` | `auth.private_key_path`, `auth.private_key_passphrase` | Keypair JWT -- recommended for production / service accounts. Requires `cryptography` package |
| `snowflake_oauth` | `auth.client_id`, `auth.client_secret`, `auth.scope` | Snowflake OAuth (authorization code + PKCE). Opens browser to authorize. Requires a Snowflake OAuth security integration |
| `device_code` | `auth.client_id`, `auth.device_authorization_endpoint`, `auth.token_endpoint`, `auth.scope` | External OAuth device code flow. Displays a code to enter in browser. Requires an external OAuth integration in Snowflake |

### Config examples

**PAT** (simplest):
```json
"auth": { "type": "pat", "pat": "eyJra..." }
```

**Keypair JWT** (no secrets stored in Snowflake):
```json
"auth": {
  "type": "privatekey",
  "private_key_path": "~/.snowflake/rsa_key.p8",
  "private_key_passphrase": ""
}
```

**Snowflake OAuth** (browser-based, supports refresh):
```json
"auth": {
  "type": "snowflake_oauth",
  "client_id": "from DESCRIBE INTEGRATION",
  "client_secret": "from SYSTEM$SHOW_OAUTH_CLIENT_SECRETS()",
  "scope": "session:role:MY_ROLE"
}
```

**External OAuth device code** (for external IdPs like Azure AD, Okta):
```json
"auth": {
  "type": "device_code",
  "client_id": "my-app-client-id",
  "device_authorization_endpoint": "https://idp.example.com/device/authorize",
  "token_endpoint": "https://idp.example.com/oauth/token",
  "scope": "snowflake"
}
```


## Configuration reference

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PROXY_PORT` | `8080` | Listen port |
| `PROXY_RAW_LOG` | `/tmp/kilo-raw.jsonl` | Full request capture; set empty to disable |
| `PROXY_UPSTREAM_TIMEOUT` | `600` | Upstream read timeout (seconds) |
| `PROXY_MAX_TOOL_STEPS` | `30` | Tool calls per conversation before forcing an answer |
| `PROXY_REPEAT_TOOL_LIMIT` | `2` | Identical tool calls allowed before blocking a loop |

### Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/v1/responses` | POST | Responses API (primary -- what Kilo uses) |
| `/v1/chat/completions` | POST | Chat Completions API (alternative) |
| `/v1/models` | GET | List available models |
| `/health` | GET | Health check with token age |

## Safety

- **Read-only by default** -- the proxy does not grant any write access to Snowflake. It is an inference proxy only.
- **Loop protection** -- identical tool calls are capped at 2 per conversation. A hard ceiling of 30 total tool steps prevents runaway sessions.
- **`PROXY_RAW_LOG`** captures full request bodies for debugging. Treat it as sensitive or disable it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Kilo says "Cannot connect to API" | Proxy isn't running -- use `./run.sh` or `python3 proxy/snowflake-cortex-proxy.py` |
| Model answers nothing, returns fast | Session token expired. Proxy auto-refreshes; check `curl localhost:8080/health` |
| `Unknown model 'gpt-4o'` | That's Kilo's built-in OpenAI catalog, not a Cortex model. Pick a Snowflake Cortex entry |
| Model rejected as unavailable | Account/region entitlement. The error includes Snowflake's reason |
| Proxy won't start, port in use | `lsof -i :8080` (macOS/Linux) or `netstat -aon | findstr :8080` (Windows), or set `PROXY_PORT=8081` |

## Layout

```
run.sh / run.bat                 launch proxy + kilo (calls setup if needed)
setup.sh / setup.bat             generate kilo.json interactively or via env vars
proxy/snowflake-cortex-proxy.py  the translating proxy (single file, stdlib only)
LICENSE                          MIT
```
