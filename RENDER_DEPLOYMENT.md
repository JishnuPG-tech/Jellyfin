# Deploy Hermes Agent to Render.com

## Why Render Over HF Spaces?
✅ **Unrestricted networking** - Telegram polling works perfectly
✅ **Always-on with pinger** - GitHub Actions keeps it alive 24/7
✅ **Free tier** - $0/month with our keep-alive solution
✅ **Fast deployment** - 5 minutes from start to working bot

## Step 1: Push to GitHub

Make sure your code is pushed to GitHub (this repo).

```bash
git add -A
git commit -m "Add Render deployment files"
git push origin main
```

## Step 2: Create Render Account

1. Go to https://render.com
2. Sign up (free)
3. Connect your GitHub account

## Step 3: Deploy to Render

1. Click **"New +"** → **"Web Service"**
2. Select your GitHub repository (NousResearch/hermes-agent)
3. Configure:

| Setting | Value |
|---------|-------|
| **Name** | `hermes-agent` |
| **Environment** | `Docker` |
| **Branch** | `main` |
| **Dockerfile** | `Dockerfile.render` |
| **Plan** | `Free` |
| **Auto-deploy** | `Yes` |

4. Click **"Create Web Service"**

Render will:
- Build the Docker image (~3 minutes)
- Start the container
- Assign a URL like `https://hermes-agent-xxxx.onrender.com`

## Step 4: Add Environment Variables

In Render dashboard:
1. Go to **Settings** → **Environment**
2. Add these as **secret** (sensitive):
   - `OPENROUTER_API_KEY` = `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`
   - `TELEGRAM_BOT_TOKEN` = `8813790386:AAGeDVV6TIjvUkV-lY7luPxkgMR8STzurH4`
   - `FIRECRAWL_API_KEY` = `fc-02e3413d720f4ca6a75002e46cb336d8`

3. Click **"Save"** → Container restarts automatically

## Step 5: Update Keep-Alive Pinger

The GitHub Actions workflow needs your Render URL:

1. Get your Render service URL from the dashboard (e.g., `https://hermes-agent-xxxx.onrender.com`)
2. Edit `.github/workflows/keep-alive.yml`
3. Replace the URL:
   ```yaml
   curl -s -o /dev/null -w "HTTP %{http_code}" https://hermes-agent-xxxx.onrender.com/
   ```

4. Commit and push:
   ```bash
   git add .github/workflows/keep-alive.yml
   git commit -m "Update Render keep-alive pinger URL"
   git push
   ```

5. GitHub Actions will now ping your service every 10 minutes automatically

## Step 6: Test the Bot

1. Get your Render URL from the dashboard
2. Send a message to your Telegram bot: `@Jishnupg_MyHermesAgent_bot`
3. Bot should respond with Claude 3.5 Sonnet answers
4. Check Render logs to see the conversation

## How Keep-Alive Works

```
GitHub Actions runs every 10 minutes
    ↓
Makes HTTP request to Render service
    ↓
Render sees activity → stays awake
    ↓
Telegram bot always responds
```

Since GitHub Actions runs 24/7 and makes requests every 10 minutes, your Render service will never sleep.

## Troubleshooting

### Service keeps spinning down
- Check that keep-alive workflow is enabled
- Verify Render URL in `.github/workflows/keep-alive.yml`
- Check GitHub Actions tab to see if pings are running

### Bot not responding
- Check Render logs (Dashboard → Logs)
- Verify environment variables are set (case-sensitive!)
- Look for errors about OpenRouter API or Telegram token

### Telegram polling slow
- Render free tier may have slight latency
- Upgrade to paid tier for guaranteed performance

## Costs
- **Render**: Free tier ($0)
- **GitHub Actions**: Free tier ($0)
- **OpenRouter API**: Pay-as-you-go (~$0.01-0.10 per conversation)
- **Firecrawl**: 100 credits free/month

**Total**: ~$0-1/month depending on usage

## Monitoring

Check your bot's uptime:
1. Render Dashboard → **Logs** (see activity in real-time)
2. GitHub Actions → **Actions tab** (see ping results)

---
**Status**: Ready to deploy!
**Next**: Follow steps 1-6 above
