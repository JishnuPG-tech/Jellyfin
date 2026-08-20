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
| **Gemini Web2API** | `/v1/` or `/gemini/` | `8081` | OpenAI-compatible API gateway for Google Gemini (Cursor/Cline ready). |
| *Project Slot 3* | `/tool3/` | `8082` | *Ready for your next application* |

---

## 🤖 Using Gemini Web2API with AI Tools

You can connect **Cursor**, **Cline**, **NextChat**, or any OpenAI-compatible tool to this Space:

* **Base URL**: `https://<your-space-name>.hf.space/v1`
* **API Key**: `sk-gemini` (or custom key configured in `/data/gemini/config.json`)
* **Available Models**:
  * `gemini-3.6-flash`
  * `gemini-3.6-pro`
  * `gemini-3.5-pro`
  * `gemini-3.5-flash`

### Optional: Setting Custom Cookies for Authenticated Gemini Access
To use your own Gemini account limits:
1. Copy your Gemini session cookies as a JSON string.
2. In HF Space Settings -> **Variables and secrets**, add a secret named `GEMINI_COOKIES` with your JSON content.
3. The Space will automatically persist it to `/data/gemini/cookies.json` upon boot.

---

## 💾 Persistent Storage Structure

All Stirling-PDF credentials, configurations, pipelines, and Gemini Web2API settings are automatically preserved across restarts:

```
/data/
├── Stirling/
│   ├── configs/       # User accounts, H2 database, settings.yml
│   ├── logs/          # Server logs
│   ├── customFiles/   # Custom fonts, digital signatures, certificates
│   ├── pipeline/      # Saved automated conversion pipelines & workflows
│   ├── storage/       # File storage
│   └── tessdata/      # Custom Tesseract OCR language models (.traineddata)
└── gemini/
    ├── config.json    # Gemini Web2API configuration & custom API keys
    └── cookies.json   # Saved session cookies (optional)
```

---

## 📄 License & Credits
* [Stirling-PDF](https://github.com/Stirling-Tools/Stirling-PDF) — GPL-3.0 License
* [gemini-web2api](https://github.com/Sophomoresty/gemini-web2api) — MIT License
* Powered by [Hugging Face Spaces](https://huggingface.co/spaces)
