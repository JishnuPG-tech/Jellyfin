# 🚀 Hermes Agent - Docker & Hugging Face Spaces Setup Guide

This guide covers deploying Hermes Agent with Docker locally and on Hugging Face Spaces.

## 📋 Prerequisites

### For Local Testing
- Docker & Docker Compose installed
- Your API keys ready:
  - OpenRouter API key (https://openrouter.ai)
  - Telegram bot token (from @BotFather)
  - Firecrawl API key (optional)

### For Hugging Face Spaces
- Hugging Face account (free)
- Same API keys as above

---

## 🏠 Local Docker Testing

### 1. Prepare Environment

Create `.env.docker` in the repo root:
```bash
OPENROUTER_API_KEY=sk-or-v1-your-key-here
TELEGRAM_BOT_TOKEN=123456789:ABCDEFGHIJxyz
FIRECRAWL_API_KEY=fc-your-key-here
```

### 2. Build & Run with Docker Compose

```bash
# Build the image
docker-compose -f docker-compose.hf.yml build

# Start the service
docker-compose -f docker-compose.hf.yml up -d

# View logs
docker-compose -f docker-compose.hf.yml logs -f

# Stop the service
docker-compose -f docker-compose.hf.yml down
```

### 3. Verify It's Working

```bash
# Check if the container is running
docker ps | grep hermes-agent

# Test the endpoint
curl http://localhost:7860

# Send a test Telegram message to your bot
# (it should respond via Claude 3.5 Sonnet)
```

### 4. Troubleshooting Local

```bash
# View detailed logs
docker-compose -f docker-compose.hf.yml logs hermes-agent --tail 100

# Execute shell in running container
docker exec -it hermes-agent bash

# Check environment variables in container
docker exec hermes-agent printenv | grep -E "OPENROUTER|TELEGRAM|FIRECRAWL"

# Rebuild without cache
docker-compose -f docker-compose.hf.yml build --no-cache
```

---

## 🤖 Deploy to Hugging Face Spaces

### Step 1: Create a Space on HF

1. Go to https://huggingface.co/spaces
2. Click **Create new Space**
3. Fill in:
   - **Space name**: `hermes-agent` (or custom name)
   - **License**: MIT
   - **Space SDK**: Docker
   - **Visibility**: Public or Private
4. Click **Create**

### Step 2: Add Repository Secrets

1. Go to your Space **Settings** → **Repository secrets**
2. Add these secrets:

| Secret Name | Value | How to Get |
|---|---|---|
| OPENROUTER_API_KEY | `sk-or-v1-...` | https://openrouter.ai/keys |
| TELEGRAM_BOT_TOKEN | `123456789:ABC...` | Message @BotFather on Telegram |
| FIRECRAWL_API_KEY | `fc-...` (optional) | https://www.firecrawl.dev |

**⚠️ IMPORTANT**: Secrets are **not** readable once saved. Make sure to copy them correctly.

### Step 3: Upload Files to Space

Clone the Space repository:
```bash
git clone https://huggingface.co/spaces/YOUR_USERNAME/hermes-agent
cd hermes-agent
```

Copy these files from this repo:
```bash
cp /path/to/hermes-agent/Dockerfile.hf ./Dockerfile
cp /path/to/hermes-agent/start.sh ./start.sh
cp /path/to/hermes-agent/.dockerignore ./.dockerignore
```

Push to HF:
```bash
git add .
git commit -m "Deploy Hermes Agent"
git push
```

### Step 4: Wait for Build & Deploy

1. HF Spaces will automatically build your Docker image
2. Check **Logs** tab for build progress
3. Once complete, your Space will show a URL like: `https://huggingface.co/spaces/YOUR_USERNAME/hermes-agent`

### Step 5: Test Your Bot

1. Open Telegram and find your bot
2. Send a message: `/start`
3. Send another message: `Hi there!`
4. The bot should respond using Claude 3.5 Sonnet

---

## 📊 Configuration Files Explained

### `Dockerfile.hf`
- **Purpose**: Builds a Docker image optimized for HF Spaces
- **Key Features**:
  - Clones Hermes Agent from GitHub
  - Installs Python dependencies with `pip install -e .`
  - Exposes port 7860 (HF Spaces standard)
  - Runs `start.sh` as entrypoint

### `start.sh`
- **Purpose**: Startup script that runs inside the container
- **What it does**:
  - Creates `/data` directory for persistent storage
  - Generates `.env` file from HF Spaces environment variables
  - Generates `config.yaml` with sensible defaults
  - Starts the Hermes gateway

### `docker-compose.hf.yml`
- **Purpose**: Local testing before deploying to HF Spaces
- **Includes**: Volume mounts, port mapping, health checks

### `.dockerignore`
- **Purpose**: Excludes unnecessary files from Docker build
- **Helps**: Reduce image size and build time

---

## 🔧 Customization

### Change the Default Model

After deployment, edit your Space's `config.yaml`:

```yaml
model:
  default: openai/gpt-4-turbo  # Change to any OpenRouter model
  provider: openrouter
```

Available models:
```
openrouter models:
  - anthropic/claude-3.5-sonnet (default)
  - anthropic/claude-3-opus
  - openai/gpt-4-turbo
  - meta-llama/llama-3.1-405b
  - mistralai/mistral-large
  - google/gemini-pro
  - and 200+ more...
```

### Add More Telegram Commands

Edit `start.sh` to add custom commands in the `config.yaml` section:

```yaml
telegram:
  reactions: false
  allowed_chats: ''  # Empty = allow all chats
```

### Enable Web Extraction

Already enabled with Firecrawl! The agent can now:
- Extract content from websites
- Search the web
- Analyze web pages

---

## 💾 Persistent Storage

The deployment uses Docker volumes (`/data`) for:
- Session history
- User skills
- Memory/context
- Configuration

**On HF Spaces**: Data persists across container restarts but is **deleted** if the Space is deleted.

**Backup**: SSH into the Space and download `/data/` directory regularly.

---

## ⚠️ HF Spaces Limitations

### Free Tier
- **Idle Timeout**: ~24 hours without activity (container restarts)
- **CPU**: 2 vCPU
- **RAM**: 16 GB
- **Storage**: 50 GB
- **Cost**: Free

### Paid Tier ($7/mo)
- **No idle timeout** (24/7 uptime)
- **Same CPU/RAM/Storage**
- **Recommended for**: Production Telegram bots

### Better Alternatives for 24/7
- **DigitalOcean App Platform**: $5/mo
- **Heroku**: Free tier retired, $7/mo now
- **Replit**: $7/mo with always-on
- **Self-hosted VPS**: $3-5/mo on Linode/Vultr

---

## 🐛 Troubleshooting

### "Bot not responding"
```bash
# Check logs
# Go to Space → Logs tab
# Look for: "API call failed" or "AuthenticationError"

# Common causes:
# 1. OPENROUTER_API_KEY not set in secrets
# 2. OPENROUTER_API_KEY is wrong/invalid
# 3. TELEGRAM_BOT_TOKEN is wrong
```

### "Container keeps restarting"
```
# Causes:
# 1. Python error in start.sh
# 2. Missing dependency
# 3. Out of memory
# 4. Idle timeout on free tier

# Check logs for errors
# If needed: delete /data/config.yaml to regenerate
```

### "Space is stuck in 'Building'"
```
# 1. Wait 10+ minutes (sometimes takes a while)
# 2. Manually trigger rebuild: go to Settings → Docker
# 3. If still stuck: delete & recreate Space
```

### "Telegram bot receives empty responses"
```
# This means: OPENROUTER_API_KEY is not set or is invalid

# Solutions:
# 1. Double-check the API key in HF Spaces secrets
# 2. Verify key starts with "sk-or-v1-"
# 3. Check OpenRouter account has credits: https://openrouter.ai
# 4. Restart Space: Settings → Restart
```

---

## 📈 Production Checklist

Before going live with a production Telegram bot:

- [ ] Test locally with `docker-compose.hf.yml`
- [ ] Verify all 3 secrets are set on HF Space (Settings → Secrets)
- [ ] Send test message to bot and confirm response
- [ ] Check OpenRouter account has sufficient credits
- [ ] Set appropriate rate limits in `config.yaml` if needed
- [ ] Monitor Space logs for errors
- [ ] Set up monitoring/alerting if critical
- [ ] Consider paid HF Spaces tier for 24/7 uptime
- [ ] Create backup of `/data` directory regularly

---

## 📚 Resources

- **Hermes Docs**: https://hermes-agent.nousresearch.com
- **OpenRouter**: https://openrouter.ai
- **Telegram Bot API**: https://core.telegram.org/bots
- **HF Spaces Docs**: https://huggingface.co/docs/hub/spaces
- **Docker Docs**: https://docs.docker.com

---

## 🆘 Need Help?

1. **Check logs**: HF Spaces → Logs tab
2. **Review errors**: Look for API key or network errors
3. **Restart**: Settings → Restart might help
4. **Rebuild**: Settings → Docker → Rebuild
5. **Read Hermes docs**: https://hermes-agent.nousresearch.com/docs/

Good luck! 🚀
