"""
Shared configuration loader for the confessions system.
All modules should import from here instead of defining their own load_config.
"""
import json
import threading
from pathlib import Path

CONFIG_FILE = Path("config.json")
_config_lock = threading.Lock()

def load_config() -> dict:
    """Load configuration from config.json"""
    with _config_lock:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}

def save_config(config: dict):
    """Save configuration to config.json"""
    with _config_lock:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)


def comments_filter_ready(config=None) -> bool:
    """Whether the configured comment-filter engine has the credentials it needs.
    'clef' (Cloudflare Workers AI decision model) needs an account id + API token;
    the default 'openai' engine needs an OpenAI API key."""
    if config is None:
        config = load_config()
    engine = config.get('comments_filter_engine', 'openai')
    if engine == 'clef':
        return bool(config.get('cloudflare_account_id') and config.get('cloudflare_api_token'))
    return bool(config.get('openai_api_key'))


def get_auto_comment_texts(config=None) -> set:
    """Return a set of all configured auto-comment texts (used to skip them in comment filtering)."""
    if config is None:
        config = load_config()
    texts = set()
    for i in range(1, 5):
        for t in config.get(f'auto_comment_group_{i}', []):
            if t and t.strip():
                texts.add(t.strip())
        old = config.get(f'auto_comment_text_{i}', '').strip()
        if old:
            texts.add(old)
    return texts
