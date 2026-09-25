---
title: Apex Media Platform
emoji: 🎬
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 🚀 Apex Media Platform

A high-performance media server hosted on **Hugging Face Spaces** with persistent storage support.

Apex combines an official **Jellyfin** media server with a **Telegram MTProto streaming gateway** (`Pyrogram`) that streams personal media on demand directly from Telegram, without permanently writing full video files to disk.

---

## 📦 Hosted Services & Endpoints

| Application | Path | Internal Port | Description |
| :--- | :--- | :--- | :--- |
| **Jellyfin Media Server** | `/jellyfin/` | `8096` | Official Jellyfin interface (Direct Play media, Continue Watching, Movies & Shows). |
| **Apex Gateway** | `/` | `7860` | Nginx + FastAPI proxy frontend (this dashboard). |
| **Gateway Health** | `/health` | `8000` | Health status and upstream diagnostics. |
| **Telegram Streamer** | `/tg-stream/` | `8080` | MTProto streamer (range streaming, webhook ingestion, health). |
| **Ops / Data API** | `/apex/ops/*` | `8000` | Admin-gated access to the `/data` volume (tree view, Jellyfin reset/init/scan). |

> **Ops API note:** every `/apex/ops/*` route requires an
> `Authorization: Bearer <key>` header where `<key>` matches the space's
> `APEX_OPS_KEY` (falling back to `APEX_SECRET_KEY` or `HF_TOKEN`). This is how
> the software storage volume can be inspected and re-initialized from outside
> the container.

---

## ⚙️ Configuration & Environment Secrets

Configure these in **HF Space Settings → Variables and secrets**:

| Variable | Description |
| :--- | :--- |
| `TELEGRAM_API_ID` | Telegram API application ID |
| `TELEGRAM_API_HASH` | Telegram API application hash |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot token for MTProto authentication |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated list of allowed source channel/group chat IDs |
| `TMDB_API_KEY` | The Movie Database v3 API key for automated metadata & poster fetching |
| `APEX_SECRET_KEY` | Secret key protecting the `/apex/ops/*` admin endpoints |
| `APEX_OPS_KEY` | Optional dedicated key for `/apex/ops/*` (falls back to `APEX_SECRET_KEY` / `HF_TOKEN`) |
| `APEX_WEBHOOK_SECRET` | Optional secret token required by Telegram webhook posts |

> Legacy `TG_API_ID` / `TG_API_HASH` / `TG_BOT_TOKEN` / `TG_CHANNEL_ID` variable
> names are also accepted as fallbacks to keep existing spaces working.

---

## 🔧 Jellyfin Integration

The Telegram streamer:
1. Receives channel posts (via `setWebhook` or the in-app webhook route).
2. Indexes the message `file_id` into `/data/jellyfin/file_ids.json`.
3. Writes lightweight `.strm` pointers into `/data/jellyfin/media/` that point at the
   internal streaming endpoint (`http://127.0.0.1:8080/stream/{chat_id}/{message_id}/video.mp4`).
4. Fetches TMDB poster art and triggers a Jellyfin library refresh.

Obsolete Go-era `.strm` files (containing `8084` / `apx_`) are purged automatically on boot.

---

## 💾 Storage Architecture

* **`/data` (Attached HF Storage Bucket):**
  * Jellyfin configuration, database, cache and logs under `/data/jellyfin/`.
  * Virtual media library (`/data/jellyfin/media/Movies`, `/data/jellyfin/media/TV Shows`)
    with `.strm` pointers, `.nfo` metadata and posters.
  * Telegram file-index cache (`/data/jellyfin/file_ids.json`).
  * Apex session state (`/data/apex/session/`) and backups (`/data/apex/backups/`).
* **`/tmp` (High-Speed Ephemeral Disk):**
  * Temporary chunk cache for HTTP Range video streaming.

---

## 🔐 Accessing & Re-initializing `/data`

Because persistent storage is only mounted inside the space container, the gateway
exposes gated endpoints (protected by `APEX_SECRET_KEY`):

```bash
# Inspect the volume tree
curl -H "Authorization: Bearer $APEX_OPS_KEY" \
  "https://jishnupg-apex.hf.space/apex/ops/tree?path=/data"

# Wipe & rebuild the Jellyfin folder layout (destructive)
curl -X POST -H "Authorization: Bearer $APEX_OPS_KEY" \
  "https://jishnupg-apex.hf.space/apex/ops/jellyfin/reset"

# Idempotently create the standard directory skeleton
curl -X POST -H "Authorization: Bearer $APEX_OPS_KEY" \
  "https://jishnupg-apex.hf.space/apex/ops/jellyfin/init"

# Trigger a Jellyfin library refresh once booted
curl -X POST -H "Authorization: Bearer $APEX_OPS_KEY" \
  "https://jishnupg-apex.hf.space/apex/ops/jellyfin/scan"
```

---

## 📄 License & Credits
* [Jellyfin](https://jellyfin.org/) — GNU GPL
* [Pyrogram](https://github.com/pyrogram/pyrogram) — LGPLv3
* Powered by [Hugging Face Spaces](https://huggingface.co/spaces)