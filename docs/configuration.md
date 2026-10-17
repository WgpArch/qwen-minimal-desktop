# Configuration

On first run the app creates `~/.config/qwen-desktop/config.json`.

!!! tip "Security First"
    This file is automatically chmod 600 (owner-only) to protect your API keys.

| Key | Default | Description |
|---|---|---|
| backend | ollama | ollama, dashscope, or kimi |
| system_prompt | You are Qwen... | system instructions |
| num_ctx | 2048 | context window (tokens) |
| max_history_messages | 24 | past messages sent as context |
| ollama_url | http://127.0.0.1:11434 | local Ollama server |
| ollama_model | qwen2.5:1.5b | text model |
| ollama_vision_model | qwen2.5vl:3b | vision model for screenshots |
| ollama_threads | 2 | CPU threads (lower = cooler) |
| kimi_base_url | https://api.moonshot.ai/v1 | Moonshot endpoint |
