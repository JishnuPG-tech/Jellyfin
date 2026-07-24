---
title: Opencode CLI
emoji: 🖥️
colorFrom: green
colorTo: blue
sdk: docker
app_port: 4096
pinned: false
---

# Opencode CLI Server

This Hugging Face Space hosts the backend `opencode serve` API server on port 4096.

## Connection Setup for Mobile App

To connect your OpenCode mobile client to this server:
1. Open the mobile app and go to **Settings**.
2. Under **Server URL**, enter the Space's direct domain:
   `https://jishnupg-opencode-cli.hf.space` (or your corresponding Space URL)
3. Set your **Auth Type** and credentials if you configured `OPENCODE_SERVER_USERNAME`/`OPENCODE_SERVER_PASSWORD` secrets.
