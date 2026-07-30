# Dockerfile optimized for Hugging Face Spaces
# Hermes Agent - Telegram Bot Gateway

FROM python:3.11-slim

WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HERMES_HOME=/data \
    PATH="/app/.venv/bin:${PATH}"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    wget \
    build-essential \
    libffi-dev \
    ripgrep \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Clone the hermes-agent repository from GitHub
RUN git clone https://github.com/NousResearch/hermes-agent.git /app-src && \
    cp -r /app-src/* /app/ && \
    rm -rf /app-src

# Create data directory for persistent storage
RUN mkdir -p /data

# Create custom HF-specific startup script
RUN cat > /app/start.sh << 'EOF' && chmod +x /app/start.sh
#!/bin/bash

set -e

echo "🚀 Starting Hermes Agent on Hugging Face Spaces..."

mkdir -p $HERMES_HOME

cat > $HERMES_HOME/.env << ENVEOF
OPENROUTER_API_KEY=${OPENROUTER_API_KEY}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
FIRECRAWL_API_KEY=${FIRECRAWL_API_KEY}
GITHUB_TOKEN=${GITHUB_TOKEN}
COPILOT_GITHUB_TOKEN=${COPILOT_GITHUB_TOKEN}
GATEWAY_ALLOW_ALL_USERS=true
ENVEOF

echo "✓ Environment configured"

if [ ! -f "$HERMES_HOME/config.yaml" ]; then
    cat > $HERMES_HOME/config.yaml << 'YAML'
model:
  default: anthropic/claude-3.5-sonnet
  provider: openrouter
  base_url: https://openrouter.ai/api/v1
  api_mode: chat_completions
agent:
  max_turns: 90
  gateway_timeout: 1800
terminal:
  backend: null
  cwd: /tmp
web:
  extract_backend: firecrawl
telegram:
  reactions: false
  channel_prompts: {}
  allowed_chats: ''
  webhook_mode: true
  webhook_base_url: "https://huggingface.co/spaces/Jishnupg/MyHermesAgent"
  webhook_path: "/webhook"
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
else
    echo "✓ Config already exists"
fi

echo "✓ Configuration ready"
echo ""
echo "🔗 Hermes Agent is starting..."
echo "   📱 Telegram Bot: Configured (Webhook mode)"
echo "   🤖 Model: Claude 3.5 Sonnet via OpenRouter"
echo "   🌐 Web Extraction: Firecrawl"
echo ""

# Register Telegram webhook if token is set
if [ ! -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "📡 Registering Telegram webhook..."
    WEBHOOK_URL="https://huggingface.co/spaces/Jishnupg/MyHermesAgent/webhook"
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
        -d "url=${WEBHOOK_URL}" \
        -d "drop_pending_updates=true" > /dev/null 2>&1 || echo "⚠ Webhook registration may have failed (network issue)"
    echo "✓ Webhook registered"
fi

echo ""
cd /app
hermes gateway run
EOF

# Install Python dependencies
RUN cd /app && \
    pip install --upgrade pip setuptools wheel && \
    pip install -e .

# Create persistent volume for Hermes data
VOLUME ["/data"]

# Expose port for Hugging Face Spaces
EXPOSE 7860

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:7860/ || exit 1

# Run the gateway
CMD ["/bin/bash", "/app/start.sh"]
