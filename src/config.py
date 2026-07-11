import tomllib
from pathlib import Path

config = None

def init_config():
    global config
    config_path = Path(__file__).parent.parent / "config.toml"
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

def get_config():
    return config