"""Validated configuration independent of the current working directory."""

from .loader import AppConfig, ConfigError, load_config

__all__ = ["AppConfig", "ConfigError", "load_config"]
