---
title: Apex Multi-Tool Suite
emoji: 🛠️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# 🚀 Apex Multi-Tool Cloud Suite

A high-performance containerized workspace hosted on **Hugging Face Spaces** with persistent storage support.

This Space uses a **Reverse Proxy Gateway (Caddy)** architecture, allowing you to host multiple self-contained tools and Docker applications inside a single Hugging Face Space.

---

## 📦 Hosted Applications

| Application | Path | Internal Port | Description |
| :--- | :--- | :--- | :--- |
| **Portal Hub** | `/` | `7860` | Interactive dashboard listing all hosted tools. |
| **Stirling-PDF** | `/stirling/` | `8080` | Full-featured offline & private PDF suite (OCR, merge, split, convert, edit). |
| *Project Slot 2* | `/tool2/` | `8081` | *Ready for your next application* |
| *Project Slot 3* | `/tool3/` | `8082` | *Ready for your next application* |

---

## 💾 Persistent Storage Structure

All Stirling-PDF credentials, user accounts, custom configurations, pipelines, and logs are automatically preserved across restarts in the persistent folder:

```
/data/Stirling/
├── configs/       # User accounts, H2 database, settings.yml, password hashes
├── logs/          # Server and access logs
├── customFiles/   # Custom fonts, digital signature certificates, stamps
├── pipeline/      # Saved automated conversion pipelines & workflows
├── storage/       # File storage
└── tessdata/      # Custom Tesseract OCR language models (.traineddata)
```

> **Note**: In Hugging Face Spaces, persistent storage is mounted at `/data`. Everything under `/data/Stirling` remains completely safe and persists across container restarts, rebuilds, and redeployments.

---

## 🔐 Enabling Authentication & Security in Stirling-PDF

To enable login authentication, set these environment variables in your Hugging Face Space Settings (**Settings -> Variables and secrets**):

* `DOCKER_ENABLE_SECURITY`: `true`
* `SECURITY_ENABLELOGIN`: `true`
* `SECURITY_INITIALLOGIN_USERNAME`: `admin`
* `SECURITY_INITIALLOGIN_PASSWORD`: `<your-secure-password>`

*Once created, all user credentials and settings are permanently saved to `/data/Stirling/configs/`.*

---

## ➕ How to Add Another Docker Project to this Space

1. **Install/Copy your tool in `Dockerfile`**
2. **Start the service in `entrypoint.sh`** (e.g. `/my-tool --port 8081 &`)
3. **Add the route in `Caddyfile`** (e.g. `handle @tool2 { reverse_proxy 127.0.0.1:8081 }`)
4. **Update `portal/index.html`** with the new tool link.

---

## 📄 License & Credits
* [Stirling-PDF](https://github.com/Stirling-Tools/Stirling-PDF) — GPL-3.0 License
* Powered by [Hugging Face Spaces](https://huggingface.co/spaces)
