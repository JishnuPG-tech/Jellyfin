# Hermes Agent - Hugging Face Spaces Deployment

Deploy a fully functional Hermes Agent Telegram bot on Hugging Face Spaces!

## 🚀 Quick Start on Hugging Face Spaces

### Step 1: Create a Space
1. Go to [huggingface.co/spaces](https://huggingface.co/spaces)
2. Click **"Create new Space"**
3. Fill in:
   - **Space name**: `hermes-agent` (or your choice)
   - **License**: MIT
   - **Space SDK**: Docker
   - **Visibility**: Public or Private
4. Click **Create**

### Step 2: Add Required Secrets
Go to **Settings** → **Repository secrets** and add:
- `OPENROUTER_API_KEY` - Your OpenRouter API key (get from https://openrouter.ai)
- `TELEGRAM_BOT_TOKEN` - Your Telegram bot token (from @BotFather)
- `FIRECRAWL_API_KEY` - Your Firecrawl API key (optional, for web extraction)

Example values:
```
OPENROUTER_API_KEY=sk-or-v1-...
TELEGRAM_BOT_TOKEN=123456789:ABCDefGHIjklmnopqrstuvwxyz...
FIRECRAWL_API_KEY=fc-...
```

### Step 3: Configure Files
Push these files to your Space repository:
- `Dockerfile.hf` - The container definition
- `start.sh` - Startup script
- `.dockerignore` - Build optimization

Or rename `Dockerfile.hf` to `Dockerfile`:
```bash
git mv Dockerfile.hf Dockerfile
```

### Step 4: Deploy
Push to your Space repo. Hugging Face will automatically build and deploy the Docker image.

---

## 📋 Configuration

The deployment automatically:
- ✅ Sets up Hermes Agent with your API keys
- ✅ Configures the Telegram bot gateway
- ✅ Enables Firecrawl for web extraction
- ✅ Starts the gateway on port 7860

### Environment Variables Required
```bash
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxx        # Required for LLM
TELEGRAM_BOT_TOKEN=123456789:ABCDEFGHIJxyz   # Required for Telegram bot
FIRECRAWL_API_KEY=fc-xxxxxxxxxx              # Optional, for web scraping
```

---

## 🤖 Available Models

The deployment uses **Claude 3.5 Sonnet** by default via OpenRouter. You can change this in the generated `config.yaml`:

```yaml
model:
  default: anthropic/claude-3.5-sonnet  # Change this to any OpenRouter model
  provider: openrouter
```

Available models:
- `anthropic/claude-3.5-sonnet` - Best reasoning
- `anthropic/claude-3-opus` - Powerful
- `openai/gpt-4-turbo` - Fast reasoning
- `meta-llama/llama-3.1-405b` - Free tier
- `mistralai/mistral-large` - Fast & good
- And 200+ others on OpenRouter

---

## 📱 Using Your Telegram Bot

1. Find your bot on Telegram (or search the username you set with @BotFather)
2. Send any message
3. The bot will respond using Claude 3.5 Sonnet

Example commands:
```
/new - Start a fresh conversation
/help - Show available commands
/model - Change the LLM model
/compress - Compress conversation context
```

---

## 💾 Persistent Storage

The deployment uses `/data` volume for persistent storage:
- Session history
- Skills
- Memory
- Configuration

This survives container restarts.

---

## 📊 Monitoring

View logs from Hugging Face Spaces interface:
1. Go to your Space
2. Click **"Logs"** tab
3. You'll see real-time gateway and agent logs

---

## ⚠️ Limitations on HF Spaces

- **Free Tier**: ~24 hour idle timeout (container restarts if no activity)
- **Storage**: Limited to 50GB
- **Memory**: 16GB RAM
- **Compute**: 2 vCPU

For production (24/7 uptime without idle timeout):
- Use **Paid Tier** ($7/mo+)
- Or deploy on a **cheap VPS** ($5-6/mo on DigitalOcean/Linode)

---

## 🔄 Local Testing Before Deployment

Test locally before pushing to HF Spaces:

```bash
# Build the image
docker build -f Dockerfile.hf -t hermes-agent-hf .

# Run with your secrets
docker run -e OPENROUTER_API_KEY=sk-or-v1-... \
           -e TELEGRAM_BOT_TOKEN=123456789:ABC... \
           -e FIRECRAWL_API_KEY=fc-... \
           -p 7860:7860 \
           hermes-agent-hf
```

---

## 🆘 Troubleshooting

**Bot not responding?**
- Check that TELEGRAM_BOT_TOKEN is correct
- Verify OPENROUTER_API_KEY is valid
- Check logs for API errors

**Container keeps restarting?**
- Check logs for Python errors
- Ensure all environment variables are set
- Free tier idle timeout may have triggered restart

**Out of storage?**
- Delete old sessions: `rm -rf /data/sessions`
- Archive skills: `/data/skills/.archive/`

---

## 📚 Resources

- [Hermes Agent Docs](https://hermes-agent.nousresearch.com/docs/)
- [OpenRouter Models](https://openrouter.ai)
- [Telegram Bot API](https://core.telegram.org/bots)
- [Firecrawl Docs](https://www.firecrawl.dev)

---

## 💡 Next Steps

After deployment:

1. **Test your bot** - Send a message on Telegram
2. **Customize model** - Edit `config.yaml` to use a different LLM
3. **Enable more tools** - Configure web search, vision, etc.
4. **Set up cron jobs** - Schedule automated tasks
5. **Deploy on VPS** - For 24/7 without idle timeout

---

**Happy coding!** 🚀
