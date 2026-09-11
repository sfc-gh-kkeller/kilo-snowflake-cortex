"""Single source of truth for the Cortex model catalog.

Both snowflake-cortex-proxy.py and ksc.py import MODEL_CATALOG from here so
the context windows, backend routing, and availability notes can't drift
between the two.
"""

from typing import Any, Dict, Optional

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


def kilo_models_block() -> Dict[str, Dict[str, Any]]:
    """Build the kilo.json provider `models` block from the catalog.

    Keeps Kilo's advertised limits and the proxy's routing from drifting
    apart -- both ksc.py (writing kilo.json) and
    snowflake-cortex-proxy.py's --print-kilo-models consume this.
    """
    models: Dict[str, Dict[str, Any]] = {}
    for model_id, entry in MODEL_CATALOG.items():
        if not entry["available"]:
            continue
        models[model_id] = {
            "name": f"Snowflake Cortex | {entry['label']}",
            "tool_call": entry["supports_tools"],
            "limit": {"context": entry["context"], "output": entry["max_output"]},
        }
    return models
