#!/bin/bash

# Hermes Agent - Hugging Face Spaces Startup Script

set -e

echo "🚀 Starting Hermes Agent on Hugging Face Spaces..."

# Create .hermes directory if it doesn't exist
mkdir -p $HERMES_HOME

# Create .env file from environment variables
SPACE_DOMAIN=${SPACE_HOST:-"jishnupg-myhermesagent.hf.space"}
TELEGRAM_WEBHOOK_SECRET="$(openssl rand -hex 32)"

cat > $HERMES_HOME/.env << EOF
OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY}
GITHUB_TOKEN=${GITHUB_TOKEN}
COPILOT_GITHUB_TOKEN=${COPILOT_GITHUB_TOKEN}
GATEWAY_ALLOW_ALL_USERS=true
TELEGRAM_WEBHOOK_URL=https://${SPACE_DOMAIN}/telegram
TELEGRAM_WEBHOOK_PORT=7860
TELEGRAM_WEBHOOK_SECRET=${TELEGRAM_WEBHOOK_SECRET}
EOF

echo "✓ Environment configured"

# Create or update config.yaml with the correct model configuration
cat > $HERMES_HOME/config.yaml << 'YAML'
model:
  default: claude-haiku-4.5
  provider: copilot
  base_url: https://api.githubcopilot.com
  api_mode: anthropic_messages

agent:
  max_turns: 90
  gateway_timeout: 1800

terminal:
  backend: local
  cwd: .

web:
  extract_backend: firecrawl

telegram:
  reactions: false
  channel_prompts: {}
  allowed_chats: ''

gateway:
  enabled: true
  platforms:
    telegram:
      enabled: true
  host: 0.0.0.0
  port: 7860

display:
  show_cost: false
  skin: default

logging:
  level: INFO
YAML
echo "✓ Config created"

echo "✓ Configuration ready"
echo ""
echo "🔗 Hermes Agent is starting..."
echo "   📱 Telegram Bot: Configured"
echo "   🤖 Model: Claude Haiku 4.5 via GitHub Copilot"
echo "   🌐 Web Extraction: Firecrawl"
echo ""

# Start the Hermes gateway
cd /app
hermes gateway run
