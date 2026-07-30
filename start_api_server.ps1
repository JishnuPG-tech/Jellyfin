# ── API Keys for model registry (set these to your actual keys) ──
$env:GEMINI_API_KEY        = "AIzaSyA7o6gxdNNU4L5uvEERlYzk_xsHW3caiH8"
# Set GROQ_API_KEY in ~/.hermes/.env or uncomment below:
# $env:GROQ_API_KEY          = "gsk_your_groq_key"
# $env:COPILOT_GITHUB_TOKEN = "ghu_your_copilot_token"  # uncomment if using Copilot models

# Disable API key auth — the app doesn't send one
$env:API_SERVER_KEY = ""

# ── Optional: override all models with a single provider (legacy mode) ──
# $env:API_SERVER_PROVIDER    = "gemini"
# $env:API_SERVER_MODEL_NAME = "gemini-2.5-flash"
# $env:API_SERVER_API_KEY    = "AIzaSyA7o6gxdNNU4L5uvEERlYzk_xsHW3caiH8"
# $env:API_SERVER_BASE_URL   = "https://generativelanguage.googleapis.com/v1beta"

python hermes_app_api.py
