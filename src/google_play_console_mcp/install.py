"""Register this server with MCP clients.

The wiring itself is delegated to `add-mcp <https://github.com/neon-solutions/add-mcp>`_
(Apache-2.0), which knows the config file, root key and scope rules for 21
agents — Claude Code, Claude Desktop, Cursor, VS Code, Codex, Gemini CLI,
Windsurf, Zed, Cline, Goose, OpenCode and the rest — and keeps up with them as
they drift. Reimplementing that here would mean tracking three different root
keys (``mcpServers``, ``servers``, ``context_servers``), TOML and YAML formats
alongside JSON, and per-agent scope paths, and getting it wrong writes a broken
config into a file the user did not ask us to touch.

Two things about the delegation are load-bearing, both established by running
``add-mcp`` and reading back what it wrote:

**The target must be a single token.** ``add-mcp`` treats a bare word as an npm
package and registers ``npx -y <word>``, which is wrong for a PyPI
distribution. It also folds a multi-token target into ``command`` verbatim and
leaves ``args`` empty, so ``"<path> --read-only"`` becomes an unexecutable
command string. Passing one token — an absolute path, or the ``uvx <package>``
runner form it splits correctly — is the only shape that round-trips.

**Server flags go through ``--args=``.** The equals form keeps the option
parser from reading a leading-dash value as one of its own options.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from google_play_console_mcp import keychain
from google_play_console_mcp.errors import ValidationError

EXECUTABLE = "google-play-console-mcp"
"""Console script this distribution installs."""

PACKAGE = "google-play-console-mcp"
"""Distribution name on PyPI, used for the ``uvx`` fallback form."""

DEFAULT_SERVER_NAME = "google-play-console"
"""Name the server is registered under. Short, and not the package name, so it
reads well in an agent's tool list."""

ADD_MCP_SPEC = os.environ.get("GOOGLE_PLAY_MCP_ADD_MCP_SPEC", "add-mcp@2")
"""npm spec for the delegate, major-pinned. Overridable so a site that mirrors
or pins npm packages can point this at its own copy."""

CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"


def _uv_cache_root() -> Path | None:
    """Where uv keeps ephemeral environments, if it is discoverable."""
    override = os.environ.get("UV_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    home = Path.home()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / "uv" / "cache" if local else None
    return home / ".cache" / "uv"


def _is_ephemeral(path: Path) -> bool:
    """True when ``path`` lives inside a uv cache, so it will not survive.

    ``uvx`` runs from ``~/.cache/uv/archive-v0/<hash>/bin``. That path is real
    right now and gone after a cache prune, and the hash changes with the
    resolved dependency set, so writing it into an agent's config produces a
    server that works until it silently does not.
    """
    cache = _uv_cache_root()
    if cache is None:  # pragma: no cover - only on win32 without LOCALAPPDATA
        return False
    try:
        path.resolve().relative_to(cache.resolve())
    except (ValueError, OSError):
        return False
    return True


def resolve_launch_target(*, prefer_uvx: bool = False) -> str:
    """Return the single-token command an agent should run.

    An absolute path to a durably installed console script is preferred: GUI
    agents such as Claude Desktop and Zed start from a login environment whose
    ``PATH`` rarely includes ``~/.local/bin``, so a bare name would resolve for
    the user's shell and not for the agent. When this process is itself running
    from a uv cache — ``uvx google-play-console-mcp install`` — there is no
    durable path to point at, so the ``uvx`` runner form is used instead.
    """
    if not prefer_uvx:
        found = shutil.which(EXECUTABLE)
        if found:
            path = Path(found)
            if not _is_ephemeral(path):
                return str(path)
    return f"uvx {PACKAGE}"


def build_server_args(*, read_only: bool = False) -> list[str]:
    """Flags the registered server is launched with."""
    return ["--read-only"] if read_only else []


def build_server_env(
    *, credentials: str | Path | None = None, use_keychain: bool = False
) -> dict[str, str]:
    """Environment the registered server is launched with.

    With ``use_keychain`` the key is moved into the OS secret store and the
    returned environment is empty: nothing about the credential is written into
    an agent's config file, which is a plain-text file that often lives in a
    dotfiles repository.
    """
    if credentials is None:
        return {}
    path = Path(credentials).expanduser()
    if not path.is_file():
        raise ValidationError(
            f"Credentials file not found: {path}",
            remedy=(
                "Pass --credentials with the path to the service account JSON key "
                "downloaded from the Google Cloud console."
            ),
        )
    if use_keychain:
        _store_in_keychain(path)
        return {}
    return {CREDENTIALS_ENV: str(path.resolve())}


def _store_in_keychain(path: Path) -> None:
    """Put the key at ``path`` into the OS secret store."""
    raw = path.read_text()
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        raise ValidationError(
            f"Not a JSON key file: {path}",
            remedy="Pass the service account JSON key downloaded from the Google Cloud console.",
        ) from None
    try:
        keychain.store(json.dumps(info))
    except keychain.KeychainUnavailableError as exc:
        raise ValidationError(
            str(exc),
            remedy=(
                "Drop --keychain to pass the key by environment variable instead, "
                "or unlock the OS secret store and retry."
            ),
        ) from exc
    print(f"Stored the key in {keychain.backend_name()}; it is not written into any config file.")


def build_add_mcp_argv(
    target: str,
    *,
    npx: str = "npx",
    name: str = DEFAULT_SERVER_NAME,
    agents: list[str] | None = None,
    all_agents: bool = False,
    global_scope: bool = True,
    yes: bool = False,
    server_args: list[str] | None = None,
    server_env: dict[str, str] | None = None,
) -> list[str]:
    """Build the full ``npx add-mcp`` argv.

    Server flags use ``--args=<flag>`` rather than a separate token so a value
    that starts with a dash is not mistaken for one of add-mcp's own options.
    """
    argv = [npx, "-y", ADD_MCP_SPEC, target, "--name", name]
    if global_scope:
        argv.append("--global")
    if all_agents:
        argv.append("--all")
    for agent in agents or []:
        argv += ["--agent", agent]
    for flag in server_args or []:
        argv.append(f"--args={flag}")
    for key, value in (server_env or {}).items():
        argv.append(f"--env={key}={value}")
    if yes:
        argv.append("--yes")
    return argv


def manual_config(
    target: str,
    *,
    name: str = DEFAULT_SERVER_NAME,
    server_args: list[str] | None = None,
    server_env: dict[str, str] | None = None,
) -> str:
    """The config block to paste by hand when the delegate cannot run.

    Emitted in the ``mcpServers`` shape that Claude Desktop, Cursor, Windsurf
    and Gemini CLI read directly; VS Code nests the same entry under
    ``servers`` and Zed under ``context_servers``.
    """
    command, *runner_args = target.split(" ")
    entry: dict[str, object] = {
        "command": command,
        "args": runner_args + list(server_args or []),
    }
    if server_env:
        entry["env"] = server_env
    return json.dumps({"mcpServers": {name: entry}}, indent=2)


def _print_manual(target: str, name: str, args: list[str], env: dict[str, str]) -> None:
    print(
        "Node.js is required to configure agents automatically: add-mcp runs "
        "under npx.\nInstall Node.js from https://nodejs.org, then re-run this "
        "command.\n\nTo wire it up by hand instead, add this to your agent's "
        "MCP config:\n",
        file=sys.stderr,
    )
    print(manual_config(target, name=name, server_args=args, server_env=env))


def install(
    *,
    name: str = DEFAULT_SERVER_NAME,
    agents: list[str] | None = None,
    all_agents: bool = False,
    global_scope: bool = True,
    yes: bool = False,
    read_only: bool = False,
    credentials: str | Path | None = None,
    use_keychain: bool = False,
    prefer_uvx: bool = False,
    dry_run: bool = False,
) -> int:
    """Register this server with the selected agents. Returns an exit code."""
    target = resolve_launch_target(prefer_uvx=prefer_uvx)
    server_args = build_server_args(read_only=read_only)
    server_env = build_server_env(credentials=credentials, use_keychain=use_keychain)

    npx = shutil.which("npx")
    if npx is None:
        _print_manual(target, name, server_args, server_env)
        return 1

    argv = build_add_mcp_argv(
        target,
        npx=npx,
        name=name,
        agents=agents,
        all_agents=all_agents,
        global_scope=global_scope,
        yes=yes,
        server_args=server_args,
        server_env=server_env,
    )

    if dry_run:
        # Quoted, because the target is a single argv element that can contain
        # a space ("uvx google-play-console-mcp") and the printed line is meant
        # to be runnable as-is.
        print(shlex.join(argv))
        return 0

    print(f"Registering {name!r} as: {target} {' '.join(server_args)}".rstrip())
    try:
        # Inherits stdio on purpose: add-mcp's agent picker is interactive.
        completed = subprocess.run(argv, check=False)  # noqa: S603 - argv list, resolved npx
    except OSError as exc:  # pragma: no cover - npx vanished between which() and run
        print(f"Could not run add-mcp: {exc}", file=sys.stderr)
        return 1
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``google-play-console-mcp install``."""
    parser = argparse.ArgumentParser(
        prog=f"{EXECUTABLE} install",
        description=(
            "Register this server with your coding agents. With no --agent, "
            "add-mcp detects the agents you have installed and offers them for "
            "selection."
        ),
        epilog=(
            "Agent names: claude-code, claude-desktop, cursor, vscode, codex, "
            "gemini-cli, windsurf, zed, cline, goose, opencode and more — run "
            "'npx add-mcp list-agents' for the full list."
        ),
    )
    parser.add_argument(
        "-a",
        "--agent",
        action="append",
        dest="agents",
        metavar="AGENT",
        help="Agent to register with; repeat for several. Omit to choose interactively.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_agents",
        help="Register with every supported agent.",
    )
    parser.add_argument(
        "--project",
        action="store_true",
        help="Write project-level config in the current directory instead of user-level.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompts.",
    )
    parser.add_argument(
        "--name",
        default=DEFAULT_SERVER_NAME,
        help=f"Name to register the server under (default: {DEFAULT_SERVER_NAME}).",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Register the server with all write operations disabled.",
    )
    parser.add_argument(
        "--credentials",
        metavar="PATH",
        help=f"Service account JSON key to pass as {CREDENTIALS_ENV}.",
    )
    parser.add_argument(
        "--keychain",
        action="store_true",
        help="Store --credentials in the OS keychain instead of in the agent config file.",
    )
    parser.add_argument(
        "--uvx",
        action="store_true",
        help="Register 'uvx google-play-console-mcp' instead of this install's path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the add-mcp command that would run, and exit.",
    )
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Entry point for the ``install`` subcommand. Returns an exit code."""
    args = build_parser().parse_args(argv)
    try:
        return install(
            name=args.name,
            agents=args.agents,
            all_agents=args.all_agents,
            global_scope=not args.project,
            yes=args.yes,
            read_only=args.read_only,
            credentials=args.credentials,
            use_keychain=args.keychain,
            prefer_uvx=args.uvx,
            dry_run=args.dry_run,
        )
    except ValidationError as exc:
        print(f"{exc.message}\n{exc.remedy}", file=sys.stderr)
        return 2
