# Keeping Older Hardware Cool

Local inference defaults to all cores plus max turbo: on old laptops that means 90 C, throttling, and jet-engine fans. Three dials fix it.

## 1. App-side thread limit

In `~/.config/qwen-desktop/config.json`:

    "ollama_threads": 2

## 2. Pin Ollama to two physical cores

Check topology (0,4 means siblings sharing one core):

    cat /sys/devices/system/cpu/cpu0/topology/thread_siblings_list

Then run:

    sudo systemctl edit ollama

and add:

    [Service]
    CPUAffinity=0 1

then apply:

    sudo systemctl daemon-reload
    sudo systemctl restart ollama

## 3. Cap turbo (optional)

    sudo cpupower frequency-set -u 2200000

Persistent across reboots: edit /etc/default/cpupower and set max_freq="2.2GHz".

## Result

2 threads + 2 cores + 2.2 GHz cap = silent fans and 65-75 C during generation on a 2012 i7.
