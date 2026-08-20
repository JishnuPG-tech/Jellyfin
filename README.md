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

A high-performance containerized workspace hosted on **Hugging Face Spaces** free tier (2 vCPU, 16 GB RAM, 50 GB disk).

This Space uses a **Reverse Proxy Gateway (Nginx)** architecture, allowing you to host multiple self-contained tools and Docker applications inside a single Hugging Face Space.

---

## 📦 Hosted Applications

| Application | Path | Internal Port | Description |
| :--- | :--- | :--- | :--- |
| **Portal Hub** | `/` | `7860` | Interactive dashboard listing all hosted tools. |
| **Stirling-PDF** | `/stirling/` | `8080` | Full-featured offline & private PDF suite (OCR, merge, split, convert, edit). |
| *Project Slot 2* | `/tool2/` | `8081` | *Ready for your next application* |
| *Project Slot 3* | `/tool3/` | `8082` | *Ready for your next application* |

---

## 🛠️ Architecture

```
                                  Hugging Face Space
                             ┌──────────────────────────────┐
                             │       Public Ingress         │
                             │        (Port 7860)           │
                             └──────────────┬───────────────┘
                                            │
                                            ▼
                             ┌──────────────────────────────┐
                             │        Nginx Gateway         │
                             │      Reverse Proxy & Hub     │
                             └──────┬───────────────┬───────┘
                                    │               │
                 ┌──────────────────┴──┐         ┌──┴──────────────────┐
                 │                     │         │                     │
                 ▼                     ▼         ▼                     ▼
          ┌─────────────┐       ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
          │  Dashboard  │       │ Stirling-PDF│ │  Project 2  │ │  Project 3  │
          │  Portal (/) │       │  (/stirling)│ │  (/tool2)   │ │  (/tool3)   │
          │  Port 7860  │       │  Port 8080  │ │  Port 8081  │ │  Port 8082  │
          └─────────────┘       └─────────────┘ └─────────────┘ └─────────────┘
```

---

## ➕ How to Add Another Docker Project to this Space

To host an additional tool or microservice (e.g. IT-Tools, CyberChef, FileBrowser, custom Python/Node API):

### 1. Install your tool in `Dockerfile`
Add the binary or repository installation in `Dockerfile`. For example:
```dockerfile
# Example: Install a second application
RUN apt-get install -y my-app
```

### 2. Register with `supervisord.conf`
Uncomment and configure the process in `supervisord.conf`:
```ini
[program:project2]
command=/usr/bin/my-app --port 8081
autostart=true
autorestart=true
priority=30
```

### 3. Add reverse proxy route in `nginx.conf`
Uncomment and adjust the location block in `nginx.conf`:
```nginx
location /tool2/ {
    proxy_pass http://127.0.0.1:8081/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### 4. Update the Card in `portal/index.html`
Update the link and title for **Project Slot 2** in `portal/index.html`.

---

## 🔐 Enabling Authentication & Security in Stirling-PDF

If you would like to password-protect your Stirling-PDF instance, set the following environment variables in your Hugging Face Space Settings (**Settings -> Variables and secrets**):

* `DOCKER_ENABLE_SECURITY`: `true`
* `SECURITY_ENABLELOGIN`: `true`
* `SECURITY_INITIALLOGIN_USERNAME`: `admin`
* `SECURITY_INITIALLOGIN_PASSWORD`: `<your-secure-password>`

---

## 📄 License & Credits
* [Stirling-PDF](https://github.com/Stirling-Tools/Stirling-PDF) — GPL-3.0 License
* Powered by [Hugging Face Spaces](https://huggingface.co/spaces)
