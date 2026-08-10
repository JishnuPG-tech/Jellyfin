#!/bin/bash
# Hermes Agent - Push to Hugging Face Spaces

set -e

# Configuration
HF_USERNAME="${HF_USERNAME:-}"
HF_SPACE_NAME="${HF_SPACE_NAME:-hermes-agent}"
HF_TOKEN="${HF_TOKEN:-}"
REPO_DIR="$PWD"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 Hermes Agent - Hugging Face Spaces Deployment${NC}"
echo ""

# Check required credentials
if [ -z "$HF_TOKEN" ]; then
    echo -e "${RED}❌ Error: HF_TOKEN not set${NC}"
    echo ""
    echo "To deploy to Hugging Face Spaces, you need:"
    echo "1. A Hugging Face account (https://huggingface.co)"
    echo "2. An access token (https://huggingface.co/settings/tokens)"
    echo "3. An existing Space (or we'll create one)"
    echo ""
    echo "Usage:"
    echo "  export HF_TOKEN='hf_xxxxxxxxxx'"
    echo "  export HF_USERNAME='your_username'"
    echo "  export HF_SPACE_NAME='hermes-agent'"
    echo "  bash deploy-hf.sh"
    exit 1
fi

if [ -z "$HF_USERNAME" ]; then
    echo -e "${RED}❌ Error: HF_USERNAME not set${NC}"
    echo ""
    echo "Set your Hugging Face username:"
    echo "  export HF_USERNAME='your_username'"
    exit 1
fi

echo -e "${YELLOW}📋 Configuration:${NC}"
echo "  Username: $HF_USERNAME"
echo "  Space: $HF_SPACE_NAME"
echo "  Token: ${HF_TOKEN:0:20}... (hidden)"
echo ""

# Login to HF
echo -e "${YELLOW}🔑 Logging into Hugging Face...${NC}"
echo "$HF_TOKEN" | hf auth login --token-only

# Verify login
echo -e "${YELLOW}✓ Verifying authentication...${NC}"
hf auth whoami

# Check if Space exists or create it
echo ""
echo -e "${YELLOW}📦 Setting up Space repository...${NC}"

SPACE_REPO_URL="https://huggingface.co/spaces/$HF_USERNAME/$HF_SPACE_NAME"
CLONE_DIR="/tmp/hermes-space-$$"

# Clone or create
if git ls-remote "$SPACE_REPO_URL" &>/dev/null; then
    echo "✓ Space already exists: $SPACE_REPO_URL"
    git clone "$SPACE_REPO_URL" "$CLONE_DIR"
else
    echo "⚠️  Space not found. Please create it first at:"
    echo "   https://huggingface.co/spaces"
    echo ""
    echo "Instructions:"
    echo "  1. Go to https://huggingface.co/spaces"
    echo "  2. Click 'Create new Space'"
    echo "  3. Fill in:"
    echo "     - Owner: $HF_USERNAME"
    echo "     - Space name: $HF_SPACE_NAME"
    echo "     - License: MIT"
    echo "     - Space SDK: Docker"
    echo "  4. Click 'Create'"
    echo "  5. Then run this script again"
    exit 1
fi

cd "$CLONE_DIR"

# Copy deployment files
echo ""
echo -e "${YELLOW}📋 Copying deployment files...${NC}"
cp "$REPO_DIR/Dockerfile.hf" ./Dockerfile || echo "⚠️  Dockerfile.hf not found"
cp "$REPO_DIR/start.sh" ./start.sh || echo "⚠️  start.sh not found"
cp "$REPO_DIR/.dockerignore" ./.dockerignore || echo "⚠️  .dockerignore not found"

chmod +x start.sh

# Verify files exist
if [ ! -f "Dockerfile" ]; then
    echo -e "${RED}❌ Dockerfile not found in destination${NC}"
    exit 1
fi

echo "✓ Deployment files copied"

# Add and commit
echo ""
echo -e "${YELLOW}📝 Committing files...${NC}"
git add .
git commit -m "Deploy Hermes Agent with Docker

- Dockerfile.hf: Optimized for HF Spaces
- start.sh: Auto-configuration script
- .dockerignore: Build optimization

Telegram bot gateway enabled with:
- Claude 3.5 Sonnet LLM
- Firecrawl web extraction
- OpenRouter API integration"

# Push
echo ""
echo -e "${YELLOW}🚀 Pushing to Hugging Face Spaces...${NC}"
git push

echo ""
echo -e "${GREEN}✅ Deployment Complete!${NC}"
echo ""
echo "Your Space is now building!"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "  1. Go to: $SPACE_REPO_URL"
echo "  2. Wait for build to complete (5-10 minutes)"
echo "  3. Watch logs: Click 'Logs' tab"
echo "  4. After build, your Telegram bot will be live!"
echo ""
echo -e "${YELLOW}Add Secrets to HF Space:${NC}"
echo "  1. Go to: $SPACE_REPO_URL/settings"
echo "  2. Add Repository secrets:"
echo "     - OPENROUTER_API_KEY: (your key)"
echo "     - TELEGRAM_BOT_TOKEN: (your token)"
echo "     - FIRECRAWL_API_KEY: (your key)"
echo ""
echo "🎉 Done!"

# Cleanup
cd /
rm -rf "$CLONE_DIR"
