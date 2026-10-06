# qwen-minimal-desktop

A single-file, zero-dependency GTK4 chat client for Qwen and Kimi. Built specifically to run efficiently on older hardware (like 2012-era Intel laptops) without melting your CPU or spinning up fans.

## Features
- **Single File Architecture**: The entire application is one plain-text Python script. No compiled binaries, no obfuscation, no `pip install` bloat.
- **Multi-Backend Support**: Seamlessly switch between local Ollama, Qwen Cloud (DashScope), and Kimi (Moonshot) via `~/.config/qwen-desktop/config.json`.
- **Streaming Responses**: Tokens appear live as they are generated.
- **Project Management**: Kimi-style sidebar with auto-naming, archiving, and deletion.
- **Smart Import/Export**: Automatically watches `~/Downloads` for Qwen web/Kimi exports and imports them with proper titles. Bulk import/export supported.
- **File & Screenshot Attachments**: Attach code files (inlined) or screenshots (routed to vision models).
- **Thermal Awareness**: Built-in thread limiting (`num_thread`) to keep older CPUs cool during local inference.

## Requirements
- **System**: `python` (3.10+), `python-gobject`, `gtk4`
- **Backend (Pick one)**:
  - *Local*: `ollama` + a pulled model (e.g., `qwen2.5:1.5b`)
  - *Cloud*: A DashScope or Moonshot API key in `config.json`

## Installation

### From AUR (Recommended for Arch users)
```bash
trizen -S qwen-minimal-desktop
yay -S qwen-minimal-desktop
paru -S qwen-minimal-desktop
```

### Manual Installation
```bash
git clone https://github.com/WgpArch/qwen-minimal-desktop.git
cd qwen-minimal-desktop
sudo install -Dm755 main.py /usr/bin/qwen-minimal-desktop
sudo install -Dm644 qwen-minimal-desktop.desktop /usr/share/applications/qwen-minimal-desktop.desktop
```

## Configuration
On first run, the app creates `~/.config/qwen-desktop/config.json`.
- **backend**: `ollama`, `dashscope`, or `kimi`
- **ollama_model**: e.g., `qwen2.5:1.5b`
- **ollama_threads**: Default `2`. Limits CPU usage on older hardware.
- **auto_import_dir**: Default `~/Downloads`.

## Keeping Older Hardware Cool
Local AI inference can spike CPU temperatures. If you are on an older laptop (e.g., Ivy Bridge i7), use the built-in `"ollama_threads": 2` setting in config.json. 

For maximum thermal control, pair this with a custom `cool-mode` script that sets your CPU governor to `powersave` and pins the Ollama systemd service to specific cores via `CPUAffinity`:

```bash
# In /etc/systemd/system/ollama.service.d/override.conf
[Service]
CPUAffinity=0 1
```

## Security & Transparency
Like all my tools, this is written entirely in plain-text Python. You can read the single `main.py` file to verify exactly what it does. It makes no network requests except to your chosen backend (Ollama/DashScope/Kimi) and stores your chat history locally in `~/.local/share/qwen-desktop/chats.db`.

## License
GPL-3.0
