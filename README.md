---
title: OpenCode Serve
emoji: 🖥️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
# Re-use the persistent storage from opencode-cli so that sessions,
# conversations, providers, model configs and SQLite DB are shared.
datasets:
- Jishnupg/Opencode-Cli-storage
---
