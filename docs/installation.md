# Installation

## Arch Linux (Recommended)

Use your AUR helper:

    trizen -S qwen-minimal-desktop

## Manual Installation (Any Linux)

    git clone https://github.com/WgpArch/qwen-minimal-desktop.git
    cd qwen-minimal-desktop
    sudo install -Dm755 main.py /usr/bin/qwen-minimal-desktop
    sudo install -Dm644 qwen-minimal-desktop.desktop /usr/share/applications/qwen-minimal-desktop.desktop

## Backend Requirements

1. Local (offline): install Ollama and pull a model: `ollama pull qwen2.5:1.5b`
2. Cloud: add a DashScope or Kimi API key in config.json (see Configuration).
