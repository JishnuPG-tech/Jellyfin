"""Central configuration for the apex_stream package.

Reads from environment variables with conservative defaults sized for the
~2 CPU / small-memory HF free tier. Every knob here has a single source of
truth so the gateway and the streamer read identical values.
"""

import os


def _int(name: str, default: int, env=None) -> int:
    raw = (env or os.environ).get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool, env=None) -> bool:
    raw = (env or os.environ).get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str_or_list(name: str, default, env=None) -> list[str]:
    raw = (env or os.environ).get(name)
    if raw is None or raw.strip() == "":
        return default
    return [t.strip() for t in raw.split(",") if t.strip()]


class Config:
    def __init__(self, environ: dict | None = None):
        env = environ if environ is not None else os.environ

        # Chunk / run geometry
        self.chunk_size = _int("APEX_STREAM_CHUNK_SIZE", 1024 * 1024, env)
        self.run_size = _int("APEX_STREAM_RUN_CHUNKS", 16, env)
        self.run_window = _int("APEX_STREAM_RUN_WINDOW", 4, env)

        # Concurrency ceilings (per process)
        self.max_global_streams = _int("APEX_STREAM_MAX_GLOBAL", 32, env)
        self.max_per_client = _int("APEX_STREAM_MAX_PER_CLIENT", 12, env)
        self.max_per_source = _int("APEX_STREAM_MAX_PER_SOURCE", 8, env)
        self.max_inflight_runs = _int("APEX_STREAM_MAX_INFLIGHT", 48, env)

        # Bounded hot-chunk cache
        self.cache_enabled = _bool("APEX_STREAM_CACHE_ENABLED", True, env)
        self.cache_size_bytes = _int("APEX_STREAM_CACHE_MB", 64, env) * 1024 * 1024
        self.cache_ttl_seconds = _int("APEX_STREAM_CACHE_TTL_SECONDS", 30, env)

        # Telegram client pool
        self.extra_tokens = _str_or_list("APEX_TELEGRAM_EXTRA_TOKENS", [], env)
        self.client_cooldown_seconds = _int("APEX_TELEGRAM_CLIENT_COOLDOWN", 30, env)
        self.client_failure_threshold = _int("APEX_TELEGRAM_FAILURE_THRESHOLD", 3, env)
        self.max_telegram_retries = _int("APEX_TELEGRAM_MAX_RETRIES", 2, env)
        self.telegram_max_concurrent = _int("APEX_TELEGRAM_MAX_CONCURRENT", 4, env)

        # Tuning / response behaviour
        self.seek_reads_use_cache = _bool("APEX_STREAM_SEEK_USE_CACHE", True, env)
        self.cancel_via_client_disconnect = _bool(
            "APEX_STREAM_CANCEL_ON_DISCONNECT", True, env
        )

    def cache_enabled_effective(self) -> bool:
        return self.cache_enabled and self.cache_size_bytes > 0

    def to_dict(self) -> dict:
        return {
            "chunk_size": self.chunk_size,
            "run_size": self.run_size,
            "run_window": self.run_window,
            "max_global_streams": self.max_global_streams,
            "max_per_client": self.max_per_client,
            "max_per_source": self.max_per_source,
            "max_inflight_runs": self.max_inflight_runs,
            "cache_enabled": self.cache_enabled,
            "cache_size_bytes": self.cache_size_bytes,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "extra_tokens": self.extra_tokens,
            "client_cooldown_seconds": self.client_cooldown_seconds,
            "client_failure_threshold": self.client_failure_threshold,
            "max_telegram_retries": self.max_telegram_retries,
            "telegram_max_concurrent": self.telegram_max_concurrent,
        }