# ✅ Hermes Agent Deployment Checklist

## Pre-Deployment Checklist

### Configuration Files
- [x] Dockerfile.hf created
- [x] start.sh created
- [x] docker-compose.hf.yml created
- [x] .dockerignore configured

### API Credentials
- [x] OPENROUTER_API_KEY set in ~/.hermes/.env
  - Key: `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`
  - Provider: OpenRouter (https://openrouter.ai)
- [x] TELEGRAM_BOT_TOKEN set in ~/.hermes/.env
  - Token: `8813790386:AAGeDVV6TIjvUkV-lY7luPxkgMR8STzurH4`
  - From: @BotFather on Telegram
- [x] FIRECRAWL_API_KEY set in ~/.hermes/.env
  - Key: `fc-02e3413d720f4ca6a75002e46cb336d8`
  - Provider: Firecrawl (https://firecrawl.dev)

### Configuration Settings
- [x] Model set to: `anthropic/claude-3.5-sonnet`
- [x] Provider set to: `openrouter`
- [x] Gateway enabled: `true`
- [x] Telegram platform enabled: `true`
- [x] Firecrawl extract_backend enabled: `true`
- [x] Gateway port: `8443` (local), will be `7860` on HF Spaces

### Documentation
- [x] QUICK_START.md - Quick reference card
- [x] HF_SPACES_DEPLOYMENT.md - Detailed HF guide
- [x] DOCKER_DEPLOYMENT.md - Complete Docker guide
- [x] SETUP_COMPLETE.md - Full documentation
- [x] DEPLOYMENT_READY.md - This file
- [x] .env.example - Template with explanations

---

## Local Testing (Optional but Recommended)

### Prepare Environment
- [ ] Install Docker & Docker Compose (if not already done)
- [ ] Navigate to: `d:\Hermes Agent\hermes-agent`

### Build Docker Image
```bash
cd "d:\Hermes Agent\hermes-agent"
docker-compose -f docker-compose.hf.yml build
```
- [ ] Build completes without errors
- [ ] Image size reasonable (~2-3 GB)

### Start Container
```bash
docker-compose -f docker-compose.hf.yml up
```
- [ ] Container starts without errors
- [ ] Logs show "Starting Hermes Agent"
- [ ] Hermes gateway initializes successfully

### Test Gateway Health
```bash
curl http://localhost:7860
```
- [ ] Endpoint responds (HTTP 200 or similar)

### Test Telegram Bot
- [ ] Find your bot on Telegram
- [ ] Send message: `/start`
- [ ] Send message: `Hello!`
- [ ] Bot responds with answer from Claude 3.5 Sonnet
- [ ] No "empty response" or "thinking" artifacts

### View Logs
```bash
docker-compose -f docker-compose.hf.yml logs -f
```
- [ ] No errors or warnings
- [ ] Gateway processes messages normally

### Cleanup
```bash
docker-compose -f docker-compose.hf.yml down
```
- [ ] Container stops cleanly
- [ ] Volumes preserved (~/data)

---

## Deploy to Hugging Face Spaces

### Create HF Space
- [ ] Go to https://huggingface.co/spaces
- [ ] Click "Create new Space"
- [ ] Fill in:
  - [ ] Name: `hermes-agent` (or your choice)
  - [ ] License: MIT
  - [ ] SDK: Docker
  - [ ] Visibility: Public/Private (your choice)
- [ ] Click "Create"

### Add Secrets to HF Space
- [ ] Go to Space Settings → Repository secrets
- [ ] Add: `OPENROUTER_API_KEY`
  - [ ] Value: `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`
- [ ] Add: `TELEGRAM_BOT_TOKEN`
  - [ ] Value: `8813790386:AAGeDVV6TIjvUkV-lY7luPxkgMR8STzurH4`
- [ ] Add: `FIRECRAWL_API_KEY`
  - [ ] Value: `fc-02e3413d720f4ca6a75002e46cb336d8`

### Push Files to HF Space
```bash
git clone https://huggingface.co/spaces/YOUR_USERNAME/hermes-agent
cd hermes-agent
cp /path/to/hermes-agent/Dockerfile.hf ./Dockerfile
cp /path/to/hermes-agent/start.sh ./start.sh
cp /path/to/hermes-agent/.dockerignore ./.dockerignore
git add .
git commit -m "Deploy Hermes Agent"
git push
```
- [ ] Clone completes
- [ ] Files copied successfully
- [ ] Git push completes

### Monitor Build
- [ ] Go to HF Space → Logs tab
- [ ] Watch build progress
- [ ] Build completes successfully (~5-10 minutes)
- [ ] No build errors

### Test Deployed Bot
- [ ] Find your bot on Telegram
- [ ] Send: `/start`
- [ ] Receive: Bot welcome message or status
- [ ] Send: `What is 2+2?`
- [ ] Receive: Correct response from Claude
- [ ] Send: `Hello, who are you?`
- [ ] Receive: Relevant response (no empty/null responses)

### Verify Logs
- [ ] HF Space → Logs tab
- [ ] No authentication errors
- [ ] No API errors
- [ ] Messages processed normally

---

## Post-Deployment Verification

### Gateway Functionality
- [ ] Telegram messages → Gateway → Agent → Response flow working
- [ ] Response time reasonable (~5-30 seconds)
- [ ] Multiple sequential messages work without issues
- [ ] No duplicate responses or lost messages

### Model Behavior
- [ ] Responses coherent and relevant
- [ ] No hallucinations or nonsensical output
- [ ] Model: Claude 3.5 Sonnet active (check from `/model` command if available)
- [ ] Web extraction working (try: "Summarize https://example.com")

### Persistence
- [ ] Conversation history maintained across messages
- [ ] Session data persists across bot restarts
- [ ] Skills/configuration survive container restarts

### Performance
- [ ] First message: ~15-30 seconds
- [ ] Subsequent messages: ~5-10 seconds
- [ ] No timeout errors (should be <30s per message)
- [ ] No out-of-memory errors

---

## Troubleshooting Checklist

### Bot Not Responding

**Step 1**: Verify API Keys
```bash
# In HF Space or locally
echo $OPENROUTER_API_KEY
echo $TELEGRAM_BOT_TOKEN
echo $FIRECRAWL_API_KEY
```
- [ ] All three should output values
- [ ] OPENROUTER_API_KEY should start with `sk-or-v1-`
- [ ] TELEGRAM_BOT_TOKEN should be digits:ABC format

**Step 2**: Check API Key Validity
- [ ] Go to https://openrouter.ai/settings/credits
- [ ] Verify account has credits remaining
- [ ] Verify no API key restrictions

**Step 3**: Check Logs
```bash
# Local: view container logs
docker-compose -f docker-compose.hf.yml logs

# HF Spaces: go to Space → Logs tab
```
- [ ] Look for "API call failed"
- [ ] Look for "Authentication error"
- [ ] Look for "HTTP 401" or "HTTP 403"

**Step 4**: Verify Telegram Bot
- [ ] Find bot username on @BotFather
- [ ] Verify bot is active in @BotFather
- [ ] Try sending message to bot

### Container Won't Start

**Check Docker build**:
```bash
docker-compose -f docker-compose.hf.yml build --no-cache
```
- [ ] Build completes without errors
- [ ] Check for dependency conflicts

**Check start.sh**:
```bash
docker run -it hermes-agent-hf bash
bash /app/start.sh
```
- [ ] Script runs without errors
- [ ] Config file generated
- [ ] Environment variables set

### Empty or Nonsensical Responses

**Check model**:
- [ ] Verify model is `anthropic/claude-3.5-sonnet` (not thinking model)
- [ ] Check in `config.yaml`: `model.default: anthropic/claude-3.5-sonnet`

**Check response length**:
- [ ] Messages should have visible text (not just reasoning)
- [ ] Responses shouldn't be empty or just `...`

**Solution**: Update model in config and restart

### Idle Timeout Issues (HF Spaces Free Tier)

**Problem**: Space restarts after 24 hours of inactivity
**Solution**: Send bot messages regularly or upgrade to Paid tier

### API Rate Limits

**Check OpenRouter limits**:
- [ ] Go to https://openrouter.ai/settings/limits
- [ ] Verify no rate limit exceeded
- [ ] Check remaining quota

**If rate limited**: Wait for reset or upgrade account

---

## Performance Optimization

### For Faster Responses
- [ ] Switch to faster model: `meta-llama/llama-3.1-405b`
- [ ] Reduce `agent.max_turns` in config.yaml
- [ ] Disable unused tools/skills

### For Better Results
- [ ] Keep current model (Claude 3.5 Sonnet)
- [ ] Enable caching for common questions
- [ ] Fine-tune model choice for your use case

### For Cost Savings
- [ ] Switch to Llama 3.1 405B (free tier)
- [ ] Set response length limits
- [ ] Cache frequently asked questions

---

## Maintenance

### Regular Checks
- [ ] Weekly: Check HF Space logs for errors
- [ ] Monthly: Verify OpenRouter credits
- [ ] Monthly: Review conversation logs
- [ ] Quarterly: Update model if new versions available

### Backups
- [ ] Backup ~/.hermes/ locally or to cloud storage
- [ ] Save conversation history periodically
- [ ] Document any customizations made

### Updates
- [ ] Check for Hermes Agent updates: `git pull origin main`
- [ ] Check for dependency updates: `pip list --outdated`
- [ ] Test updates locally before deploying to HF Spaces

---

## Sign-Off

- [x] All files created and configured
- [x] API credentials set and verified
- [x] Configuration validated
- [x] Documentation complete
- [x] Ready for deployment

**Status**: ✅ **READY FOR DEPLOYMENT**

**Next Steps**: Follow "Deploy to Hugging Face Spaces" section above

---

## Support Resources

- **Hermes Documentation**: https://hermes-agent.nousresearch.com
- **OpenRouter API**: https://openrouter.ai/docs
- **Telegram Bot API**: https://core.telegram.org/bots
- **HF Spaces Help**: https://huggingface.co/docs/hub/spaces
- **Docker Docs**: https://docs.docker.com

---

**Good luck with your deployment!** 🚀✨
