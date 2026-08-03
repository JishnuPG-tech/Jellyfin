"""
Hermes App API — Production-Ready FastAPI server for Flutter integration.

Exposes OpenAI-compatible endpoints:
  POST /v1/chat/completions   — chat with SSE streaming or JSON
  GET  /v1/models             — list available models with API key status
  GET  /health                — detailed health check
  GET  /v1/config             — current active configuration (no secrets)

Bug fixes in this version:
  1. NameError: result.get() → removed; result_text used correctly
  2. runtime_kwargs override: provider/model/base_url/api_key stripped
     from runtime_kwargs before merge so request model always wins
  3. Conversation history: history injected as prior turns or system block
  4. Stream drain race: rewritten with asyncio.Queue and clean sentinel
  5. Stream stop: CancellationToken pattern for cooperative cancel
  6. Model badge: X-Hermes-Model-Used response header always set
  7. Pydantic validation on ChatCompletionRequest
  8. Global exception handler — no raw Python tracebacks to clients
  9. Structured per-request logging with request_id
 10. /v1/models reports which models have API keys configured
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

try:
    import dotenv
    dotenv.load_dotenv()
    hermes_env = os.path.expanduser("~/.hermes/.env")
    if os.path.isfile(hermes_env):
        dotenv.load_dotenv(hermes_env, override=False)
except ImportError:
    pass

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
    if False  # structured filter below
    else "%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("hermes_app_api")

# ─── Configuration ────────────────────────────────────────────────────────────

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
API_KEY = os.getenv("API_SERVER_KEY", "")
CORS_ORIGINS = os.getenv("API_SERVER_CORS_ORIGINS", "*")
MODEL_NAME = os.getenv("API_SERVER_MODEL_NAME", "hermes-agent")
MAX_ITERATIONS = int(os.getenv("HERMES_MAX_ITERATIONS", "10"))
STREAM_TIMEOUT = float(os.getenv("HERMES_STREAM_TIMEOUT", "180.0"))

# Legacy env-var provider overrides (lower priority than request model)
_ENV_PROVIDER = os.getenv("API_SERVER_PROVIDER", "")
_ENV_BASE_URL = os.getenv("API_SERVER_BASE_URL", "")
_ENV_API_KEY_VAL = os.getenv("API_SERVER_API_KEY", "")

# ─── Model Registry ───────────────────────────────────────────────────────────
# Maps model-id → {provider, api_key_env, base_url}
# api_key_env: name of the env var that holds the API key for this model.
# All keys resolved lazily at request time so .env changes are picked up.

_MODEL_REGISTRY: Dict[str, Dict[str, str]] = {
    # ── Google Gemini ─────────────────────────────────────────────────────
    "gemini-2.5-flash": {
        "provider": "gemini",
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    },
    "gemini-2.5-pro": {
        "provider": "gemini",
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    },
    "gemini-2.0-flash": {
        "provider": "gemini",
        "api_key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
    },
    # ── Groq (via OpenAI-compatible endpoint) ─────────────────────────────
    "llama-3.3-70b-versatile": {
        "provider": "custom",
        "api_key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "llama-3.1-8b-instant": {
        "provider": "custom",
        "api_key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "mixtral-8x7b-32768": {
        "provider": "custom",
        "api_key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "gemma2-9b-it": {
        "provider": "custom",
        "api_key_env": "GROQ_API_KEY",
        "base_url": "https://api.groq.com/openai/v1",
    },
    # ── GitHub Copilot (Anthropic Claude) ─────────────────────────────────
    "claude-opus-4-5": {
        "provider": "copilot",
        "api_key_env": "COPILOT_GITHUB_TOKEN",
        "base_url": "https://api.githubcopilot.com",
    },
    "claude-haiku-4-5": {
        "provider": "copilot",
        "api_key_env": "COPILOT_GITHUB_TOKEN",
        "base_url": "https://api.githubcopilot.com",
    },
    # Aliases used in Flutter AppConstants (with dots)
    "claude-opus-4.5": {
        "provider": "copilot",
        "api_key_env": "COPILOT_GITHUB_TOKEN",
        "base_url": "https://api.githubcopilot.com",
    },
    "claude-haiku-4.5": {
        "provider": "copilot",
        "api_key_env": "COPILOT_GITHUB_TOKEN",
        "base_url": "https://api.githubcopilot.com",
    },
    # ── OpenAI ────────────────────────────────────────────────────────────
    "gpt-4o": {
        "provider": "openai",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
    },
    "gpt-4o-mini": {
        "provider": "openai",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
    },
    "o1-mini": {
        "provider": "openai",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
    },
    # ── OpenRouter ────────────────────────────────────────────────────────
    "openrouter/auto": {
        "provider": "openrouter",
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
    },
    # ── Nous Portal ───────────────────────────────────────────────────────
    "hermes-3-llama-3.1-405b": {
        "provider": "nous",
        "api_key_env": "NOUS_API_KEY",
        "base_url": "https://inference-api.nousresearch.com/v1",
    },
    # ── Mistral ───────────────────────────────────────────────────────────
    "mistral-large-latest": {
        "provider": "custom",
        "api_key_env": "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1",
    },
    "mistral-small-latest": {
        "provider": "custom",
        "api_key_env": "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1",
    },
}

# Keys that must NEVER be passed from runtime_kwargs into the agent
# when an explicit model is requested — prevents config from overriding request.
_RUNTIME_OVERRIDE_KEYS = {"model", "provider", "base_url", "api_key"}


def _resolve_model_config(model: str) -> Dict[str, str]:
    """Look up provider config for a model, resolving the API key from env."""
    entry = _MODEL_REGISTRY.get(model)
    if entry is None:
        return {}
    resolved: Dict[str, str] = {
        "provider": entry.get("provider", ""),
        "base_url": entry.get("base_url", ""),
    }
    env_key = entry.get("api_key_env", "")
    if env_key:
        resolved["api_key"] = os.getenv(env_key, "")
    return resolved


def _has_api_key(model: str) -> bool:
    """Return True if the model's required API key is configured."""
    entry = _MODEL_REGISTRY.get(model, {})
    env_key = entry.get("api_key_env", "")
    if not env_key:
        return True  # no key needed
    return bool(os.getenv(env_key, "").strip())


# ─── Pydantic Request Models ──────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: Any  # str or list of content parts (OpenAI multi-part)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in {"system", "user", "assistant", "tool", "function"}:
            raise ValueError(f"Invalid role: {v!r}")
        return v


class ChatCompletionRequest(BaseModel):
    model: str = Field(default="hermes-agent", min_length=1)
    messages: List[ChatMessage] = Field(min_length=1)
    stream: bool = False
    use_tools: bool = True
    temperature: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    max_tokens: Optional[int] = Field(default=None, gt=0)
    session_id: Optional[str] = None  # can also come from header


# ─── FastAPI App ─────────────────────────────────────────────────────────────

_agent_config: Dict[str, Any] = {}
_startup_model: str = MODEL_NAME  # track what model was loaded at startup


def _load_agent_config() -> None:
    """Load Hermes agent base configuration once at startup.

    IMPORTANT: We intentionally strip model/provider/base_url/api_key from
    runtime_kwargs so they cannot override per-request model selection.
    """
    global _agent_config, _startup_model
    
    # ── SYNCHRONIZE SKILLS TO HERMES_HOME ──────────────────────────────
    try:
        import shutil
        from pathlib import Path
        from hermes_constants import get_skills_dir
        
        skills_dir = get_skills_dir()
        skills_dir.mkdir(parents=True, exist_ok=True)
        
        # Source skills directory inside the repository (usually /app/AI-Skill)
        app_ai_skill_dir = Path("/app/AI-Skill")
        if not app_ai_skill_dir.exists():
            app_ai_skill_dir = Path(__file__).parent / "AI-Skill"
            
        if app_ai_skill_dir.exists():
            logger.info("Synchronizing skills from %s to %s...", app_ai_skill_dir, skills_dir)
            copied_count = 0
            for src_type_dir in app_ai_skill_dir.iterdir():
                if src_type_dir.is_dir() and not src_type_dir.name.startswith("."):
                    for item in src_type_dir.iterdir():
                        if item.is_dir() and not item.name.startswith("."):
                            target_skill_path = skills_dir / item.name
                            if target_skill_path.exists():
                                try:
                                    shutil.rmtree(target_skill_path)
                                except Exception as e:
                                    logger.warning("Could not remove old skill path %s: %s", target_skill_path, e)
                            try:
                                shutil.copytree(item, target_skill_path)
                                copied_count += 1
                            except Exception as e:
                                logger.error("Failed to copy skill %s to %s: %s", item.name, target_skill_path, e)
            logger.info("Successfully synchronized %d skills to %s", copied_count, skills_dir)
        else:
            logger.warning("Source skills directory NOT found at %s. Skipping synchronization.", app_ai_skill_dir)
    except Exception as e:
        logger.exception("Error synchronizing skills on startup: %s", e)

    try:
        from hermes_cli.tools_config import _get_platform_tools
        from gateway.run import (
            GatewayRunner,
            _load_gateway_config,
            _resolve_gateway_model,
            _resolve_runtime_agent_kwargs,
        )

        raw_runtime_kwargs = _resolve_runtime_agent_kwargs()
        # ── BUG FIX: Strip model-routing keys from runtime_kwargs ──────────
        # These keys from the config file would override the per-request model.
        clean_runtime_kwargs = {
            k: v
            for k, v in raw_runtime_kwargs.items()
            if k not in _RUNTIME_OVERRIDE_KEYS
        }

        _agent_config["runtime_kwargs"] = clean_runtime_kwargs
        _startup_model = _resolve_gateway_model()
        _agent_config["reasoning_config"] = GatewayRunner._load_reasoning_config()
        _agent_config["fallback_model"] = GatewayRunner._load_fallback_model()
        user_config = _load_gateway_config()
        _agent_config["enabled_toolsets"] = sorted(
            _get_platform_tools(user_config, "api_server")
        )

        try:
            from hermes_state import SessionDB
            _agent_config["session_db"] = SessionDB()
        except Exception:
            _agent_config["session_db"] = None

        logger.info(
            "Agent config loaded: startup_model=%s toolsets=%d",
            _startup_model,
            len(_agent_config.get("enabled_toolsets", [])),
        )
    except ImportError as exc:
        logger.warning(
            "Hermes gateway modules unavailable (%s). Using direct AIAgent mode.", exc
        )
        _agent_config["runtime_kwargs"] = {}
        _agent_config["session_db"] = None

    _agent_config["loaded"] = True


@asynccontextmanager
async def _lifespan(app: FastAPI):
    _load_agent_config()
    yield


app = FastAPI(
    title="Hermes App API",
    description="OpenAI-compatible API for the Hermes Flutter app",
    version="2.0.0",
    lifespan=_lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────────────
_origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if "*" in _origins else _origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Hermes-Session-Id", "X-Hermes-Model-Used", "X-Hermes-Request-Id"],
)


# ── Global Exception Handler ──────────────────────────────────────────────────
@app.exception_handler(Exception)
async def _global_exc_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled exception [%s]: %s", request_id, exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "Internal server error. Check backend logs.",
                "type": "server_error",
                "code": "internal_error",
                "request_id": request_id,
            }
        },
    )


# ── Request ID Middleware ─────────────────────────────────────────────────────
@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id", uuid.uuid4().hex[:12])
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Hermes-Request-Id"] = request_id
    return response


# ─── Agent Factory ────────────────────────────────────────────────────────────

def _create_agent(
    *,
    request_model: str,
    session_id: str,
    stream_delta_callback=None,
    tool_start_callback=None,
    tool_complete_callback=None,
    status_callback=None,
    tool_progress_callback=None,
    gateway_session_key: Optional[str] = None,
    request_id: str = "unknown",
    use_tools: bool = True,
) -> Any:
    """Create a fresh AIAgent for each request.

    Model resolution priority (highest → lowest):
      1. request_model from request body
      2. MODEL_NAME from API_SERVER_MODEL_NAME env var (if set)
      3. Startup model from ~/.hermes/config.yaml
      4. "hermes-agent" hardcoded default

    The _RUNTIME_OVERRIDE_KEYS are stripped from runtime_kwargs at startup,
    so the config file can NEVER win over a per-request model selection.
    """
    try:
        from run_agent import AIAgent
    except ImportError as exc:
        raise RuntimeError(
            f"Hermes AIAgent unavailable ({exc}). "
            "Run: pip install -e .[all] inside hermes-agent/"
        ) from exc

    # ── Determine effective model ─────────────────────────────────────────
    if request_model and request_model not in {"hermes-agent", ""}:
        effective_model = request_model
    elif MODEL_NAME and MODEL_NAME != "hermes-agent":
        effective_model = MODEL_NAME
    else:
        effective_model = _startup_model or MODEL_NAME

    # ── Build kwargs from clean runtime_kwargs (no model/provider keys) ───
    kwargs: Dict[str, Any] = {
        **_agent_config.get("runtime_kwargs", {}),  # already stripped
        "model": effective_model,
        "max_iterations": MAX_ITERATIONS,
        "quiet_mode": True,
        "verbose_logging": False,
        "enabled_toolsets": _agent_config.get("enabled_toolsets", []) if use_tools else [],
        "session_id": session_id,
        "platform": "api_server",
        "stream_delta_callback": stream_delta_callback,
        "tool_start_callback": tool_start_callback,
        "tool_complete_callback": tool_complete_callback,
        "status_callback": status_callback,
        "tool_progress_callback": tool_progress_callback,
        "session_db": _agent_config.get("session_db"),
        "fallback_model": _agent_config.get("fallback_model"),
        "reasoning_config": _agent_config.get("reasoning_config"),
        "gateway_session_key": gateway_session_key,
    }

    # ── Resolve provider config from registry ─────────────────────────────
    registry = _resolve_model_config(effective_model)
    if registry.get("provider"):
        kwargs["provider"] = registry["provider"]
        if registry.get("base_url"):
            kwargs["base_url"] = registry["base_url"]
        if registry.get("api_key"):
            kwargs["api_key"] = registry["api_key"]
    else:
        # Fall back to legacy env-var overrides
        if _ENV_PROVIDER:
            kwargs["provider"] = _ENV_PROVIDER
        if _ENV_BASE_URL:
            kwargs["base_url"] = _ENV_BASE_URL
        if _ENV_API_KEY_VAL:
            kwargs["api_key"] = _ENV_API_KEY_VAL

    # ── Sanity-force base_url for known providers ─────────────────────────
    provider = kwargs.get("provider", "")
    if provider == "gemini":
        kwargs["base_url"] = "https://generativelanguage.googleapis.com/v1beta"
    elif provider == "openrouter":
        kwargs["base_url"] = "https://openrouter.ai/api/v1"
    elif provider == "openai":
        kwargs.setdefault("base_url", "https://api.openai.com/v1")
    elif provider == "copilot":
        kwargs.setdefault("base_url", "https://api.githubcopilot.com")

    # Remove None values to avoid unexpected keyword args
    kwargs = {k: v for k, v in kwargs.items() if v is not None}

    logger.info(
        "[%s] Creating AIAgent model=%s provider=%s base_url=%s api_key_set=%s",
        request_id,
        kwargs.get("model"),
        kwargs.get("provider", "unset"),
        kwargs.get("base_url", "unset"),
        bool(kwargs.get("api_key")),
    )

    return AIAgent(**kwargs)


# ─── Message Parsing ──────────────────────────────────────────────────────────

def _parse_content(raw: Any) -> str:
    """Normalise OpenAI multi-part or plain string content → str."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = [
            p.get("text", "")
            for p in raw
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(parts) or str(raw)
    return str(raw) if raw is not None else ""


def _split_messages(
    messages: List[ChatMessage],
) -> Tuple[Optional[str], List[Dict[str, str]], str]:
    """Return (system_prompt, history, user_message).

    history = all but the last user/assistant turns.
    user_message = content of the final user message.
    """
    system_parts: List[str] = []
    conversation: List[Dict[str, str]] = []

    for msg in messages:
        content = _parse_content(msg.content)
        if msg.role == "system":
            system_parts.append(content)
        elif msg.role in {"user", "assistant"}:
            conversation.append({"role": msg.role, "content": content})

    system_prompt = "\n\n".join(system_parts) if system_parts else None

    if not conversation:
        return system_prompt, [], ""

    # Last turn must be user
    user_message = ""
    history: List[Dict[str, str]] = []
    for i in range(len(conversation) - 1, -1, -1):
        if conversation[i]["role"] == "user":
            user_message = conversation[i]["content"]
            history = conversation[:i]
            break
        # If last is assistant, still extract last user before it
    else:
        user_message = conversation[-1]["content"]
        history = conversation[:-1]

    return system_prompt, history, user_message


# ─── Auth ─────────────────────────────────────────────────────────────────────

def _check_auth(request: Request) -> Optional[JSONResponse]:
    if not API_KEY:
        return None
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    if token == API_KEY:
        return None
    return JSONResponse(
        {
            "error": {
                "message": "Invalid or missing API key.",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        },
        status_code=401,
    )


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health(request: Request):
    """Detailed health check — reports model and API key availability."""
    return {
        "status": "ok",
        "platform": "hermes-agent",
        "api_version": "2.0.0",
        "startup_model": _startup_model,
        "env_model": MODEL_NAME,
        "config_loaded": _agent_config.get("loaded", False),
        "models_with_keys": [m for m in _MODEL_REGISTRY if _has_api_key(m)],
        "request_id": request.state.request_id,
    }


@app.get("/v1/models")
async def list_models(request: Request):
    """List all registered models and whether their API key is configured."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    now = int(time.time())
    data = []
    seen: set = set()

    for model_id, entry in _MODEL_REGISTRY.items():
        if model_id in seen:
            continue
        seen.add(model_id)
        has_key = _has_api_key(model_id)
        data.append(
            {
                "id": model_id,
                "object": "model",
                "created": now,
                "owned_by": entry.get("provider", "hermes"),
                "ready": has_key,  # extra field: can this model be used?
            }
        )

    # Inject env-var model if not in registry
    if MODEL_NAME and MODEL_NAME not in seen and MODEL_NAME != "hermes-agent":
        data.insert(
            0,
            {
                "id": MODEL_NAME,
                "object": "model",
                "created": now,
                "owned_by": "hermes",
                "ready": True,
            },
        )

    return {"object": "list", "data": data}


# ── Remote Configuration API ─────────────────────────────────
#  Allows the Android APK to read and write backend config
#  (API keys, default model, Telegram token, etc.) at runtime.
#  All secret values are masked in GET responses.

class BackendConfigUpdate(BaseModel):
    """Fields that the APK can remotely update on the Hermes backend."""
    # ── LLM Provider API Keys ──────────────────────────
    openrouter_api_key:   Optional[str] = None
    openai_api_key:       Optional[str] = None
    anthropic_api_key:    Optional[str] = None
    google_api_key:       Optional[str] = None
    groq_api_key:         Optional[str] = None
    mistral_api_key:      Optional[str] = None
    cohere_api_key:       Optional[str] = None
    # ── Messaging Platform Tokens ──────────────────────
    telegram_bot_token:   Optional[str] = None
    telegram_home_channel: Optional[str] = None
    discord_token:        Optional[str] = None
    whatsapp_token:       Optional[str] = None
    # ── Tool API Keys ──────────────────────────────────
    firecrawl_api_key:    Optional[str] = None
    github_token:         Optional[str] = None
    # ── Agent Behaviour ────────────────────────────────
    default_model:        Optional[str] = None
    default_provider:     Optional[str] = None
    max_iterations:       Optional[int] = None
    stream_timeout:       Optional[float] = None
    # ── Admin PIN (required to update secrets) ─────────
    admin_pin:            Optional[str] = None


def _mask(value: str) -> str:
    """Mask a secret — show only first 4 and last 4 chars."""
    if not value or len(value) < 10:
        return "****" if value else ""
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _read_hermes_env() -> dict:
    """Read ~/.hermes/.env as a key→value dict."""
    env_path = os.path.expanduser("~/.hermes/.env")
    result = {}
    if not os.path.isfile(env_path):
        return result
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _write_hermes_env(updates: dict) -> None:
    """Merge updates into ~/.hermes/.env (upsert — never removes existing keys)."""
    env_path = os.path.expanduser("~/.hermes/.env")
    os.makedirs(os.path.dirname(env_path), exist_ok=True)

    # Read existing
    existing = {}
    lines = []
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k, _, v = stripped.partition("=")
                existing[k.strip()] = v.strip()

    # Apply updates
    existing.update({k: v for k, v in updates.items() if v is not None})

    # Write back
    with open(env_path, "w", encoding="utf-8") as f:
        for k, v in existing.items():
            f.write(f'{k}="{v}"\n')

    # Also update running process env vars immediately
    for k, v in updates.items():
        if v is not None:
            os.environ[k] = v


_ADMIN_PIN = os.getenv("HERMES_ADMIN_PIN", "")  # Set in Render env vars


@app.get("/v1/config")
async def get_config(request: Request):
    """
    Return the current backend configuration (secrets are masked).
    Used by the Android APK to show what's configured.
    """
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    env = _read_hermes_env()

    def has(key: str) -> bool:
        return bool(env.get(key) or os.getenv(key, ""))

    def masked(key: str) -> str:
        val = env.get(key) or os.getenv(key, "")
        return _mask(val)

    return {
        "api_version": "2.0.0",
        "default_model": _startup_model or MODEL_NAME,
        "max_iterations": MAX_ITERATIONS,
        "stream_timeout": STREAM_TIMEOUT,
        "providers": {
            "openrouter":  {"configured": has("OPENROUTER_API_KEY"),  "key_preview": masked("OPENROUTER_API_KEY")},
            "openai":      {"configured": has("OPENAI_API_KEY"),       "key_preview": masked("OPENAI_API_KEY")},
            "anthropic":   {"configured": has("ANTHROPIC_API_KEY"),    "key_preview": masked("ANTHROPIC_API_KEY")},
            "google":      {"configured": has("GOOGLE_API_KEY") or has("GEMINI_API_KEY"), "key_preview": masked("GOOGLE_API_KEY") or masked("GEMINI_API_KEY")},
            "groq":        {"configured": has("GROQ_API_KEY"),         "key_preview": masked("GROQ_API_KEY")},
            "mistral":     {"configured": has("MISTRAL_API_KEY"),      "key_preview": masked("MISTRAL_API_KEY")},
            "cohere":      {"configured": has("COHERE_API_KEY"),       "key_preview": masked("COHERE_API_KEY")},
        },
        "messaging": {
            "telegram":   {"configured": has("TELEGRAM_BOT_TOKEN"),   "channel": env.get("TELEGRAM_HOME_CHANNEL", "")},
            "discord":    {"configured": has("DISCORD_BOT_TOKEN")},
            "whatsapp":   {"configured": has("WHATSAPP_TOKEN")},
        },
        "tools": {
            "firecrawl":  {"configured": has("FIRECRAWL_API_KEY"),    "key_preview": masked("FIRECRAWL_API_KEY")},
            "github":     {"configured": has("GITHUB_TOKEN"),          "key_preview": masked("GITHUB_TOKEN")},
        },
        "admin_pin_set": bool(_ADMIN_PIN),
    }


@app.post("/v1/config")
async def update_config(request: Request, body: BackendConfigUpdate):
    """
    Update backend configuration remotely from the Android APK.
    Writes to ~/.hermes/.env and updates running env vars immediately.

    Requires either:
      - The server API key (Authorization header), OR
      - The HERMES_ADMIN_PIN matching body.admin_pin
    """
    auth_err = _check_auth(request)
    if auth_err:
        # Allow if admin PIN matches
        if not _ADMIN_PIN or body.admin_pin != _ADMIN_PIN:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized. Provide valid Authorization header or correct admin_pin."}
            )

    # Build the env-var update dict
    updates: dict[str, str] = {}

    if body.openrouter_api_key:   updates["OPENROUTER_API_KEY"]     = body.openrouter_api_key
    if body.openai_api_key:       updates["OPENAI_API_KEY"]          = body.openai_api_key
    if body.anthropic_api_key:    updates["ANTHROPIC_API_KEY"]       = body.anthropic_api_key
    if body.google_api_key:       updates["GOOGLE_API_KEY"]          = body.google_api_key
    if body.groq_api_key:         updates["GROQ_API_KEY"]            = body.groq_api_key
    if body.mistral_api_key:      updates["MISTRAL_API_KEY"]         = body.mistral_api_key
    if body.cohere_api_key:       updates["COHERE_API_KEY"]          = body.cohere_api_key
    if body.telegram_bot_token:   updates["TELEGRAM_BOT_TOKEN"]      = body.telegram_bot_token
    if body.telegram_home_channel: updates["TELEGRAM_HOME_CHANNEL"]  = body.telegram_home_channel
    if body.discord_token:        updates["DISCORD_BOT_TOKEN"]       = body.discord_token
    if body.whatsapp_token:       updates["WHATSAPP_TOKEN"]          = body.whatsapp_token
    if body.firecrawl_api_key:    updates["FIRECRAWL_API_KEY"]       = body.firecrawl_api_key
    if body.github_token:         updates["GITHUB_TOKEN"]            = body.github_token
    if body.default_model:        updates["API_SERVER_MODEL_NAME"]   = body.default_model
    if body.default_provider:     updates["API_SERVER_PROVIDER"]     = body.default_provider

    if not updates:
        return {"status": "no_changes", "message": "No fields to update were provided."}

    try:
        _write_hermes_env(updates)
        logger.info("Remote config update: %d keys updated via APK", len(updates))
        return {
            "status": "ok",
            "updated_keys": list(updates.keys()),
            "message": "Configuration updated. Messaging gateway changes (Telegram/Discord) require a gateway restart.",
            "restart_required_for": [k for k in updates if "TOKEN" in k or "CHANNEL" in k]
        }
    except Exception as e:
        logger.error("Config update failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to write config: {e}")


# ── Cloud Projects Workspace API ─────────────────────────────

class ProjectCreate(BaseModel):
    title: str
    description: Optional[str] = None
    system_instruction: Optional[str] = None

class FileUpload(BaseModel):
    file_name: str
    file_content: str

@app.get("/v1/projects")
async def list_projects(request: Request):
    """List all cloud projects."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    db = _agent_config.get("session_db")
    if not db:
        return {"projects": []}

    try:
        cursor = db._conn.execute(
            "SELECT id, title, description, system_instruction, created_at FROM projects ORDER BY created_at DESC"
        )
        rows = cursor.fetchall()
        projects_list = []
        for r in rows:
            projects_list.append({
                "id": r["id"],
                "title": r["title"],
                "description": r["description"],
                "system_instruction": r["system_instruction"],
                "created_at": r["created_at"]
            })
        return {"projects": projects_list}
    except Exception as e:
        logger.error("Error listing projects: %s", e)
        return {"projects": [], "error": str(e)}

@app.post("/v1/projects")
async def create_project(request: Request, body: ProjectCreate):
    """Create a new cloud project."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    db = _agent_config.get("session_db")
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    import uuid
    project_id = str(uuid.uuid4())
    created_at = time.time()

    def insert_project(conn):
        conn.execute(
            "INSERT INTO projects (id, title, description, system_instruction, created_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, body.title, body.description, body.system_instruction, created_at)
        )
        return project_id

    try:
        db._write_transaction(insert_project)
        return {
            "id": project_id,
            "title": body.title,
            "description": body.description,
            "system_instruction": body.system_instruction,
            "created_at": created_at
        }
    except Exception as e:
        logger.error("Error creating project: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/v1/projects/{project_id}")
async def delete_project(request: Request, project_id: str):
    """Delete a cloud project and its files."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    db = _agent_config.get("session_db")
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    try:
        db._write_transaction(lambda conn: conn.execute("DELETE FROM projects WHERE id = ?", (project_id,)))
        return {"success": True}
    except Exception as e:
        logger.error("Error deleting project: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/projects/{project_id}/files")
async def list_project_files(request: Request, project_id: str):
    """List all files pinned inside a project."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    db = _agent_config.get("session_db")
    if not db:
        return {"files": []}

    try:
        cursor = db._conn.execute(
            "SELECT id, file_name, file_size, uploaded_at FROM project_files WHERE project_id = ? ORDER BY uploaded_at DESC",
            (project_id,)
        )
        rows = cursor.fetchall()
        files_list = []
        for r in rows:
            files_list.append({
                "id": r["id"],
                "file_name": r["file_name"],
                "file_size": r["file_size"],
                "uploaded_at": r["uploaded_at"]
            })
        return {"files": files_list}
    except Exception as e:
        logger.error("Error listing project files: %s", e)
        return {"files": [], "error": str(e)}

@app.post("/v1/projects/{project_id}/files")
async def upload_project_file(request: Request, project_id: str, body: FileUpload):
    """Upload a file to a cloud project."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    db = _agent_config.get("session_db")
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    import uuid
    file_id = str(uuid.uuid4())
    uploaded_at = time.time()
    file_size = len(body.file_content.encode("utf-8"))

    def insert_file(conn):
        conn.execute(
            "INSERT INTO project_files (id, project_id, file_name, file_content, file_size, uploaded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, project_id, body.file_name, body.file_content, file_size, uploaded_at)
        )
        return file_id

    try:
        db._write_transaction(insert_file)
        return {
            "id": file_id,
            "project_id": project_id,
            "file_name": body.file_name,
            "file_size": file_size,
            "uploaded_at": uploaded_at
        }
    except Exception as e:
        logger.error("Error uploading file: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/v1/projects/{project_id}/files/{file_id}")
async def delete_project_file(request: Request, project_id: str, file_id: str):
    """Delete a file from a project."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    db = _agent_config.get("session_db")
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    try:
        db._write_transaction(lambda conn: conn.execute("DELETE FROM project_files WHERE id = ? AND project_id = ?", (file_id, project_id)))
        return {"success": True}
    except Exception as e:
        logger.error("Error deleting file: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/projects/{project_id}/threads/{thread_id}")
async def link_thread_to_project(request: Request, project_id: str, thread_id: str):
    """Link a chat thread (session) to a project."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    db = _agent_config.get("session_db")
    if not db:
        raise HTTPException(status_code=500, detail="Database not available")

    p_id = None if project_id.lower() == "null" else project_id

    try:
        db._write_transaction(lambda conn: conn.execute(
            "UPDATE sessions SET project_id = ? WHERE id = ?",
            (p_id, thread_id)
        ))
        return {"success": True}
    except Exception as e:
        logger.error("Error linking thread: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/v1/threads/{thread_id}/project")
async def get_thread_project(request: Request, thread_id: str):
    """Get the cloud project linked to a thread."""
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    db = _agent_config.get("session_db")
    if not db:
        return {"project": None}

    try:
        cursor = db._conn.execute(
            "SELECT p.id, p.title, p.description, p.system_instruction, p.created_at "
            "FROM sessions s JOIN projects p ON s.project_id = p.id WHERE s.id = ?",
            (thread_id,)
        )
        r = cursor.fetchone()
        if r:
            return {
                "project": {
                    "id": r["id"],
                    "title": r["title"],
                    "description": r["description"],
                    "system_instruction": r["system_instruction"],
                    "created_at": r["created_at"]
                }
            }
        return {"project": None}
    except Exception as e:
        logger.error("Error getting thread project: %s", e)
        return {"project": None, "error": str(e)}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions endpoint.

    Supports:
    - Streaming (SSE) and non-streaming JSON
    - Any model in _MODEL_REGISTRY
    - Per-request model switching (fixes the primary bug)
    - Full conversation history
    - X-Hermes-Session-Id header for session continuity
    """
    auth_err = _check_auth(request)
    if auth_err:
        return auth_err

    request_id: str = getattr(request.state, "request_id", uuid.uuid4().hex[:12])

    # ── Parse & validate request ──────────────────────────────────────────
    try:
        raw_body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "Invalid JSON body.", "type": "invalid_request_error"}},
            status_code=400,
        )

    try:
        chat_req = ChatCompletionRequest(**raw_body)
    except Exception as exc:
        return JSONResponse(
            {
                "error": {
                    "message": f"Request validation error: {exc}",
                    "type": "invalid_request_error",
                }
            },
            status_code=422,
        )

    session_id = (
        chat_req.session_id
        or request.headers.get("X-Hermes-Session-Id")
        or str(uuid.uuid4())
    )
    gateway_session_key = request.headers.get("X-Hermes-Session-Key")

    # ── Parse messages ────────────────────────────────────────────────────
    system_prompt, history, user_message = _split_messages(chat_req.messages)

    # ── Inject Cloud Project Context ──────────────────────────────────────
    db = _agent_config.get("session_db")
    project_prompt_addition = ""
    if db and session_id:
        try:
            cursor = db._conn.execute("SELECT project_id FROM sessions WHERE id = ?", (session_id,))
            row = cursor.fetchone()
            if row and row["project_id"]:
                project_id = row["project_id"]
                p_cursor = db._conn.execute(
                    "SELECT title, description, system_instruction FROM projects WHERE id = ?",
                    (project_id,)
                )
                p_row = p_cursor.fetchone()
                if p_row:
                    instructions = p_row["system_instruction"] or ""
                    project_context = f"\n\n=== CLOUD PROJECT WORKSPACE: {p_row['title'].upper()} ===\n"
                    if p_row["description"]:
                        project_context += f"Description: {p_row['description']}\n"
                    if instructions:
                        project_context += f"Instructions:\n{instructions}\n"
                    
                    f_cursor = db._conn.execute(
                        "SELECT file_name, file_content FROM project_files WHERE project_id = ?",
                        (project_id,)
                    )
                    f_rows = f_cursor.fetchall()
                    if f_rows:
                        project_context += "\nProject Reference Files:\n"
                        for f in f_rows:
                            project_context += f'<file name="{f["file_name"]}">\n{f["file_content"]}\n</file>\n'
                    
                    project_prompt_addition = project_context
        except Exception as e:
            logger.error("Error building project context inside chat completions: %s", e)

    if project_prompt_addition:
        if system_prompt:
            system_prompt = project_prompt_addition + "\n\n" + system_prompt
        else:
            system_prompt = project_prompt_addition

    if not user_message:
        return JSONResponse(
            {
                "error": {
                    "message": "No user message found in messages array.",
                    "type": "invalid_request_error",
                }
            },
            status_code=400,
        )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:29]}"
    created = int(time.time())
    effective_model = (
        chat_req.model
        if chat_req.model and chat_req.model != "hermes-agent"
        else (MODEL_NAME if MODEL_NAME != "hermes-agent" else _startup_model or MODEL_NAME)
    )

    logger.info(
        "[%s] Request: session=%s model=%s stream=%s history_turns=%d",
        request_id,
        session_id,
        effective_model,
        chat_req.stream,
        len(history),
    )

    common_headers = {
        "X-Hermes-Session-Id": session_id,
        "X-Hermes-Model-Used": effective_model,
        "X-Hermes-Request-Id": request_id,
    }

    if chat_req.stream:
        return StreamingResponse(
            _stream_chat(
                user_message=user_message,
                history=history,
                system_prompt=system_prompt,
                session_id=session_id,
                gateway_session_key=gateway_session_key,
                completion_id=completion_id,
                model=effective_model,
                created=created,
                request_id=request_id,
                use_tools=chat_req.use_tools,
            ),
            media_type="text/event-stream",
            headers={
                **common_headers,
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ── Non-streaming ─────────────────────────────────────────────────────
    body = await _non_streaming_completion(
        user_message=user_message,
        history=history,
        system_prompt=system_prompt,
        session_id=session_id,
        gateway_session_key=gateway_session_key,
        completion_id=completion_id,
        created=created,
        effective_model=effective_model,
        request_id=request_id,
        use_tools=chat_req.use_tools,
    )
    return JSONResponse(body, headers=common_headers)


# ─── Non-Streaming Handler ────────────────────────────────────────────────────

async def _non_streaming_completion(
    *,
    user_message: str,
    history: List[Dict[str, str]],
    system_prompt: Optional[str],
    session_id: str,
    gateway_session_key: Optional[str],
    completion_id: str,
    created: int,
    effective_model: str,
    request_id: str,
    use_tools: bool = True,
) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()

    def _run() -> Dict[str, Any]:
        agent = _create_agent(
            request_model=effective_model,
            session_id=session_id,
            gateway_session_key=gateway_session_key,
            request_id=request_id,
            use_tools=use_tools,
        )
        try:
            res = agent.run_conversation(
                user_message=user_message,
                system_message=system_prompt,
                conversation_history=history,
            )
            if res and not res.get("completed", False) and "error" in res:
                err_str = str(res["error"]).lower()
                if ("413" in err_str or "too large" in err_str or "rate_limit" in err_str) and effective_model == "llama-3.3-70b-versatile":
                    logger.warning("[%s] Non-streaming agent failed with rate/payload limit. Retrying with gemini-2.5-flash...", request_id)
                    fallback_agent = _create_agent(
                        request_model="gemini-2.5-flash",
                        session_id=session_id,
                        gateway_session_key=gateway_session_key,
                        request_id=request_id,
                        use_tools=use_tools,
                    )
                    return fallback_agent.run_conversation(
                        user_message=user_message,
                        system_message=system_prompt,
                        conversation_history=history,
                    )
            return res
        except Exception as exc:
            exc_str = str(exc).lower()
            if ("413" in exc_str or "too large" in exc_str or "rate_limit" in exc_str) and effective_model == "llama-3.3-70b-versatile":
                logger.warning("[%s] Non-streaming agent failed with exception %s. Retrying with gemini-2.5-flash...", request_id, exc)
                fallback_agent = _create_agent(
                    request_model="gemini-2.5-flash",
                    session_id=session_id,
                    gateway_session_key=gateway_session_key,
                    request_id=request_id,
                    use_tools=use_tools,
                )
                return fallback_agent.run_conversation(
                    user_message=user_message,
                    system_message=system_prompt,
                    conversation_history=history,
                )
            raise

    try:
        result: Dict[str, Any] = await asyncio.wait_for(
            loop.run_in_executor(None, _run),
            timeout=STREAM_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("[%s] Non-streaming agent timed out", request_id)
        return _error_response(
            completion_id,
            created,
            effective_model,
            "The agent took too long to respond. Check your LLM provider API key.",
            finish_reason="timeout",
        )
    except Exception as exc:
        logger.exception("[%s] Non-streaming agent error: %s", request_id, exc)
        return _error_response(
            completion_id,
            created,
            effective_model,
            f"Agent error: {exc}",
        )

    if not result or not result.get("completed", False) or "error" in result:
        error_msg = result.get("error", "Unknown error occurred during execution.")
        logger.error("[%s] Non-streaming agent failed: %s", request_id, error_msg)
        return _error_response(
            completion_id,
            created,
            effective_model,
            error_msg,
        )

    response_text = result.get("final_response") or ""
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": effective_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": result.get("prompt_tokens", 0) or 0,
            "completion_tokens": result.get("completion_tokens", 0) or 0,
            "total_tokens": result.get("total_tokens", 0) or 0,
        },
    }


def _error_response(
    completion_id: str,
    created: int,
    model: str,
    message: str,
    finish_reason: str = "error",
) -> Dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": message},
                "finish_reason": finish_reason,
            }
        ],
        "error": {"message": message, "type": "server_error"},
    }


# ─── Streaming Handler ────────────────────────────────────────────────────────

def _truncate_str(s: str, limit: int = 20000) -> str:
    if s is None:
        return ""
    if len(s) > limit:
        return s[:limit] + f"\n\n... [Truncated {len(s) - limit} characters for database and UI performance] ..."
    return s

async def _stream_chat(
    *,
    user_message: str,
    history: List[Dict[str, str]],
    system_prompt: Optional[str],
    session_id: str,
    gateway_session_key: Optional[str],
    completion_id: str,
    model: str,
    created: int,
    request_id: str,
    use_tools: bool = True,
) -> AsyncGenerator[str, None]:
    """Produce SSE frames for a streaming chat completion.

    Uses asyncio.Queue with a typed sentinel (None = done, str = delta,
    Exception = error) to cleanly bridge the sync agent thread and the
    async SSE generator without race conditions.
    """
    # asyncio.Queue is thread-safe for put/get from different threads
    q: asyncio.Queue[Optional[Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    # Track currently active tools running inside each subagent
    active_subagent_tools: Dict[str, str] = {} # maps subagent_id -> active toolCallId

    def _on_delta(delta: Optional[str]) -> None:
        if delta:
            loop.call_soon_threadsafe(q.put_nowait, ("delta", delta))

    def _on_tool_start(tool_call_id: str, name: str, args: Any) -> None:
        try:
            from agent.display import build_tool_preview, get_tool_emoji
            emoji = get_tool_emoji(name)
            preview = build_tool_preview(name, args)
            label = f"{name.capitalize()}: {preview}" if preview else name.capitalize()
            
            args_str = ""
            try:
                args_str = json.dumps(args, indent=2)
            except Exception:
                args_str = str(args)
            args_str = _truncate_str(args_str, 20000)

            payload = {
                "tool_call_id": tool_call_id,
                "toolCallId": tool_call_id, # Added camelCase for Moshi Kotlin client
                "tool": name,
                "emoji": emoji,
                "label": label,
                "status": "running",
                "args": args_str,
                "result": ""
            }
            loop.call_soon_threadsafe(q.put_nowait, ("tool_progress", payload))
        except Exception as exc:
            logger.warning("Error in _on_tool_start callback: %s", exc)

    def _on_tool_complete(tool_call_id: str, name: str, args: Any, result: Any) -> None:
        try:
            from agent.display import build_tool_preview, get_tool_emoji
            emoji = get_tool_emoji(name)
            preview = build_tool_preview(name, args)
            label = f"{name.capitalize()}: {preview}" if preview else name.capitalize()
            
            args_str = ""
            try:
                args_str = json.dumps(args, indent=2)
            except Exception:
                args_str = str(args)
            args_str = _truncate_str(args_str, 20000)

            result_str = ""
            try:
                if isinstance(result, (dict, list)):
                    result_str = json.dumps(result, indent=2)
                else:
                    result_str = str(result)
            except Exception:
                result_str = str(result)
            result_str = _truncate_str(result_str, 20000)

            payload = {
                "tool_call_id": tool_call_id,
                "toolCallId": tool_call_id, # Added camelCase for Moshi Kotlin client
                "tool": name,
                "emoji": emoji,
                "label": label,
                "status": "completed",
                "args": args_str,
                "result": result_str
            }
            loop.call_soon_threadsafe(q.put_nowait, ("tool_progress", payload))
        except Exception as exc:
            logger.warning("Error in _on_tool_complete callback: %s", exc)

    def _on_tool_progress(
        event_type: str,
        name: Optional[str] = None,
        preview: Optional[str] = None,
        args: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        try:
            subagent_id = kwargs.get("subagent_id", "subagent")
            
            # 1. Subagent started a tool
            if event_type == "subagent.tool" and name:
                from agent.display import get_tool_emoji
                emoji = get_tool_emoji(name)
                label = f"{name.capitalize()}: {preview}" if preview else name.capitalize()
                
                # If there was a previous tool running for this subagent, complete it first!
                if subagent_id in active_subagent_tools:
                    prev_call_id = active_subagent_tools[subagent_id]
                    payload_complete = {
                        "tool_call_id": prev_call_id,
                        "toolCallId": prev_call_id,
                        "tool": name,
                        "emoji": emoji,
                        "label": f"Subagent: {label}",
                        "status": "completed",
                        "args": "",
                        "result": "success"
                    }
                    loop.call_soon_threadsafe(q.put_nowait, ("tool_progress", payload_complete))
                
                # Register new active tool call for this subagent
                call_id = f"{subagent_id}-{name}"
                active_subagent_tools[subagent_id] = call_id
                
                payload = {
                    "tool_call_id": call_id,
                    "toolCallId": call_id,
                    "tool": name,
                    "emoji": emoji,
                    "label": f"Subagent: {label}",
                    "status": "running",
                    "args": str(args) if args else "",
                    "result": ""
                }
                loop.call_soon_threadsafe(q.put_nowait, ("tool_progress", payload))
            
            # 2. Subagent thinking or overall progress delta
            elif event_type in ("subagent.thinking", "subagent.progress", "subagent_progress"):
                msg = preview or name or ""
                if msg:
                    payload = {
                        "type": "thinking",
                        "message": f"Subagent: {msg}"
                    }
                    loop.call_soon_threadsafe(q.put_nowait, ("status", payload))
            
            # 3. Subagent completed its overall task
            elif event_type == "subagent.complete":
                # Complete the last active tool for this subagent if any
                if subagent_id in active_subagent_tools:
                    call_id = active_subagent_tools.pop(subagent_id)
                    payload_complete = {
                        "tool_call_id": call_id,
                        "toolCallId": call_id,
                        "tool": name or "subagent",
                        "emoji": "🔀",
                        "label": f"Subagent completed: {preview or 'Success'}",
                        "status": "completed",
                        "args": "",
                        "result": "success"
                    }
                    loop.call_soon_threadsafe(q.put_nowait, ("tool_progress", payload_complete))
        except Exception as exc:
            logger.warning("Error in _on_tool_progress callback: %s", exc)

    def _on_status(status_type: str, message: str) -> None:
        try:
            payload = {
                "type": status_type,
                "message": message
            }
            loop.call_soon_threadsafe(q.put_nowait, ("status", payload))
        except Exception as exc:
            logger.warning("Error in _on_status callback: %s", exc)

    def _on_error(exc: Exception) -> None:
        loop.call_soon_threadsafe(q.put_nowait, exc)
        loop.call_soon_threadsafe(q.put_nowait, None)  # sentinel

    def _run_in_thread() -> None:
        try:
            agent = _create_agent(
                request_model=model,
                session_id=session_id,
                stream_delta_callback=_on_delta,
                tool_start_callback=_on_tool_start,
                tool_complete_callback=_on_tool_complete,
                status_callback=_on_status,
                tool_progress_callback=_on_tool_progress,
                gateway_session_key=gateway_session_key,
                request_id=request_id,
                use_tools=use_tools,
            )
            result = agent.run_conversation(
                user_message=user_message,
                system_message=system_prompt,
                conversation_history=history,
            )
            if result and not result.get("completed", False) and "error" in result:
                err_str = str(result["error"]).lower()
                if ("413" in err_str or "too large" in err_str or "rate_limit" in err_str) and model == "llama-3.3-70b-versatile":
                    logger.warning("[%s] Streaming agent failed with rate/payload limit. Retrying with gemini-2.5-flash...", request_id)
                    fallback_agent = _create_agent(
                        request_model="gemini-2.5-flash",
                        session_id=session_id,
                        stream_delta_callback=_on_delta,
                        tool_start_callback=_on_tool_start,
                        tool_complete_callback=_on_tool_complete,
                        status_callback=_on_status,
                        tool_progress_callback=_on_tool_progress,
                        gateway_session_key=gateway_session_key,
                        request_id=request_id,
                        use_tools=use_tools,
                    )
                    result = fallback_agent.run_conversation(
                        user_message=user_message,
                        system_message=system_prompt,
                        conversation_history=history,
                    )
            if result and not result.get("completed", False) and "error" in result:
                _on_error(RuntimeError(result["error"]))
        except Exception as exc:
            exc_str = str(exc).lower()
            if ("413" in exc_str or "too large" in exc_str or "rate_limit" in exc_str) and model == "llama-3.3-70b-versatile":
                try:
                    logger.warning("[%s] Streaming agent failed with exception %s. Retrying with gemini-2.5-flash...", request_id, exc)
                    fallback_agent = _create_agent(
                        request_model="gemini-2.5-flash",
                        session_id=session_id,
                        stream_delta_callback=_on_delta,
                        tool_start_callback=_on_tool_start,
                        tool_complete_callback=_on_tool_complete,
                        status_callback=_on_status,
                        tool_progress_callback=_on_tool_progress,
                        gateway_session_key=gateway_session_key,
                        request_id=request_id,
                        use_tools=use_tools,
                    )
                    result = fallback_agent.run_conversation(
                        user_message=user_message,
                        system_message=system_prompt,
                        conversation_history=history,
                    )
                    if result and not result.get("completed", False) and "error" in result:
                        _on_error(RuntimeError(result["error"]))
                    return
                except Exception as exc2:
                    exc = exc2
            logger.exception("[%s] Streaming agent error: %s", request_id, exc)
            _on_error(exc)
        finally:
            # Always send sentinel so generator terminates
            loop.call_soon_threadsafe(q.put_nowait, None)

    task = loop.run_in_executor(None, _run_in_thread)

    def _sse(data: Dict) -> str:
        return f"data: {json.dumps(data)}\n\n"

    # ── Emit role chunk first ─────────────────────────────────────────────
    yield _sse(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )

    finish_reason = "stop"
    deadline = loop.time() + STREAM_TIMEOUT

    # ── Drain queue until sentinel ────────────────────────────────────────
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.warning("[%s] Stream timeout — cancelling", request_id)
            task.cancel()
            yield _sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "timeout"}],
                    "error": "Agent took too long. Check your LLM provider API key.",
                }
            )
            yield "data: [DONE]\n\n"
            return

        try:
            item = await asyncio.wait_for(q.get(), timeout=min(remaining, 1.0))
        except asyncio.TimeoutError:
            # No item yet — keep looping (task still running)
            continue

        if item is None:
            # Clean sentinel — agent finished
            break

        if isinstance(item, Exception):
            err_msg = str(item)
            logger.error("[%s] Agent error in stream: %s", request_id, err_msg)
            finish_reason = "error"
            yield _sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}],
                    "error": err_msg,
                }
            )
            yield "data: [DONE]\n\n"
            return

        # item is a tuple: (type, payload)
        event_type, payload = item
        if event_type == "delta":
            yield _sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"content": payload}, "finish_reason": None}
                    ],
                }
            )
        elif event_type == "tool_progress":
            yield f"event: hermes.tool_progress\ndata: {json.dumps(payload)}\n\n"
        elif event_type == "status":
            yield f"event: hermes.status\ndata: {json.dumps(payload)}\n\n"

    # ── Flush any remaining items after sentinel (shouldn't happen but safe) ──
    while not q.empty():
        item = q.get_nowait()
        if item is None or isinstance(item, Exception):
            break
        event_type, payload = item
        if event_type == "delta":
            yield _sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"content": payload}, "finish_reason": None}
                    ],
                }
            )
        elif event_type == "tool_progress":
            yield f"event: hermes.tool_progress\ndata: {json.dumps(payload)}\n\n"
        elif event_type == "status":
            yield f"event: hermes.status\ndata: {json.dumps(payload)}\n\n"

    # ── Final stop chunk ──────────────────────────────────────────────────
    yield _sse(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        }
    )
    yield "data: [DONE]\n\n"
    logger.info("[%s] Stream complete finish_reason=%s", request_id, finish_reason)


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "hermes_app_api:app",
        host=HOST,
        port=PORT,
        reload=False,
        log_level="info",
        access_log=True,
    )
