"""``google-play-console-mcp credentials`` — manage stored credentials.

Four subcommands, each doing one thing to the keychain entry the server reads:
``set`` puts a service-account key in, ``show`` says what is configured without
printing any of it, ``delete`` removes it, and ``adc`` prints the login line for
user OAuth.

The secret never appears in a terminal, a log line, or a shell history entry:
``set`` reads the key from a file or from stdin, and ``show`` renders only the
service-account e-mail and project.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from google_play_console_mcp import keychain
from google_play_console_mcp.credentials import ALL_SCOPES, USE_ADC_ENV, adc_enabled

CREDENTIALS_ENV = "GOOGLE_APPLICATION_CREDENTIALS"
INLINE_ENV = "GOOGLE_PLAY_STORE_CREDENTIALS"


def _read_key(source: str | None) -> tuple[str | None, str | None]:
    """Return ``(key_json, error)`` from a file path, or stdin when given ``-``."""
    if source is None or source == "-":
        if sys.stdin.isatty():
            return None, (
                "Nothing to read. Pass a file — `credentials set key.json` — or pipe "
                "the key in: `pbpaste | google-play-console-mcp credentials set -`."
            )
        return sys.stdin.read(), None

    path = Path(source).expanduser()
    if not path.is_file():
        return None, f"No such file: {path}"
    return path.read_text(), None


def _cmd_set(args: argparse.Namespace) -> int:
    raw, error = _read_key(args.key_file)
    if error:
        print(error, file=sys.stderr)
        return 2

    assert raw is not None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        print(
            "That is not JSON. Pass the service-account key file downloaded from "
            "the Google Cloud console.",
            file=sys.stderr,
        )
        return 2

    if info.get("type") != "service_account":
        print(
            f"Expected a service-account key; this JSON has type "
            f"{info.get('type', '<missing>')!r}.",
            file=sys.stderr,
        )
        return 2

    try:
        keychain.store(json.dumps(info), account=args.account)
    except keychain.KeychainUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        f"Stored in {keychain.backend_name()} as {args.account!r}: "
        f"{info.get('client_email', '<no client_email>')}"
    )
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    print(f"Keychain backend: {keychain.backend_name()}")

    stored = keychain.describe_stored(account=args.account)
    if stored is None:
        print(f"  stored key:    none (account {args.account!r})")
    else:
        print(f"  stored key:    {stored.get('client_email') or stored.get('type')}")
        print(f"  project:       {stored.get('project_id')}")

    for env in (CREDENTIALS_ENV, INLINE_ENV):
        value = os.environ.get(env)
        print(f"  {env}: {'set' if value else 'not set'}")

    print(f"  ADC ({USE_ADC_ENV}): {'enabled' if adc_enabled() else 'disabled'}")
    print(
        "\nResolution order: --credentials argument, then the environment "
        "variables above, then the keychain, then ADC if enabled."
    )
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    if keychain.delete(account=args.account):
        print(f"Removed {args.account!r} from {keychain.backend_name()}.")
        return 0
    print(f"Nothing stored under {args.account!r}.", file=sys.stderr)
    return 1


def _cmd_adc(_args: argparse.Namespace) -> int:
    print(
        "User OAuth is handled by the Google auth libraries, so there is no\n"
        "client secret or redirect to configure here. Two steps:\n\n"
        "1. Log in, naming the scopes. They are fixed at login: a user\n"
        "   credential arrives pre-scoped and cannot be widened later.\n\n"
        f"   gcloud auth application-default login \\\n     --scopes={','.join(ALL_SCOPES)}\n\n"
        f"2. Turn it on for this server, which is off by default so an unrelated\n"
        f"   gcloud login can never quietly become the identity that ships your\n"
        f"   releases:\n\n"
        f"   export {USE_ADC_ENV}=1\n\n"
        "   ...or pass --use-adc when starting the server.\n\n"
        "The Google account you log in with needs its own Play Console access;\n"
        "the scopes alone do not grant it."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for the ``credentials`` subcommand."""
    parser = argparse.ArgumentParser(
        prog="google-play-console-mcp credentials",
        description="Manage the credentials this server authenticates with.",
    )
    parser.add_argument(
        "--account",
        default=keychain.DEFAULT_ACCOUNT,
        help=(
            f"Keychain entry to act on (default: {keychain.DEFAULT_ACCOUNT}). "
            "Use different names to keep several Play accounts side by side."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_set = sub.add_parser("set", help="Store a service-account key in the keychain.")
    p_set.add_argument(
        "key_file",
        nargs="?",
        help="Path to the service-account JSON key, or '-' to read stdin.",
    )
    p_set.set_defaults(func=_cmd_set)

    sub.add_parser("show", help="Show what is configured, without printing secrets.").set_defaults(
        func=_cmd_show
    )
    sub.add_parser("delete", help="Remove the stored key.").set_defaults(func=_cmd_delete)
    sub.add_parser(
        "adc", help="Explain user OAuth via Application Default Credentials."
    ).set_defaults(func=_cmd_adc)
    return parser


def run_cli(argv: list[str] | None = None) -> int:
    """Entry point for ``credentials``. Returns an exit code."""
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result
