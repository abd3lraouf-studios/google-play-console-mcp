"""Tests for agent registration via add-mcp."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from google_play_console_mcp import install as inst
from google_play_console_mcp.errors import ValidationError


class TestUvCacheRoot:
    def test_uses_uv_cache_dir_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("UV_CACHE_DIR", "/custom/uv")
        assert inst._uv_cache_root() == Path("/custom/uv")

    def test_posix_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
        monkeypatch.setattr(inst.sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: Path("/home/x")))
        assert inst._uv_cache_root() == Path("/home/x/.cache/uv")

    def test_windows_uses_localappdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("UV_CACHE_DIR", raising=False)
        monkeypatch.setattr(inst.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
        assert inst._uv_cache_root() == Path(r"C:\Users\x\AppData\Local") / "uv" / "cache"


class TestIsEphemeral:
    def test_inside_cache_is_ephemeral(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path))
        target = tmp_path / "archive-v0" / "abc" / "bin" / "x"
        target.parent.mkdir(parents=True)
        target.touch()
        assert inst._is_ephemeral(target) is True

    def test_outside_cache_is_durable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "cache"))
        other = tmp_path / "local" / "bin" / "x"
        other.parent.mkdir(parents=True)
        other.touch()
        assert inst._is_ephemeral(other) is False

    def test_unresolvable_path_is_not_ephemeral(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path))

        def boom(_self: Path, *_a: Any, **_k: Any) -> Path:
            raise OSError("nope")

        monkeypatch.setattr(Path, "resolve", boom)
        assert inst._is_ephemeral(Path("/whatever")) is False


class TestResolveLaunchTarget:
    def test_prefer_uvx_skips_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(inst.shutil, "which", lambda _: "/should/not/be/used")
        assert inst.resolve_launch_target(prefer_uvx=True) == f"uvx {inst.PACKAGE}"

    def test_durable_console_script_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(inst.shutil, "which", lambda _: "/usr/local/bin/gpc")
        monkeypatch.setattr(inst, "_is_ephemeral", lambda _: False)
        assert inst.resolve_launch_target() == "/usr/local/bin/gpc"

    def test_ephemeral_path_falls_back_to_uvx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(inst.shutil, "which", lambda _: "/cache/uv/archive-v0/h/bin/gpc")
        monkeypatch.setattr(inst, "_is_ephemeral", lambda _: True)
        assert inst.resolve_launch_target() == f"uvx {inst.PACKAGE}"

    def test_missing_executable_falls_back_to_uvx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(inst.shutil, "which", lambda _: None)
        assert inst.resolve_launch_target() == f"uvx {inst.PACKAGE}"


class TestServerArgsAndEnv:
    def test_read_only_flag(self) -> None:
        assert inst.build_server_args(read_only=True) == ["--read-only"]
        assert inst.build_server_args() == []

    def test_no_credentials_is_empty_env(self) -> None:
        assert inst.build_server_env() == {}

    def test_credentials_resolved_to_absolute(self, tmp_path: Path) -> None:
        key = tmp_path / "key.json"
        key.write_text("{}")
        env = inst.build_server_env(credentials=key)
        assert env == {inst.CREDENTIALS_ENV: str(key.resolve())}

    def test_missing_credentials_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError) as excinfo:
            inst.build_server_env(credentials=tmp_path / "absent.json")
        assert "not found" in excinfo.value.message
        assert excinfo.value.remedy


class TestBuildAddMcpArgv:
    def test_defaults_are_global_and_named(self) -> None:
        argv = inst.build_add_mcp_argv("/bin/gpc")
        assert argv == [
            "npx",
            "-y",
            inst.ADD_MCP_SPEC,
            "/bin/gpc",
            "--name",
            inst.DEFAULT_SERVER_NAME,
            "--global",
        ]

    def test_project_scope_omits_global(self) -> None:
        assert "--global" not in inst.build_add_mcp_argv("/bin/gpc", global_scope=False)

    def test_agents_and_all_and_yes(self) -> None:
        argv = inst.build_add_mcp_argv(
            "/bin/gpc",
            agents=["cursor", "zed"],
            all_agents=True,
            yes=True,
        )
        assert "--all" in argv
        assert argv.count("--agent") == 2
        assert argv[argv.index("--agent") + 1] == "cursor"
        assert argv[-1] == "--yes"

    def test_server_flags_use_equals_form(self) -> None:
        """A bare `--args --read-only` would be parsed as add-mcp's own option."""
        argv = inst.build_add_mcp_argv("/bin/gpc", server_args=["--read-only"])
        assert "--args=--read-only" in argv
        assert "--read-only" not in argv

    def test_env_uses_equals_form(self) -> None:
        argv = inst.build_add_mcp_argv("/bin/gpc", server_env={"K": "V"})
        assert "--env=K=V" in argv

    def test_target_stays_one_token(self) -> None:
        """add-mcp splits `uvx <pkg>` itself; pre-splitting it registers npx."""
        argv = inst.build_add_mcp_argv("uvx google-play-console-mcp")
        assert "uvx google-play-console-mcp" in argv


class TestManualConfig:
    def test_single_token_target(self) -> None:
        cfg = json.loads(inst.manual_config("/bin/gpc", server_args=["--read-only"]))
        entry = cfg["mcpServers"][inst.DEFAULT_SERVER_NAME]
        assert entry == {"command": "/bin/gpc", "args": ["--read-only"]}

    def test_runner_target_splits_into_args(self) -> None:
        cfg = json.loads(inst.manual_config("uvx google-play-console-mcp"))
        entry = cfg["mcpServers"][inst.DEFAULT_SERVER_NAME]
        assert entry["command"] == "uvx"
        assert entry["args"] == ["google-play-console-mcp"]

    def test_env_included_when_present(self) -> None:
        cfg = json.loads(inst.manual_config("/bin/gpc", name="x", server_env={"K": "V"}))
        assert cfg["mcpServers"]["x"]["env"] == {"K": "V"}


class TestInstall:
    def test_missing_npx_prints_manual_config(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(inst.shutil, "which", lambda _name: None)
        assert inst.install() == 1
        out = capsys.readouterr()
        assert "Node.js is required" in out.err
        assert json.loads(out.out)["mcpServers"]

    def test_dry_run_prints_quoted_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            inst.shutil, "which", lambda name: "/usr/bin/npx" if name == "npx" else None
        )
        assert inst.install(dry_run=True) == 0
        printed = capsys.readouterr().out
        assert "'uvx google-play-console-mcp'" in printed

    def test_runs_add_mcp_and_returns_its_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 3)

        monkeypatch.setattr(inst.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(inst, "_is_ephemeral", lambda _: False)
        monkeypatch.setattr(inst.subprocess, "run", fake_run)

        assert inst.install(agents=["cursor"], read_only=True) == 3
        assert seen["argv"][0] == "/usr/bin/npx"
        assert "--args=--read-only" in seen["argv"]


class TestRunCli:
    def test_passes_flags_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def fake_install(**kwargs: Any) -> int:
            seen.update(kwargs)
            return 0

        monkeypatch.setattr(inst, "install", fake_install)
        code = inst.run_cli(
            ["-a", "cursor", "--all", "--project", "-y", "--read-only", "--uvx", "--dry-run"]
        )
        assert code == 0
        assert seen["agents"] == ["cursor"]
        assert seen["all_agents"] is True
        assert seen["global_scope"] is False
        assert seen["yes"] is True
        assert seen["read_only"] is True
        assert seen["prefer_uvx"] is True
        assert seen["dry_run"] is True

    def test_defaults_to_global_scope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}
        monkeypatch.setattr(inst, "install", lambda **kw: seen.update(kw) or 0)
        assert inst.run_cli([]) == 0
        assert seen["global_scope"] is True
        assert seen["name"] == inst.DEFAULT_SERVER_NAME

    def test_validation_error_becomes_exit_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def boom(**_kwargs: Any) -> int:
            raise ValidationError("bad key", remedy="fix it")

        monkeypatch.setattr(inst, "install", boom)
        assert inst.run_cli([]) == 2
        assert "fix it" in capsys.readouterr().err


class TestServerCliRouting:
    def test_install_subcommand_is_routed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The subcommand must not reach the server's own argument parser."""
        import google_play_console_mcp.server as server

        monkeypatch.setattr(server, "run_install_cli", lambda _argv: 7)
        with pytest.raises(SystemExit) as excinfo:
            server.main(["install", "-a", "cursor"])
        assert excinfo.value.code == 7

    def test_bare_invocation_still_starts_stdio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MCP clients launch the executable with no arguments."""
        import google_play_console_mcp.server as server

        started: dict[str, Any] = {}
        monkeypatch.setattr(server.mcp, "run", lambda **kw: started.update(kw))
        server.main([])
        assert started == {"transport": "stdio"}
