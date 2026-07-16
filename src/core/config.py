import tomllib
from pathlib import Path

config = None

def init_config():
    global config
    config_path = Path(__file__).parent.parent.parent / "config.toml"
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

def get_config():
    global config
    if config is not None:
        return config
    else:
        init_config()
        return config

def reload_config():
    global config
    config = None
    init_config()
