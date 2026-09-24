"""
OpenCode Space — Automatic Credentials Synchronizer
===================================================
Automatically captures API keys and credentials entered in the OmniRoute Web UI
and persists them into:
  1. Persistent Disk Vault (/data/omniroute/credentials_vault.json)
  2. Hugging Face Space Secrets API (if HF_TOKEN with write permission is present)
  3. Active Process Environment (os.environ)
"""

import os
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger("CredentialsSync")

VAULT_PATH = "/data/omniroute/credentials_vault.json"
SPACE_ID = os.environ.get("SPACE_ID", "Jishnupg/Opencode-Cli")

PROVIDER_ENV_MAP = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "google": "GEMINI_API_KEY",
    "antigravity": "ANTIGRAVITY_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
    "nvidia-nim": "NVIDIA_API_KEY",
    "huggingface": "HF_TOKEN",
    "hf": "HF_TOKEN",
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "kimi-coding": "MOONSHOT_API_KEY",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "xai": "XAI_API_KEY",
    "grok": "XAI_API_KEY",
    "github": "GITHUB_TOKEN",
}

def normalize_provider_name(name: str) -> str:
    if not name:
        return ""
    clean = name.lower().strip()
    for k, v in PROVIDER_ENV_MAP.items():
        if k in clean:
            return v
    # Fallback: create a sanitized env var name
    sanitized = "".join(c if c.isalnum() else "_" for c in name.upper())
    return f"{sanitized}_API_KEY"

def save_to_local_vault(env_name: str, secret_val: str):
    """Saves key-value pairs into /data/omniroute/credentials_vault.json."""
    if not env_name or not secret_val:
        return
    try:
        os.makedirs(os.path.dirname(VAULT_PATH), exist_ok=True)
        vault = {}
        if os.path.exists(VAULT_PATH):
            try:
                with open(VAULT_PATH, "r", encoding="utf-8") as f:
                    vault = json.load(f)
            except Exception:
                vault = {}
        
        vault[env_name] = secret_val
        with open(VAULT_PATH, "w", encoding="utf-8") as f:
            json.dump(vault, f, indent=2)
        
        # Inject into active process environment
        os.environ[env_name] = secret_val
        logger.info(f"[VAULT] Saved {env_name} to {VAULT_PATH} & active os.environ")
    except Exception as e:
        logger.warning(f"[VAULT] Failed to save {env_name} to local vault: {e}")

def sync_secret_to_huggingface(secret_name: str, secret_val: str):
    """Syncs a secret to Hugging Face Space Secrets via REST API."""
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY")
    if not token or not secret_name or not secret_val:
        return False

    url = f"https://huggingface.co/api/spaces/{SPACE_ID}/secrets"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "OpenCode-Space-Vault/1.0"
    }
    payload = {
        "key": secret_name,
        "value": secret_val,
        "description": "Auto-synced from OmniRoute Web UI"
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                logger.info(f"[HF_SYNC] Successfully uploaded secret {secret_name} to Hugging Face Space Secrets!")
                return True
    except urllib.error.HTTPError as e:
        # If already exists, try PUT or ignore
        if e.code in (400, 409):
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="PUT")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    logger.info(f"[HF_SYNC] Updated secret {secret_name} in Hugging Face Space Secrets.")
                    return True
            except Exception:
                pass
        logger.warning(f"[HF_SYNC] HF API returned status {e.code} while syncing {secret_name}: {e.read().decode('utf-8', 'ignore')}")
    except Exception as e:
        logger.warning(f"[HF_SYNC] Failed to sync {secret_name} to HF Space: {e}")
    return False

def handle_captured_credential(provider_hint: str, api_key: str):
    """High-level dispatch to save locally and attempt HF cloud sync."""
    if not api_key:
        return
    api_key_str = str(api_key).strip()
    if len(api_key_str) < 5:
        return
    
    # Check if this is a structured JSON string (like Antigravity OAuth object)
    if api_key_str.startswith("{") and api_key_str.endswith("}"):
        try:
            parsed = json.loads(api_key_str)
            if isinstance(parsed, dict):
                # Extract refresh token or access token if available
                rt = parsed.get("refresh_token") or parsed.get("token") or parsed.get("access_token")
                if rt and isinstance(rt, str):
                    env_name = normalize_provider_name(provider_hint)
                    save_to_local_vault(env_name, rt)
                    sync_secret_to_huggingface(env_name, rt)
        except Exception:
            pass

    env_name = normalize_provider_name(provider_hint)
    save_to_local_vault(env_name, api_key_str)
    sync_secret_to_huggingface(env_name, api_key_str)

def flush_omniroute_db():
    """Flushes SQLite WAL buffer and atomically syncs runtime db to persistent storage."""
    import sqlite3
    src = "/root/.omniroute/storage.sqlite"
    dst = "/data/omniroute/storage.sqlite"
    if not os.path.exists(src) or os.path.getsize(src) == 0:
        return
    try:
        os.makedirs("/data/omniroute", exist_ok=True)
        conn = sqlite3.connect(src)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.commit()
        conn.close()

        tmp_dst = f"{dst}.tmp"
        src_conn = sqlite3.connect(src)
        dst_conn = sqlite3.connect(tmp_dst)
        src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()

        if os.path.exists(tmp_dst) and os.path.getsize(tmp_dst) > 0:
            os.replace(tmp_dst, dst)
            logger.info(f"[PERSISTENCE] Flushed {os.path.getsize(dst)} bytes to persistent volume ({dst})")

        sync_sqlite_credentials_to_vault(src)
    except Exception as e:
        logger.warning(f"[PERSISTENCE] flush_omniroute_db error: {e}")

def sync_sqlite_credentials_to_vault(db_path="/root/.omniroute/storage.sqlite"):
    """Scans OmniRoute SQLite database for any newly configured providers and syncs them."""
    import sqlite3
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [t[0] for t in cursor.fetchall()]
        
        # Check connections and provider tables
        for tbl in ["connections", "provider_connections", "keys", "providers"]:
            if tbl in tables:
                cursor.execute(f"SELECT * FROM {tbl}")
                rows = cursor.fetchall()
                col_names = [d[0].lower() for d in cursor.description]
                for r in rows:
                    row_dict = dict(zip(col_names, r))
                    provider = str(row_dict.get("provider_id") or row_dict.get("provider") or row_dict.get("name") or row_dict.get("id") or "")
                    key_val = str(row_dict.get("api_key") or row_dict.get("key") or row_dict.get("token") or row_dict.get("credentials") or "")
                    if provider and key_val and len(key_val) > 5 and not key_val.startswith("enc:"):
                        handle_captured_credential(provider, key_val)
        conn.close()
    except Exception as e:
        logger.debug(f"[VAULT] SQLite scan skipped: {e}")

