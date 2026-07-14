"""Tests for the worktrees_hives subprocess bridge."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from worktrees_hives.bridge import WhClient, _resolve_wh_binary
from worktrees_hives.contract import (
    ErrorResponse,
    Response,
    SuccessResponse,
    classify,
)
from worktrees_hives.errors import (
    PolicyError,
    WhBinaryNotFoundError,
    WhJsonDecodeError,
    WhProcessError,
    WhSchemaError,
)

# ---------------------------------------------------------------------------
# _resolve_wh_binary
# ---------------------------------------------------------------------------


class TestResolveWhBinary:
    """Tests for binary resolution logic."""

    def test_explicit_path_takes_priority(self, tmp_path):
        binary = tmp_path / "wh"
        binary.write_text("#!/bin/sh")
        binary.chmod(0o755)
        result = _resolve_wh_binary(str(binary))
        assert result == str(binary)

    def test_explicit_path_missing_raises(self):
        with pytest.raises(WhBinaryNotFoundError, match="does not exist"):
            _resolve_wh_binary("/nonexistent/wh")

    def test_wh_bin_env_var(self, tmp_path, monkeypatch):
        binary = tmp_path / "wh"
        binary.write_text("#!/bin/sh")
        binary.chmod(0o755)
        monkeypatch.setenv("WH_BIN", str(binary))
        result = _resolve_wh_binary(None)
        assert result == str(binary)

    def test_wh_bin_env_var_missing_raises(self, monkeypatch):
        monkeypatch.setenv("WH_BIN", "/nonexistent/wh")
        with pytest.raises(WhBinaryNotFoundError, match="WH_BIN"):
            _resolve_wh_binary(None)

    def test_path_fallback(self, tmp_path, monkeypatch):
        binary = tmp_path / "wh"
        binary.write_text("#!/bin/sh")
        binary.chmod(0o755)
        monkeypatch.delenv("WH_BIN", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path))
        result = _resolve_wh_binary(None)
        assert result == str(binary)

    def test_no_binary_found_raises(self, monkeypatch):
        monkeypatch.delenv("WH_BIN", raising=False)
        monkeypatch.setenv("PATH", "")
        with pytest.raises(WhBinaryNotFoundError, match="not found"):
            _resolve_wh_binary(None)


# ---------------------------------------------------------------------------
# Response.from_dict validation
# ---------------------------------------------------------------------------


class TestResponseFromDict:
    """Tests for v1 envelope schema validation."""

    def _valid_envelope(self, **overrides):
        base = {
            "ok": True,
            "schema_version": 1,
            "command": "cli.bootstrap",
            "data": {},
            "error": None,
        }
        base.update(overrides)
        return base

    def test_valid_success_envelope(self):
        resp = Response.from_dict(self._valid_envelope())
        assert resp.ok is True
        assert resp.schema_version == 1
        assert resp.command == "cli.bootstrap"
        assert resp.data == {}
        assert resp.error is None

    def test_valid_error_envelope(self):
        envelope = self._valid_envelope(
            ok=False,
            error={"code": "E001", "message": "something broke"},
        )
        resp = Response.from_dict(envelope)
        assert resp.ok is False
        assert resp.error is not None
        assert resp.error.code == "E001"
        assert resp.error.message == "something broke"

    def test_non_dict_raises_schema_error(self):
        with pytest.raises(WhSchemaError, match="Expected a JSON object"):
            Response.from_dict([1, 2, 3])

    def test_missing_ok_raises(self):
        envelope = self._valid_envelope()
        del envelope["ok"]
        with pytest.raises(WhSchemaError, match="'ok'"):
            Response.from_dict(envelope)

    def test_bad_ok_type_raises(self):
        with pytest.raises(WhSchemaError, match="'ok'"):
            Response.from_dict(self._valid_envelope(ok="yes"))

    def test_bad_schema_version_type_raises(self):
        with pytest.raises(WhSchemaError, match="'schema_version'"):
            Response.from_dict(self._valid_envelope(schema_version="one"))

    def test_bad_command_type_raises(self):
        with pytest.raises(WhSchemaError, match="'command'"):
            Response.from_dict(self._valid_envelope(command=42))

    def test_bad_data_type_raises(self):
        with pytest.raises(WhSchemaError, match="'data'"):
            Response.from_dict(self._valid_envelope(data="not a dict"))

    def test_bad_error_type_raises(self):
        with pytest.raises(WhSchemaError, match="'error'"):
            Response.from_dict(self._valid_envelope(error="oops"))

    def test_error_missing_code_raises(self):
        with pytest.raises(WhSchemaError, match=r"'error\.code'"):
            Response.from_dict(self._valid_envelope(ok=False, error={"message": "x"}))

    def test_error_missing_message_raises(self):
        with pytest.raises(WhSchemaError, match=r"'error\.code'"):
            Response.from_dict(self._valid_envelope(ok=False, error={"code": "E001"}))

    def test_bool_schema_version_raises(self):
        # bool is an int subclass; must still be rejected as schema_version.
        with pytest.raises(WhSchemaError, match="'schema_version'"):
            Response.from_dict(self._valid_envelope(schema_version=True))

    def test_ok_true_with_error_raises(self):
        with pytest.raises(WhSchemaError, match="'error' must be null when ok is true"):
            Response.from_dict(
                self._valid_envelope(
                    ok=True,
                    error={"code": "E001", "message": "should not appear on success"},
                )
            )

    def test_ok_false_without_error_raises(self):
        with pytest.raises(WhSchemaError, match="'error' is required when ok is false"):
            Response.from_dict(self._valid_envelope(ok=False, error=None))

    def test_unsupported_schema_version_raises(self):
        with pytest.raises(WhSchemaError, match="Unsupported schema version"):
            Response.from_dict(self._valid_envelope(schema_version=99))


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class TestClassify:
    """Tests for the SuccessResponse / ErrorResponse discriminator."""

    def test_success(self):
        resp = Response(
            ok=True,
            schema_version=1,
            command="cli.bootstrap",
            data={"key": "value"},
            error=None,
        )
        result = classify(resp)
        assert isinstance(result, SuccessResponse)
        assert result.data == {"key": "value"}

    def test_error(self):
        from worktrees_hives.contract import ErrorData

        resp = Response(
            ok=False,
            schema_version=1,
            command="cli.some_command",
            data={},
            error=ErrorData(code="E001", message="bad"),
        )
        result = classify(resp)
        assert isinstance(result, ErrorResponse)
        assert result.error.code == "E001"


# ---------------------------------------------------------------------------
# WhClient.run (mocked subprocess)
# ---------------------------------------------------------------------------


FAKE_SUCCESS_JSON = json.dumps(
    {
        "ok": True,
        "schema_version": 1,
        "command": "cli.bootstrap",
        "data": {},
        "error": None,
    }
)

FAKE_ERROR_JSON = json.dumps(
    {
        "ok": False,
        "schema_version": 1,
        "command": "cli.some_command",
        "data": {},
        "error": {"code": "E001", "message": "something went wrong"},
    }
)


class TestWhClientRun:
    """Tests for WhClient.run() with mocked subprocess."""

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_bootstrap_success(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=FAKE_SUCCESS_JSON, stderr="")
        client = WhClient()
        result = client.bootstrap()
        assert isinstance(result, SuccessResponse)
        assert result.command == "cli.bootstrap"
        mock_run.assert_called_once()

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_error_response(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=FAKE_ERROR_JSON, stderr="")
        client = WhClient()
        result = client.run("some-command")
        assert isinstance(result, ErrorResponse)
        assert result.error.code == "E001"

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_nonzero_exit_raises_process_error(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="wh: unknown command")
        client = WhClient()
        with pytest.raises(WhProcessError, match="exited with code 2"):
            client.run("bad-cmd")

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_empty_stdout_raises_decode_error(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        client = WhClient()
        with pytest.raises(WhJsonDecodeError, match="empty output"):
            client.run()

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_invalid_json_raises_decode_error(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="not json at all", stderr="")
        client = WhClient()
        with pytest.raises(WhJsonDecodeError, match="Failed to decode"):
            client.run()

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_malformed_envelope_raises_schema_error(self, mock_resolve, mock_run):
        bad_json = json.dumps({"ok": True})
        mock_run.return_value = MagicMock(returncode=0, stdout=bad_json, stderr="")
        client = WhClient()
        with pytest.raises(WhSchemaError):
            client.run()

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_args_passed_to_subprocess(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=FAKE_SUCCESS_JSON, stderr="")
        client = WhClient()
        client.run("worktree", "create", "--issue", "123")
        call_args = mock_run.call_args
        cmd = call_args[0][0]
        assert cmd == ["/usr/bin/wh", "--json", "worktree", "create", "--issue", "123"]

    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_timeout_forwarded_to_subprocess(self, mock_resolve):
        with patch("worktrees_hives.bridge.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=FAKE_SUCCESS_JSON, stderr="")
            client = WhClient(timeout=60.0)
            client.run()
            assert mock_run.call_args[1]["timeout"] == 60.0

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_binary_not_found_during_exec(self, mock_resolve, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        client = WhClient()
        with pytest.raises(WhBinaryNotFoundError, match="Failed to execute"):
            client.run()

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_timeout_raises_process_error(self, mock_resolve, mock_run):
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(cmd="wh", timeout=30.0)
        client = WhClient()
        with pytest.raises(WhProcessError, match="timed out"):
            client.run()


class TestPolicyExitCode:
    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_exit_2_valid_error_envelope_raises_policy_error(self, mock_resolve, mock_run):
        envelope = json.dumps(
            {
                "ok": False,
                "schema_version": 1,
                "command": "worktree.create",
                "data": {},
                "error": {"code": "PathEscape", "message": "path outside sandbox"},
            }
        )
        mock_run.return_value = MagicMock(returncode=2, stdout=envelope, stderr="")
        with pytest.raises(PolicyError, match="PathEscape") as exc_info:
            WhClient().run("worktree", "create")
        assert exc_info.value.code == "PathEscape"

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_exit_2_schema_error_falls_back_to_process_error(self, mock_resolve, mock_run):
        # ok=false without error is a schema violation; exit-2 path must not
        # surface WhSchemaError — fall back to WhProcessError like #57.
        envelope = json.dumps(
            {
                "ok": False,
                "schema_version": 1,
                "command": "worktree.create",
                "data": {},
                "error": None,
            }
        )
        mock_run.return_value = MagicMock(returncode=2, stdout=envelope, stderr="policy rejection")
        with pytest.raises(WhProcessError, match="exited with code 2"):
            WhClient().run("worktree", "create")

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_exit_2_invalid_json_falls_back_to_process_error(self, mock_resolve, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2, stdout="not-json", stderr="policy rejection"
        )
        with pytest.raises(WhProcessError, match="exited with code 2"):
            WhClient().run("worktree", "create")

    @patch("worktrees_hives.bridge.subprocess.run")
    @patch("worktrees_hives.bridge._resolve_wh_binary", return_value="/usr/bin/wh")
    def test_exit_2_ok_true_with_error_falls_back_to_process_error(self, mock_resolve, mock_run):
        envelope = json.dumps(
            {
                "ok": True,
                "schema_version": 1,
                "command": "worktree.create",
                "data": {},
                "error": {"code": "X", "message": "inconsistent"},
            }
        )
        mock_run.return_value = MagicMock(returncode=2, stdout=envelope, stderr="policy")
        with pytest.raises(WhProcessError, match="exited with code 2"):
            WhClient().run("worktree", "create")
