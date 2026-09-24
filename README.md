---
title: Apex Media Platform
emoji: 🎬
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 🚀 Apex Media Platform & Cloud Suite

A high-performance media server and containerized workspace hosted on **Hugging Face Spaces** with persistent storage support.

Apex combines an official **Jellyfin** media server with a high-concurrency **Go Streaming Gateway** (`gotd/td`) that allows on-demand streaming of authorized personal media directly from Telegram without permanently writing complete video files to disk.

---

## 📦 Hosted Services & Endpoints

| Application | Path | Internal Port | Description |
| :--- | :--- | :--- | :--- |
| **Jellyfin Media Server** | `/` | `8096` | Official Jellyfin interface (Direct Play media, Continue Watching, Movies & Shows). |
| **Apex Range Streaming** | `/stream/` | `8084` | Unbuffered HTTP Range streaming gateway backed by Telegram MTProto. |
| **Apex VLC Playback** | `/vlc/` | `8084` | Direct HTTP streaming endpoint for external players (VLC, MPV). |
| **Apex Health API** | `/apex/health`| `8084` | Health status and diagnostic metrics for the Go daemon. |
| **Stirling-PDF** | `/stirling/` | `8080` | Full-featured offline & private PDF suite (OCR, merge, split, convert, edit). |
| **Lucent — Document Restorer** | `/enhancer/` | `8082` | Laser-clean document whitening & bleed-through remover (FastAPI + React 19). |
| **Portal Hub** | `/portal/` | `7860` | Central static landing dashboard for tools. |

---

## ⚙️ Configuration & Environment Secrets

Configure these in **HF Space Settings $\rightarrow$ Variables and secrets**:

| Variable | Description |
| :--- | :--- |
| `TELEGRAM_API_ID` | Telegram API application ID |
| `TELEGRAM_API_HASH` | Telegram API application hash |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot token for MTProto authentication |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated list of allowed source channel/group chat IDs |
| `TMDB_API_KEY` | The Movie Database v3 API key for automated metadata & poster fetching |
| `APEX_SECRET_KEY` | Secret key used for signing tokens and admin operations |
| `APEX_JELLYFIN_API_KEY` | Jellyfin API key used by Apex for library provisioning and refresh operations |

---

## 🔧 Jellyfin Integration

Apex automatically maintains the application-owned **Movies** and **Shows** Jellyfin libraries. It verifies the canonical paths, repairs stale library paths, and refreshes Jellyfin after catalog ingestion.

The Jellyfin database is checked for SQLite integrity and Jellyfin 10.9.11 schema compatibility before startup. Corrupt or incompatible database files are quarantined under `/data/jellyfin/backups/` while the STRM/NFO media catalog under `/data/jellyfin/media/` is preserved.

## 💾 Storage Architecture

* **`/data` (Attached HF Storage Bucket):**
  * Persistent Jellyfin configurations, user accounts, and library databases.
  * Virtual media library (`.strm` virtual pointers, `.nfo` metadata, high-resolution posters).
  * Telegram session state (`/data/apex/session/`).
  * Periodic atomic snapshots of the Apex catalog database (`/data/apex/backups/apex_latest.db`).
* **`/tmp` (High-Speed Ephemeral Disk):**
  * Live SQLite database (`/tmp/apex-db/apex.db`).
  * Temporary chunk cache for HTTP Range video streaming (`/tmp/apex-stream-cache/`), automatically managed by a two-tier LRU engine.

---

## 📄 License & Credits
* [Jellyfin](https://jellyfin.org/) — GNU GPL
* [gotd/td](https://github.com/gotd/td) — MIT License
* [Stirling-PDF](https://github.com/Stirling-Tools/Stirling-PDF) — GPL-3.0 License
* Powered by [Hugging Face Spaces](https://huggingface.co/spaces)
