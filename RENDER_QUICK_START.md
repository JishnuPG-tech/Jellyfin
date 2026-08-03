# Deploy Hermes Agent to Render.com (Quick Start)

## ✅ Solution for Render Sleep Issue

The GitHub Actions **keep-alive pinger** solves Render's free tier sleep problem:

### How It Works
```
GitHub Actions (runs every 10 minutes, 24/7)
    ↓
Makes HTTP request to your Render service
    ↓
Render sees activity → never goes to sleep
    ↓
Telegram bot always available
```

### Result
✅ **Free tier** - $0/month  
✅ **Always-on** - 24/7 uptime  
✅ **Telegram polling works** - Unrestricted outbound networking  
✅ **No costs** - Only pay for API calls to OpenRouter  

---

## 📋 Deployment Checklist

### Step 1: Create Render Account
- [ ] Go to https://render.com
- [ ] Sign up (GitHub login works)
- [ ] Verify email

### Step 2: Deploy Service
- [ ] Click "New +" → "Web Service"
- [ ] Select your GitHub repo: `NousResearch/hermes-agent`
- [ ] Name: `hermes-agent`
- [ ] Dockerfile path: `Dockerfile.render`
- [ ] Plan: `Free`
- [ ] Click "Create Web Service"
- [ ] **Copy the Render URL** (will look like: `https://hermes-agent-xxxx.onrender.com`)

### Step 3: Add Environment Variables
In Render Dashboard → Settings → Environment → Add these:

```
OPENROUTER_API_KEY=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
TELEGRAM_BOT_TOKEN=8813790386:AAGeDVV6TIjvUkV-lY7luPxkgMR8STzurH4
FIRECRAWL_API_KEY=fc-02e3413d720f4ca6a75002e46cb336d8
GATEWAY_ALLOW_ALL_USERS=true
```

Then click **Save** (service will restart)

### Step 4: Set Up Keep-Alive Pinger

Edit `.github/workflows/keep-alive.yml` in your repo:

**Find this line:**
```yaml
curl -s -o /dev/null -w "HTTP %{http_code}" https://hermes-agent.onrender.com/
```

**Replace with your Render URL:**
```yaml
curl -s -o /dev/null -w "HTTP %{http_code}" https://hermes-agent-xxxx.onrender.com/
```

Commit and push - GitHub Actions will now ping every 10 minutes automatically.

---

## 🧪 Test the Bot

**Wait 5 minutes for Render to build and start, then:**

1. Open Telegram
2. Find your bot: `@Jishnupg_MyHermesAgent_bot`
3. Send a message: `"Hello"`
4. **Expected response:** Claude 3.5 Sonnet answer within 10 seconds

**Check logs:**
- Render Dashboard → Logs (watch messages come through)
- GitHub Actions → Actions tab (see pings every 10 min)

---

## 📊 Monitoring

### Check Service Status
- Dashboard shows **"Live"** = ✅ Running
- Look for activity in **Logs** tab

### Keep-Alive Status
- GitHub Actions tab → `keep-alive` workflow
- Should show green checkmarks every 10 minutes

### Bot Activity
```
[Render logs]
Gateway starting...
Telegram connected...
Message received from user: "hello"
Response sent: "Hi! I'm Claude 3.5..."
```

---

## 💰 Costs Breakdown

| Service | Cost | Notes |
|---------|------|-------|
| **Render** | $0 | Free tier (with pinger) |
| **GitHub Actions** | $0 | Free tier |
| **OpenRouter API** | $0.01-0.20 | Per conversation (~5-20 messages) |
| **Firecrawl** | $0 | 100 free credits/month |
| **TOTAL** | ~$0-1/month | Depends on usage |

---

## 🚨 Troubleshooting

### Service keeps crashing
- Check Render logs for errors
- Verify environment variables are set (case-sensitive!)
- Check that secrets don't have extra spaces

### Bot not responding
- Verify TELEGRAM_BOT_TOKEN is correct
- Check Telegram bot is not used elsewhere
- Wait 30 seconds, try again

### Keep-alive not working
- Verify `.github/workflows/keep-alive.yml` has your Render URL
- Check GitHub Actions tab - should show pings every 10 minutes
- If not running, enable Actions in repo settings

### Slow responses
- Render free tier may take 2-5 seconds on first request
- Subsequent requests are faster
- Upgrade to paid tier if needed

---

## 📝 Files Created

- **Dockerfile.render** - Docker config optimized for Render
- **.github/workflows/keep-alive.yml** - GitHub Actions pinger (prevents sleep)
- **RENDER_DEPLOYMENT.md** - Full deployment guide

## 🎯 Next Steps

1. Follow the **Deployment Checklist** above (5-10 minutes)
2. Wait for Render build to complete (2-3 minutes)
3. Test the bot on Telegram
4. Monitor logs to see messages flowing

**Your Telegram bot will be live in ~15 minutes!**
