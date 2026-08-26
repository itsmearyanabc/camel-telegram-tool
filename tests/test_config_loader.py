import os
import json
import pytest
from utils.config_loader import load_config, save_config, update_account_setting

@pytest.fixture
def mock_config_file(tmp_path):
    config_path = tmp_path / "config.json"
    # Patch the global CONFIG_FILE in config_loader
    import utils.config_loader
    original_path = utils.config_loader.CONFIG_FILE
    utils.config_loader.CONFIG_FILE = str(config_path)
    yield config_path
    utils.config_loader.CONFIG_FILE = original_path

def test_load_config_defaults(mock_config_file):
    config = load_config()
    assert config["loop_interval"] == 15
    assert isinstance(config["account_settings"], dict)

def test_save_and_load_config(mock_config_file):
    data = {"api_id": "123", "api_hash": "abc"}
    save_config(data)
    
    loaded = load_config()
    assert loaded["api_id"] == "123"
    assert loaded["api_hash"] == "abc"

def test_update_account_setting(mock_config_file):
    update_account_setting("+123", "is_loop_active", True)
    config = load_config()
    assert config["account_settings"]["123"]["is_loop_active"] is True
    
    update_account_setting("+123", "loop_interval", 25)
    config = load_config()
    assert config["account_settings"]["123"]["loop_interval"] == 25
