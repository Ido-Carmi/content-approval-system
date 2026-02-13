"""
Shared configuration loader for the confessions system.
All modules should import from here instead of defining their own load_config.
"""
import json
from pathlib import Path

CONFIG_FILE = Path("config.json")

def load_config() -> dict:
    """Load configuration from config.json"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_config(config: dict):
    """Save configuration to config.json"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
