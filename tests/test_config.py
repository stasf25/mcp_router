"""Тесты конфигурации: загрузка, резолвинг env-переменных, атомарное сохранение."""

from __future__ import annotations

import pytest

from mcp_router.config import ConfigError, atomic_save, config_to_dict, load_config


def write_yaml(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_load_basic_config(tmp_path):
    p = write_yaml(
        tmp_path,
        """
servers:
  - name: playwright
    url: http://playwright:3000/mcp
    enabled: true
    timeout: 30
groups:
  - name: web
    tools: [playwright.navigate]
    enabled: true
""",
    )
    cfg = load_config(p)
    assert cfg.server("playwright").url == "http://playwright:3000/mcp"
    assert cfg.group("web").tools == ["playwright.navigate"]


def test_env_header_substitution(tmp_path):
    p = write_yaml(
        tmp_path,
        """
servers:
  - name: s
    url: http://s/mcp
    headers:
      - X-API-Key: ${MY_KEY_VAR}
""",
    )
    cfg = load_config(p, env={"MY_KEY_VAR": "secret-value"})
    assert cfg.server("s").header_map == {"X-API-Key": "secret-value"}


def test_env_substitution_in_url(tmp_path):
    p = write_yaml(
        tmp_path,
        """
servers:
  - name: s
    url: https://s/mcp?token=${TOKEN}
""",
    )
    cfg = load_config(p, env={"TOKEN": "abc123"})
    assert cfg.server("s").url == "https://s/mcp?token=abc123"


def test_missing_env_raises(tmp_path):
    p = write_yaml(
        tmp_path,
        """
servers:
  - name: s
    url: http://s/mcp
    headers:
      - Authorization: Bearer ${MISSING_VAR}
""",
    )
    with pytest.raises(ConfigError):
        load_config(p, env={})


def test_plain_header_no_env(tmp_path):
    p = write_yaml(
        tmp_path,
        """
servers:
  - name: s
    url: http://s/mcp
    headers:
      - X-Static: value
""",
    )
    cfg = load_config(p, env={})
    assert cfg.server("s").header_map == {"X-Static": "value"}


def test_duplicate_server_names_rejected(tmp_path):
    p = write_yaml(
        tmp_path,
        """
servers:
  - name: dup
    url: http://a/mcp
  - name: dup
    url: http://b/mcp
""",
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_invalid_url_rejected(tmp_path):
    p = write_yaml(
        tmp_path,
        """
servers:
  - name: s
    url: not-a-url
""",
    )
    with pytest.raises(ConfigError):
        load_config(p)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_atomic_save_roundtrip(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("servers: []\ngroups: []\n")
    atomic_save(p, {"servers": [], "groups": [{"name": "g", "tools": ["a.b"], "enabled": True}]})
    reloaded = load_config(p)
    assert reloaded.group("g").tools == ["a.b"]


def test_config_to_dict_excludes_none(tmp_path):
    p = write_yaml(tmp_path, "servers: []\ngroups: []\n")
    cfg = load_config(p)
    d = config_to_dict(cfg)
    assert "servers" in d
