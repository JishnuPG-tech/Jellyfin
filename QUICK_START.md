# 🚀 Hermes Agent HF Spaces - Quick Reference Card

## Deploy to Hugging Face Spaces in 5 Minutes

### 1️⃣ Create Space
```
https://huggingface.co/spaces → Create new Space
Name: hermes-agent
SDK: Docker
Visibility: Public/Private
```

### 2️⃣ Add Secrets (Settings → Repository secrets)
```
OPENROUTER_API_KEY = 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
TELEGRAM_BOT_TOKEN = 8813790386:AAGeDVV6TIjvUkV-lY7luPxkgMR8STzurH4
FIRECRAWL_API_KEY = fc-02e3413d720f4ca6a75002e46cb336d8
```

### 3️⃣ Push Files
```bash
git clone https://huggingface.co/spaces/YOUR_USERNAME/hermes-agent
cd hermes-agent
cp /path/to/Dockerfile.hf ./Dockerfile
cp /path/to/start.sh ./start.sh
cp /path/to/.dockerignore .
git add .
git commit -m "Deploy Hermes Agent"
git push
```

### 4️⃣ Wait & Test
- Watch build in HF Spaces Logs (~5-10 min)
- Send message to your Telegram bot
- Bot responds with Claude 3.5 Sonnet ✅

---

## Local Testing First (Recommended)

```bash
# Build
docker-compose -f docker-compose.hf.yml build

# Run
docker-compose -f docker-compose.hf.yml up

# Test
curl http://localhost:7860
# Send test Telegram message

# Stop
docker-compose -f docker-compose.hf.yml down
```

---

## Your Credentials

✅ OpenRouter: `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`
✅ Telegram: `8813790386:AAGeDVV6TIjvUkV-lY7luPxkgMR8STzurH4`
✅ Firecrawl: `fc-02e3413d720f4ca6a75002e46cb336d8`

---

## Files Created

| File | Purpose |
|------|---------|
| `Dockerfile.hf` | Docker image definition |
| `start.sh` | Container startup script |
| `docker-compose.hf.yml` | Local testing setup |
| `HF_SPACES_DEPLOYMENT.md` | Detailed HF guide |
| `DOCKER_DEPLOYMENT.md` | Complete Docker guide |
| `SETUP_COMPLETE.md` | Full setup documentation |

---

## Key Features

- 🤖 Claude 3.5 Sonnet LLM (via OpenRouter)
- 🤖 Llama, Mistral, GPT-4 available too
- 📱 Telegram bot integration
- 🌐 Web extraction (Firecrawl)
- 💾 Persistent storage
- 🐳 Full Docker support
- 📊 Production-ready

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Bot not responding | Check OPENROUTER_API_KEY in secrets |
| Container won't start | View logs: HF Space → Logs |
| Empty responses | Update model in config.yaml |
| Idle timeout | Use paid HF tier ($7/mo) |
| Out of credits | Check https://openrouter.ai/settings/credits |

---

## Costs

- **HF Spaces Free**: $0 (with idle timeout)
- **HF Spaces Paid**: $7/month (24/7)
- **OpenRouter API**: ~$0.01-0.10 per message
- **Total**: $7-20/month for active production bot

---

## Next: Advanced Setup

1. Change model in `config.yaml`
2. Enable web search / vision / code execution
3. Set up cron jobs for scheduled tasks
4. Add custom skills
5. Scale to multiple profiles

See `DOCKER_DEPLOYMENT.md` for details.

---

**Ready?** → Go to step 1️⃣ above and create your Space!
