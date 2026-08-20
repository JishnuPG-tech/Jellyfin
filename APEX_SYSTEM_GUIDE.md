# 🛠️ Apex Multi-Tool Cloud Suite — Complete System & Troubleshooting Guide

**Repository / Space**: [Hugging Face Space: Jishnupg/Apex](https://huggingface.co/spaces/Jishnupg/Apex)  
**Architecture**: Caddy Reverse Proxy Gateway + Stirling-PDF + Gemini Web2API + Static Portal Hub  
**Environment**: Hugging Face Spaces (Docker, Persistent Storage at `/data`)

---

## 1. 🏗️ Architecture Overview

The Apex Space is a **modular multi-tool cloud suite** running inside a single container using a Caddy reverse proxy to unify all internal services under public port `7860`.

```
                              Internet / HF Space URL (:7860)
                                             │
                                             ▼
                     ┌───────────────────────────────────────────────┐
                     │          Caddy Gateway (:7860 Public)         │
                     └───────┬───────────────┬───────────────┬───────┘
                             │               │               │
              ┌──────────────┴───────┐       │       ┌───────┴───────────────────────┐
              ▼                      ▼       │       ▼                               ▼
      /stirling* (Proxy)     /v1* & /gemini* │  /v1beta* (Proxy)             / (Static Files)
              │                      │       │       │                               │
              ▼                      └───────┼───────┘                               ▼
   ┌───────────────────────┐                 ▼                           ┌───────────────────────────┐
   │  Stirling-PDF (:8080) │      ┌─────────────────────┐                │     Portal Hub UI         │
   │   (Java 25 + OCR)     │      │ Gemini Web2API      │                │     (/srv/portal)         │
   │ (H2 Embedded Storage) │      │   (:8081 Python)    │                │ (Interactive Dashboard)   │
   └───────────────────────┘      └─────────────────────┘                └───────────────────────────┘
              │                              │
              ▼                              ▼
      /data/Stirling/                  /data/gemini/
    (Persistent Storage)            (Persistent Storage)
```

---

## 2. 🤖 Gemini Web2API Configuration

### Live Endpoints
* **Base URL**: `https://jishnupg-apex.hf.space/v1`
* **Models list**: `GET https://jishnupg-apex.hf.space/v1/models`
* **Chat completions**: `POST https://jishnupg-apex.hf.space/v1/chat/completions`

### Authentication
* **Default API Key**: `sk-gemini` (Pass as `Authorization: Bearer sk-gemini`)
* **Custom Cookie Secret**: Added as `GEMINI_COOKIES` in HF Space Settings -> automatically writes to `/data/gemini/cookies.json` and auto-fetches Google `SNlM0e` XSRF security tokens.

---

## 3. 📁 Persistent Storage Layout

```
/data/
├── Stirling/
│   ├── configs/          # User accounts, passwords, embedded H2 database, settings.yml
│   ├── logs/             # Application and server logs
│   ├── customFiles/      # Custom fonts, digital signatures, certificates
│   ├── pipeline/         # Saved automated conversion pipelines & workflows
│   ├── storage/          # Temporary & persistent file storage
│   └── tessdata/         # Tesseract OCR language training data (.traineddata)
└── gemini/
    ├── config.json       # Gemini Web2API configuration & custom API keys
    └── cookies.json      # Saved session cookies
```

---

## 4. 🧪 Verified Live Test Results

### A. Non-Streaming Test
* **Request**: `{"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "Hello, what is 2+2? Answer in one word."}]}`
* **Response**: `HTTP 200 OK` → `{"choices": [{"message": {"role": "assistant", "content": "Four"}}]}`

### B. Streaming Test
* **Request**: `{"model": "gemini-3.6-flash", "stream": true, "messages": [...]}`
* **Response**: `HTTP 200 OK` → Server-Sent Events (SSE) chunks received and finalized with `[DONE]`.
