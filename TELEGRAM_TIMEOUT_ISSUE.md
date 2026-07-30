# Telegram Connection Timeout on HF Spaces

## Issue
The gateway is running successfully but Telegram connection times out:
```
ERROR gateway.run: ✗ telegram error: telegram connect timed out after 30s
WARNING gateway.platforms.telegram: [Telegram] Connect attempt 1/8 failed: Timed out — retrying in 1s
```

## Root Cause
HF Spaces containers appear to have limited outbound network connectivity to Telegram's API servers. This is likely a:
1. Network restriction policy on HF Spaces
2. Firewall/proxy blocking telegram.org connections
3. DNS resolution issue

## Solutions to Try

### Option 1: Use Webhook Mode (Recommended for HF Spaces)
Instead of polling Telegram's API (current approach), use webhook mode where Telegram pushes updates to your app:

```yaml
gateway:
  enabled: true
  platforms:
    telegram:
      enabled: true
      webhook_url: "https://huggingface.co/spaces/Jishnupg/MyHermesAgent/webhook"
      webhook_port: 7860
```

This requires:
- HF Spaces to expose HTTPS port 7860 (usually enabled by default)
- Telegram webhook registration

### Option 2: Test Network Connectivity
Add diagnostics to verify if outbound connections work:

```bash
# Test DNS resolution
nslookup api.telegram.org

# Test connection to Telegram
curl -v https://api.telegram.org/

# Test OpenRouter (AI provider) connectivity  
curl -v https://openrouter.ai/api/v1
```

### Option 3: Check HF Spaces Network Policy
- Visit: https://huggingface.co/spaces/Jishnupg/MyHermesAgent/settings
- Check if there are network restrictions or firewall settings
- Look for egress proxy configuration

### Option 4: Use HTTP Proxy (if HF Spaces requires it)
If HF Spaces requires outbound traffic through a proxy:

```yaml
# Add to config.yaml
network:
  proxy: "http://proxy-address:port"
```

## What's Working
✅ Gateway is running (hermes gateway run)
✅ Config loading correctly
✅ Model configuration (Claude 3.5 Sonnet)
✅ Firecrawl web extraction backend
✅ Open access enabled (GATEWAY_ALLOW_ALL_USERS=true)

## Next Steps
1. **Try webhook mode** - best fit for HF Spaces' architecture
2. **Test outbound connectivity** from the container
3. **Check HF Spaces logs** for network policy violations
4. **Consider alternative hosting** - VPS (DigitalOcean, Linode) allows unlimited outbound connections

## Alternative: Local Testing
The setup works perfectly locally. To test:

```bash
docker-compose -f docker-compose.hf.yml up
# Should connect to Telegram successfully on local machine
```

If it works locally but not on HF Spaces, that confirms it's an HF Spaces network policy issue.

---
**Status**: Gateway operational, Telegram connectivity issue needs investigation
**Next Action**: Implement webhook mode for HF Spaces compatibility
