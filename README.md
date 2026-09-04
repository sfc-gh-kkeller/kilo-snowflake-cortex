# Kilo x Snowflake Cortex

Run [Kilo Code](https://kilocode.ai) against **Snowflake Cortex** models -- Claude Opus 5, Sonnet 5, GPT 5.4 -- with full agentic tool calling.

Your code and prompts stay inside your Snowflake perimeter. No third-party model API keys required.

## How it works

Snowflake Cortex natively supports the **OpenAI Chat Completions API** at `/api/v2/cortex/v1/chat/completions`. Kilo Code points directly at Snowflake -- no proxy needed for inference.

The **auth sidecar** manages token lifecycle (minting, refreshing, persisting) and writes a valid Bearer token into `kilo.json` so Kilo always has a fresh credential:

```
snowflake-auth-sidecar.py
    |  authenticates (PAT / keypair / OAuth / device code)
    |  writes Bearer token into kilo.json
    v
kilo.json  -->  Kilo Code  -->  Snowflake Cortex (OpenAI API)
    ^                               /api/v2/cortex/v1/chat/completions
    |
    +-- refreshes token before expiry
```

For advanced use cases (server-side Cortex Agent tools like Cortex Search, Cortex Analyst, SQL execution), the **translating proxy** is also included. It translates between Kilo's OpenAI Responses API and Snowflake's Cortex Agent API.

## Requirements

- Python 3.9+
- [Kilo Code CLI](https://kilocode.ai) -- `npm install -g @kilocode/cli`
- A Snowflake account with Cortex enabled
- A Snowflake **programmatic access token (PAT)**, keypair, or OAuth credentials
- `pip install PyJWT cryptography` (for keypair or OAuth auth)

## Quick start

```bash
git clone https://github.com/sfc-gh-kkeller/kilo-snowflake-cortex.git
cd kilo-snowflake-cortex

# macOS / Linux
./run.sh

# Windows
run.bat
```

On first run, `setup.sh` / `setup.bat` will prompt for your Snowflake account, user, and PAT, then generate `~/.config/kilo/kilo.json`. After setup, it starts the auth sidecar and launches Kilo.

### Manual setup

#### 1. Configure Kilo

Create or edit `~/.config/kilo/kilo.json`:

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
      "api": "https://MYORG-MYACCOUNT.snowflakecomputing.com/api/v2/cortex/v1",
      "options": {
        "baseURL": "https://MYORG-MYACCOUNT.snowflakecomputing.com/api/v2/cortex/v1",
        "apiKey": "YOUR_PAT_OR_JWT_HERE"
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

> The `provider.openai.options.apiKey` is managed by the auth sidecar. For PAT auth, you can set it manually and skip the sidecar.

#### 2. Start the auth sidecar (for non-PAT auth)

```bash
# Authenticate once and update kilo.json
python3 proxy/snowflake-auth-sidecar.py --once

# Or run continuously (refreshes tokens before expiry)
python3 proxy/snowflake-auth-sidecar.py
```

#### 3. Launch Kilo

```bash
kilo
```

## Authentication

Set `auth.type` in `provider.snowflake-cortex`:

| Type | Fields | Notes |
|---|---|---|
| `pat` | `auth.pat` | Static token. Sidecar writes it to `apiKey` once. Simplest option |
| `privatekey` | `auth.private_key_path` | Keypair JWT. Sidecar re-mints every 55 min. Requires `cryptography` + `PyJWT` |
| `snowflake_oauth` | `auth.client_id`, `auth.client_secret` | Authorization code + PKCE. Opens browser. Refresh tokens persisted across restarts |
| `device_code` | `auth.client_id`, `auth.device_authorization_endpoint`, `auth.token_endpoint`, `auth.scope` | External OAuth. Displays code for browser. Refresh tokens persisted |

### Config examples

**PAT** (simplest):
```json
"auth": { "type": "pat", "pat": "eyJra..." }
```

**Keypair JWT**:
```json
"auth": {
  "type": "privatekey",
  "private_key_path": "~/.snowflake/rsa_key.p8"
}
```

**Snowflake OAuth** (browser-based):
```json
"auth": {
  "type": "snowflake_oauth",
  "client_id": "from DESCRIBE INTEGRATION",
  "client_secret": "from SYSTEM$SHOW_OAUTH_CLIENT_SECRETS()"
}
```

**External OAuth device code**:
```json
"auth": {
  "type": "device_code",
  "client_id": "my-app-client-id",
  "device_authorization_endpoint": "https://idp.example.com/device/authorize",
  "token_endpoint": "https://idp.example.com/oauth/token",
  "scope": "session:role:MY_ROLE"
}
```

### Auth sidecar CLI

```bash
python3 proxy/snowflake-auth-sidecar.py --once       # authenticate once, update kilo.json, exit
python3 proxy/snowflake-auth-sidecar.py               # run continuously with refresh loop
python3 proxy/snowflake-auth-sidecar.py --status      # show current auth state
python3 proxy/snowflake-auth-sidecar.py --verify      # verify the token in kilo.json works
python3 proxy/snowflake-auth-sidecar.py --provider X  # write to provider.X instead of provider.openai
```

The sidecar persists refresh tokens in `~/.config/kilo/snowflake-auth-state.json` so it can resume without re-prompting after a restart.

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

Run `python3 proxy/snowflake-cortex-proxy.py --print-kilo-models` to generate the full model block for kilo.json.

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
proxy/snowflake-auth-sidecar.py  auth token manager (primary)
proxy/snowflake-cortex-proxy.py  translating proxy for Cortex Agent API (advanced)
test/idp.py                      throwaway OIDC IdP for testing device code flow
run.sh / run.bat                 launch scripts
setup.sh / setup.bat             generate kilo.json interactively
LICENSE                          MIT
```
