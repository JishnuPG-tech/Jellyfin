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
   - External player support via HMAC-authenticated URLs: `/vlc/{token}`.
2. **Two-Tier Cache with Singleflight**:
   - Tier 1: In-memory LRU ring buffer for micro-chunks (`APEX_MEMORY_CACHE_MB=128`).
   - Tier 2: Local disk LRU cache (`APEX_DISK_CACHE_MB=2048`) on `/tmp/apex-stream-cache`.
   - Singleflight request coalescing prevents duplicate concurrent downloads of the same Telegram media chunk.
3. **Adaptive Media Transfer Pool**:
   - Multi-worker MTProto pool with health tracking, error counting, and `FLOOD_WAIT` avoidance.
4. **Decoupled Ingestion Queue**:
   - Telegram message updates are received non-blockingly and dispatched to an ingestion worker pool.
   - Metadata parsed via smart title parser (multi-episode `S01E01-E03`, anime `[Group] Title - 01`, resolution tags).
   - Matched against TMDB with confidence scoring. Automatic download of poster, fanart, and generation of Kodi/Jellyfin-compatible NFO files.
5. **Real-time Media Probe**:
   - Inspects streams with official `jellyfin-ffmpeg` `ffprobe` to determine container, video codec, audio codec, bit depth, HDR type, and DirectPlay eligibility.
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
│   │               └── Breaking Bad S01E01-thumb.jpg
│   └── log/                               # Jellyfin log files
└── apex/
    ├── backups/
    │   └── apex_latest.db                 # Periodic snapshot of Apex Core SQLite database
    ├── session/
    │   ├── session.json                   # Telegram MTProto session credentials
    │   └── secret.key                     # 32-byte secret key for VLC HMAC tokens
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
| `TELEGRAM_API_ID` | Telegram API ID from my.telegram.org | `0` |
| `TELEGRAM_API_HASH` | Telegram API Hash from my.telegram.org | `""` |
| `TELEGRAM_BOT_TOKEN` | Bot Token from @BotFather | `""` |
| `TELEGRAM_ALLOWED_CHAT_IDS`| Comma-separated list of allowed user/chat/channel IDs | `""` (all allowed) |
| `TMDB_API_KEY` | The Movie Database v3 API Key | `""` |
| `APEX_SECRET_KEY` | 32-byte secret key for HMAC VLC URLs | Auto-generated & persisted |
| `APEX_MEMORY_CACHE_MB` | RAM buffer for streaming chunks | `128` |
| `APEX_DISK_CACHE_MB` | Ephemeral disk LRU cache size | `2048` |
| `APEX_PREFETCH_MB` | Sequential prefetch window | `16` |
| `APEX_MAX_STREAMS` | Concurrency limit for active streams | `2` |
| `APEX_TELEGRAM_MEDIA_CLIENTS` | Size of MTProto worker pool | `2` |

---

## 5. 🚀 Usage Instructions

1. **Initial Setup**:
   - Access the platform via your Hugging Face Space URL or `http://localhost:7860`.
   - On first launch, follow the standard Jellyfin Setup Wizard:
     - Set your preferred admin username and password.
     - Notice that **Movies** and **Shows** libraries are already pre-wired pointing to `/data/jellyfin/media/Movies` and `/data/jellyfin/media/Shows`.
2. **Cataloging Content**:
   - Send or forward any video file (.mp4, .mkv, etc.) to your configured Telegram bot.
   - The bot acknowledges the message and queues it for ingestion.
   - Apex automatically parses the title, fetches TMDB posters, backdrops, and episode details, writes the `.strm` file, and triggers a debounced scan.
   - Within seconds, the media item appears in Jellyfin with full artwork and metadata!
3. **Playing Content**:
   - Click Play in Jellyfin Web, Jellyfin Android/iOS, Android TV, or Kodi.
   - The client streams directly through the Range proxy with zero transcoding lag.
