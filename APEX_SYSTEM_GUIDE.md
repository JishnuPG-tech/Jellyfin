# 🎬 Apex Media Platform (v2.0 Production)

**Live Ingress**: Port `7860` (Hugging Face Spaces default)  
**Stack**: Nginx Reverse Proxy Gateway + Official Jellyfin 10.9.11 LTS + Apex Core Go MTProto Daemon  
**Storage Architecture**: High-speed local cache (`/tmp`) + persistent NFS volume (`/data`)

---

## 1. 🏗️ Architecture Overview

Apex transforms Telegram into an infinite cloud video backend for Jellyfin. Video files sent or forwarded to the connected Telegram bot are cataloged without downloading to local disk: Apex writes lightweight `.strm` virtual pointer files that stream directly through an RFC 7233 HTTP Range proxy backed by MTProto workers.

```
                          Internet / Jellyfin Clients (:7860)
                                           │
                                           ▼
                 ┌───────────────────────────────────────────────────┐
                 │             Nginx Gateway (:7860)                 │
                 └─────────┬───────────────────────┬─────────────────┘
                           │                       │
         /stream/* & /vlc/*│                       │ / & /socket
       (Unbuffered Ranges) │                       │ (Web & Media API)
                           ▼                       ▼
                 ┌───────────────────┐   ┌───────────────────────────┐
                 │  Apex Core (Go)   │   │     Jellyfin 10.9.11      │
                 │      (:8084)      │   │          (:8096)          │
                 └─────────┬─────────┘   └─────────────┬─────────────┘
                           │                           │
          ┌────────────────┼────────────────┐          │ Reads .strm pointers
          │ MTProto Pool   │ Two-Tier Cache │          ▼
          │ (gotd/td)      │ (RAM + Disk)   │    /data/jellyfin/media/
          └────────┬───────┴────────────────┘    ├── Movies/
                   │                             └── Shows/
                   ▼
          Telegram Cloud Datacenters
```

---

## 2. ⚡ Key Features

1. **Direct Play Telegram Streaming**:
   - Zero permanent video storage on disk. Videos are streamed chunk-by-chunk on demand.
   - RFC 7233 compliant Partial Content (`206`) implementation with seek-aware prefetching.
   - Strict dual-tier authorization: local server playback is automatically authenticated; external access requires a valid HMAC token (`/vlc/{token}` or `/stream/{id}?token=...`).
2. **Two-Tier Cache with Singleflight**:
   - Tier 1: In-memory LRU ring buffer for micro-chunks (`APEX_MEMORY_CACHE_MB=128`).
   - Tier 2: Local disk LRU cache (`APEX_DISK_CACHE_MB=2048`) on `/tmp/apex-stream-cache`.
   - Singleflight request coalescing prevents duplicate concurrent downloads of the same Telegram media chunk.
   - Synchronous atomic disk persistence ensures zero race conditions during cache eviction.
3. **Adaptive Media Transfer Pool**:
   - Multi-worker MTProto pool with health tracking, error counting, and `FLOOD_WAIT` avoidance.
4. **Decoupled Ingestion Queue**:
   - Telegram message updates are received non-blockingly and dispatched to an ingestion worker pool.
   - Metadata parsed via smart title parser (multi-episode `S01E01-E03`, anime `[Group] Title - 01`, resolution tags).
   - Matched against TMDB with normalized confidence scoring (0.0 to 1.0). Automatic download of poster, fanart, and generation of XML-escaped Kodi/Jellyfin-compatible NFO files.
5. **Real-time Media Probe**:
   - Inspects streams with official `jellyfin-ffmpeg` `ffprobe` to determine container, video codec, audio codec, bit depth, HDR type, and baseline compatibility profile.
6. **Debounced Library Refresh**:
   - Batch uploads (e.g. whole seasons) trigger a single consolidated Jellyfin library scan after an 8-second debounce window.
7. **NFS SQLite Lock Safety**:
   - Jellyfin runs directly against `/data/jellyfin/data` with dedicated ASP.NET DataProtection persistence in `/data/jellyfin/.aspnet`.
   - Zero external scripts open Jellyfin's SQLite database while it is running, eliminating `SQLite Error 10: 'disk I/O error'`.
   - Apex Core runs its SQLite engine in `/tmp/apex-db/apex.db` (fast local memory/disk) and snapshots periodically to `/data/apex/backups/apex_latest.db`.

---

## 3. 📁 Storage Layout

```
/data/                                     # Persistent Storage Volume
├── jellyfin/
│   ├── data/                              # Jellyfin database & internal data
│   │   └── root/default/                  # Pre-configured library definitions
│   │       ├── Movies/options.xml
│   │       └── Shows/options.xml
│   ├── config/                            # Jellyfin system configs & branding
│   ├── .aspnet/DataProtection-Keys/       # Persistent ASP.NET encryption keys (prevents login resets)
│   ├── media/                             # Virtual media directory
│   │   ├── Movies/
│   │   │   └── Inception (2010)/
│   │   │       ├── Inception (2010).strm  # Contains "http://127.0.0.1:8084/stream/apx_..."
│   │   │       ├── movie.nfo
│   │   │       ├── poster.jpg
│   │   │       └── backdrop.jpg
│   │   └── Shows/
│   │       └── Breaking Bad/
│   │           ├── tvshow.nfo
│   │           ├── poster.jpg
│   │           └── Season 01/
│   │               ├── Breaking Bad S01E01.strm
│   │               ├── Breaking Bad S01E01.nfo
│   │               ├── Breaking Bad S01E01-thumb.jpg
│   │               ├── Breaking Bad S01E02.strm
│   │               ├── Breaking Bad S01E02.nfo
│   │               └── Breaking Bad S01E02-thumb.jpg
│   └── log/                               # Jellyfin log files
└── apex/
    ├── backups/
    │   └── apex_latest.db                 # Periodic snapshot of Apex Core SQLite database
    ├── session/
    │   ├── session.json                   # Telegram MTProto session credentials
    │   └── jellyfin_api_key.txt           # Persisted Jellyfin Administrator API key
    └── metadata-cache/                    # TMDB cache

/tmp/                                      # High-speed ephemeral container storage
├── apex-db/                               # Apex Core live SQLite DB (zero NFS locks)
├── apex-stream-cache/                     # Local 2GB LRU disk cache for video chunks
├── jellyfin-cache/                        # Jellyfin transcode & image cache
├── nginx_access.log
└── nginx_error.log
```

---

## 4. ⚙️ Environment Variables

| Variable | Description | Default |
|---|---|---|
| `APEX_SECRET_KEY` | 32-byte HMAC secret key for streaming & URL tokens | **Required** (set in HF Secrets) |
| `TELEGRAM_API_ID` | Telegram API ID from my.telegram.org | `0` |
| `TELEGRAM_API_HASH` | Telegram API Hash from my.telegram.org | `""` |
| `TELEGRAM_BOT_TOKEN` | Bot Token from @BotFather | `""` |
| `TELEGRAM_ALLOWED_CHAT_IDS`| Comma-separated list of allowed user/chat/channel IDs | `""` (all allowed) |
| `TMDB_API_KEY` | The Movie Database v3 API Key | `""` |
| `APEX_JELLYFIN_API_KEY`| Jellyfin Administrator API key for library sync | `""` (can bootstrap via `/apex/api-key`) |
| `APEX_MEMORY_CACHE_MB` | RAM buffer for streaming chunks | `128` |
| `APEX_DISK_CACHE_MB` | Ephemeral disk LRU cache size | `2048` |
| `APEX_PREFETCH_MB` | Sequential prefetch window | `16` |
| `APEX_MAX_STREAMS` | Concurrency limit for active streams | `2` |
| `APEX_TELEGRAM_MEDIA_CLIENTS` | Size of MTProto worker pool | `2` |

---

## 5. 🚀 Usage & Setup Instructions

1. **Initial Deployment**:
   - Set `APEX_SECRET_KEY` (32 characters or hex string) in Hugging Face Space Secrets.
   - Start the Space. On first launch, access `http://<space-host>:7860` to complete the initial Jellyfin Setup Wizard:
     - Create your administrator user and password.
     - Movies and Shows libraries are pre-configured pointing to `/data/jellyfin/media/Movies` and `/data/jellyfin/media/Shows`.
2. **Connecting Jellyfin API**:
   - In Jellyfin Web: Go to **Dashboard** $\rightarrow$ **API Keys** $\rightarrow$ Click **+** to generate a new key (name it e.g. `Apex`).
   - Copy the key and either:
     - Set it as `APEX_JELLYFIN_API_KEY` in HF Secrets, OR
     - Send a POST request to Apex Core:
       ```bash
       curl -X POST http://<space-host>:7860/apex/api-key \
            -H "Content-Type: application/json" \
            -d '{"api_key": "<YOUR_JELLYFIN_API_KEY>"}'
       ```
     - Apex validates the key, persists it to `/data/apex/session/jellyfin_api_key.txt`, and automatically ensures libraries are synchronized!
3. **Cataloging Content via Telegram**:
   - Send or forward any video file (.mp4, .mkv, etc.) to your Telegram bot.
   - Apex queues the file, resolves TMDB metadata, writes the virtual `.strm` pointers, and triggers a debounced scan.
   - For multi-episode files (e.g. `S01E01-E03`), discrete `.strm` and NFO files are created for every individual episode, providing seamless library indexing and independent progress tracking.
4. **Playing Content**:
   - Play directly in Jellyfin Web or mobile apps with zero permanent storage footprint.
   - For external players (e.g. VLC), use signed URLs generated via `/vlc/{token}`.
