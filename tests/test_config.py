"""Unit tests for core/config.py — from_file() classmethod."""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.config import Config, _FLOAT_FIELDS, _INT_FIELDS


@pytest.fixture()
def minimal_json(tmp_path: Path) -> Path:
    """Config JSON with only a subset of fields."""
    data = {"imap_host": "smtp.example.com", "imap_port": "465", "imap_user": "user@example.com"}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data))
    return p


@pytest.fixture()
def full_json(tmp_path: Path) -> Path:
    """Config JSON with all known fields."""
    data = {
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_user": "test@example.com",
        "imap_pass": "secret",
        "anthropic_api_key": "sk-test",
        "slack_bot_token": "xoxb-test",
        "slack_app_token": "xapp-test",
        "slack_channel": "#test",
        "approval_channel": "#approvals",
        "attachment_dir": "/tmp/attachments",
        "pipeline_interval_seconds": 120,
        "pipeline_batch_size": 5,
        "min_confidence_threshold": 0.75,
    }
    p = tmp_path / "full_config.json"
    p.write_text(json.dumps(data))
    return p


class TestFromFile:
    def test_basic_load(self, minimal_json: Path) -> None:
        cfg = Config.from_file(str(minimal_json))
        assert cfg.imap_host == "smtp.example.com"
        assert cfg.imap_user == "user@example.com"

    def test_int_coercion_from_string(self, minimal_json: Path) -> None:
        """imap_port is stored as string "465" in JSON; must come out as int."""
        cfg = Config.from_file(str(minimal_json))
        assert cfg.imap_port == 465
        assert isinstance(cfg.imap_port, int)

    def test_int_coercion_from_number(self, full_json: Path) -> None:
        cfg = Config.from_file(str(full_json))
        assert cfg.imap_port == 993
        assert isinstance(cfg.imap_port, int)
        assert cfg.pipeline_interval_seconds == 120
        assert cfg.pipeline_batch_size == 5

    def test_float_coercion(self, full_json: Path) -> None:
        cfg = Config.from_file(str(full_json))
        assert cfg.min_confidence_threshold == pytest.approx(0.75)
        assert isinstance(cfg.min_confidence_threshold, float)

    def test_unknown_keys_ignored(self, tmp_path: Path) -> None:
        """Extra keys in JSON must not raise."""
        data = {"imap_host": "h", "nonexistent_field": "boom", "also_fake": 42}
        p = tmp_path / "c.json"
        p.write_text(json.dumps(data))
        cfg = Config.from_file(str(p))
        assert cfg.imap_host == "h"
        assert not hasattr(cfg, "nonexistent_field")

    def test_bad_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{ not valid json }")
        with pytest.raises(json.JSONDecodeError):
            Config.from_file(str(p))

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            Config.from_file("/nonexistent/path/config.json")

    def test_file_wins_over_env(self, tmp_path: Path) -> None:
        """JSON file value must override a pre-existing env var (file > env)."""
        data = {"imap_port": 8993}
        p = tmp_path / "c.json"
        p.write_text(json.dumps(data))
        with patch.dict(os.environ, {"IMAP_PORT": "1234"}):
            cfg = Config.from_file(str(p))
        assert cfg.imap_port == 8993, "file value must win over env var"

    def test_env_fallback_for_missing_fields(self, tmp_path: Path) -> None:
        """Fields absent from JSON must fall back to env var value."""
        data = {"imap_host": "file-host"}
        p = tmp_path / "c.json"
        p.write_text(json.dumps(data))
        with patch.dict(os.environ, {"IMAP_PORT": "8143"}):
            cfg = Config.from_file(str(p))
        assert cfg.imap_port == 8143

    def test_int_fields_constant(self) -> None:
        assert "imap_port" in _INT_FIELDS
        assert "pipeline_interval_seconds" in _INT_FIELDS
        assert "pipeline_batch_size" in _INT_FIELDS
        assert "attachment_max_file_bytes" in _INT_FIELDS
        assert "attachment_max_mail_bytes" in _INT_FIELDS

    def test_float_fields_constant(self) -> None:
        assert "min_confidence_threshold" in _FLOAT_FIELDS


class TestCmdStartWithConfig:
    def test_cmd_start_uses_from_file_when_config_arg_given(self, full_json: Path) -> None:
        """cmd_start must call Config.from_file() when --config is passed."""
        from daemon.cli import cmd_start

        args = MagicMock()
        args.config = str(full_json)
        args.pid_file = None
        args.verbose = False

        mock_daemon = MagicMock()
        mock_daemon.run.return_value = 0

        with (
            patch("core.config.Config.from_file", return_value=MagicMock()) as mock_ff,
            patch("daemon.main_loop.MailDaemon", return_value=mock_daemon),
        ):
            result = cmd_start(args)

        mock_ff.assert_called_once_with(str(full_json))
        assert result == 0

    def test_cmd_start_uses_from_env_when_no_config(self) -> None:
        """cmd_start must call Config.from_env() when --config is not passed."""
        from daemon.cli import cmd_start

        args = MagicMock()
        args.config = None
        args.pid_file = None
        args.verbose = False

        mock_daemon = MagicMock()
        mock_daemon.run.return_value = 0

        with (
            patch("core.config.Config.from_env", return_value=MagicMock()) as mock_fe,
            patch("daemon.main_loop.MailDaemon", return_value=mock_daemon),
        ):
            result = cmd_start(args)

        mock_fe.assert_called_once()
        assert result == 0
