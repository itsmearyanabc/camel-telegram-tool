import json
import os
import shutil
import tempfile
from utils.logger import logger

CONFIG_FILE = "config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {
            "api_id": "", "api_hash": "", "phones": "", 
            "source_channel": "", "loop_interval": 15,
            "account_settings": {} 
        }
    
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            # Schema Integrity / Migration
            data.setdefault("account_settings", {})
            data.setdefault("loop_interval", 15)
            data.setdefault("phones", "")
            return data
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Config corruption detected, loading defaults: {e}")
        return {
            "api_id": "", "api_hash": "", "phones": "", 
            "source_channel": "", "loop_interval": 15,
            "account_settings": {} 
        }

def save_config(config):
    """
    Saves configuration atomically using a temp-file swap pattern.
    This prevents file corruption if the system crashes during a write.
    """
    fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(CONFIG_FILE))
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(config, f, indent=4)
        # Flush to disk
        os.replace(temp_path, CONFIG_FILE)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.error(f"CRITICAL: Atomic config save failed: {e}")

def update_account_setting(phone, key, value):
    config = load_config()
    p_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    settings = config.setdefault("account_settings", {}).setdefault(p_clean, {})
    
    # Only save if value actually changed to reduce IO
    if settings.get(key) != value:
        settings[key] = value
        save_config(config)
