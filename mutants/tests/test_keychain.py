"""Tests for keychain storage and the credentials CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from google_play_console_mcp import credentials as creds_mod
from google_play_console_mcp import credentials_cli as cli
from google_play_console_mcp import keychain

if TYPE_CHECKING:
    from pathlib import Path


class FakeKeyring:
    """In-memory stand-in for a platform secret store."""

    def __init__(self, *, priority: int = 5, fail: bool = False) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.priority = priority
        self.fail = fail

    def get_keyring(self) -> FakeKeyring:
        return self

    def set_password(self, service: str, account: str, secret: str) -> None:
        if self.fail:
            raise RuntimeError("locked")
        self.store[(service, account)] = secret

    def get_password(self, service: str, account: str) -> str | None:
        if self.fail:
            raise RuntimeError("locked")
        return self.store.get((service, account))

    def delete_password(self, service: str, account: str) -> None:
        if self.fail:
            raise RuntimeError("locked")
        del self.store[(service, account)]


@pytest.fixture
def fake_keyring(
    monkeypatch: pytest.MonkeyPatch,
    _isolate_ambient_credentials: None,
) -> FakeKeyring:
    """A working in-memory store.

    Depends on the autouse isolation fixture so it is guaranteed to run *after*
    it: both patch `keychain._keyring`, and whichever runs last wins.
    """
    ring = FakeKeyring()
    monkeypatch.setattr(keychain, "_keyring", lambda: ring)
    return ring


class TestRealBackend:
    """The lazy import is a runtime dependency, so it has to actually resolve.

    Deliberately does not take `fake_keyring`: this is the one test that must
    see the genuine module.
    """

    def test_keyring_imports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import keyring as real_keyring

        monkeypatch.undo()  # drop the autouse backend isolation
        assert keychain._keyring() is real_keyring


SA_KEY = {
    "type": "service_account",
    "project_id": "demo",
    "client_email": "bot@demo.iam.gserviceaccount.com",
    "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
}


@pytest.mark.usefixtures("fake_keyring")
class TestKeychainRoundTrip:
    def test_store_load_delete(self) -> None:
        keychain.store("secret-value")
        assert keychain.load() == "secret-value"
        assert keychain.delete() is True
        assert keychain.load() is None

    def test_delete_missing_entry_is_false(self) -> None:
        assert keychain.delete() is False

    def test_accounts_are_independent(self) -> None:
        keychain.store("a", account="one")
        keychain.store("b", account="two")
        assert keychain.load(account="one") == "a"
        assert keychain.load(account="two") == "b"


class TestKeychainDegradation:
    """An unreachable store must never take the server down."""

    def test_load_returns_none_when_backend_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(keychain, "_keyring", lambda: FakeKeyring(fail=True))
        assert keychain.load() is None

    def test_delete_returns_false_when_backend_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(keychain, "_keyring", lambda: FakeKeyring(fail=True))
        assert keychain.delete() is False

    def test_store_raises_a_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(keychain, "_keyring", lambda: FakeKeyring(fail=True))
        with pytest.raises(keychain.KeychainUnavailableError):
            keychain.store("x")

    def test_availability_tracks_backend_priority(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(keychain, "_keyring", lambda: FakeKeyring(priority=5))
        assert keychain.is_available() is True
        monkeypatch.setattr(keychain, "_keyring", lambda: FakeKeyring(priority=0))
        assert keychain.is_available() is False

    def test_availability_false_when_import_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom() -> Any:
            raise keychain.KeychainUnavailableError("no keyring")

        monkeypatch.setattr(keychain, "_keyring", boom)
        assert keychain.is_available() is False
        assert keychain.backend_name() == "unavailable"

    def test_backend_name_is_qualified(self, fake_keyring: FakeKeyring) -> None:
        """Every backend class is called Keyring; the module says which one."""
        assert keychain.backend_name().endswith(f".{type(fake_keyring).__name__}")


@pytest.mark.usefixtures("fake_keyring")
@pytest.mark.usefixtures("fake_keyring")
class TestDescribeStored:
    def test_none_when_empty(self) -> None:
        assert keychain.describe_stored() is None

    def test_identifies_without_revealing(self) -> None:
        keychain.store(json.dumps(SA_KEY))
        described = keychain.describe_stored()
        assert described == {
            "account": keychain.DEFAULT_ACCOUNT,
            "type": "service_account",
            "client_email": "bot@demo.iam.gserviceaccount.com",
            "project_id": "demo",
        }
        assert "private_key" not in json.dumps(described)

    def test_opaque_when_not_json(self) -> None:
        keychain.store("not-json")
        assert keychain.describe_stored() == {
            "account": keychain.DEFAULT_ACCOUNT,
            "type": "opaque",
        }


class TestCredentialResolution:
    def test_keychain_entry_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel = object()
        monkeypatch.setattr(creds_mod.keychain, "load", lambda **_kw: json.dumps(SA_KEY))
        monkeypatch.setattr(
            creds_mod.service_account.Credentials,
            "from_service_account_info",
            classmethod(lambda _cls, _info, **_kw: sentinel),
        )
        assert creds_mod.load_service_account_credentials(None, None, ["s"]) is sentinel

    def test_malformed_keychain_entry_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(creds_mod.keychain, "load", lambda **_kw: "{not json")
        assert creds_mod.load_service_account_credentials(None, None, ["s"]) is None

    def test_non_service_account_entry_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(creds_mod.keychain, "load", lambda **_kw: json.dumps({"a": 1}))

        def boom(_cls: Any, _info: Any, **_kw: Any) -> Any:
            raise ValueError("not a service account key")

        monkeypatch.setattr(
            creds_mod.service_account.Credentials,
            "from_service_account_info",
            classmethod(boom),
        )
        assert creds_mod.load_service_account_credentials(None, None, ["s"]) is None

    def test_empty_keychain_falls_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(creds_mod.keychain, "load", lambda **_kw: None)
        assert creds_mod.load_service_account_credentials(None, None, ["s"]) is None


class TestAdcGate:
    """ADC is ambient machine state, so it must never engage by accident."""

    def test_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(creds_mod.USE_ADC_ENV, raising=False)
        called = False

        def spy(**_kw: Any) -> Any:
            nonlocal called
            called = True
            return (object(), "proj")

        monkeypatch.setattr(creds_mod.google.auth, "default", spy)
        assert creds_mod.load_service_account_credentials(None, None, ["s"]) is None
        assert called is False, "ADC was consulted without being enabled"

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_enabled_by_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(creds_mod.USE_ADC_ENV, value)
        assert creds_mod.adc_enabled() is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_disabled_by_falsy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(creds_mod.USE_ADC_ENV, value)
        assert creds_mod.adc_enabled() is False

    def test_used_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel = object()
        monkeypatch.setenv(creds_mod.USE_ADC_ENV, "1")
        monkeypatch.setattr(creds_mod.google.auth, "default", lambda **_kw: (sentinel, "proj"))
        assert creds_mod.load_service_account_credentials(None, None, ["s"]) is sentinel

    def test_absent_adc_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(creds_mod.USE_ADC_ENV, "1")

        def boom(**_kw: Any) -> Any:
            raise creds_mod.DefaultCredentialsError("none configured")

        monkeypatch.setattr(creds_mod.google.auth, "default", boom)
        assert creds_mod.load_service_account_credentials(None, None, ["s"]) is None


@pytest.mark.usefixtures("fake_keyring")
class TestCredentialsCli:
    def test_set_from_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        key = tmp_path / "k.json"
        key.write_text(json.dumps(SA_KEY))
        assert cli.run_cli(["set", str(key)]) == 0
        assert json.loads(keychain.load() or "{}")["client_email"] == SA_KEY["client_email"]
        assert "bot@demo.iam.gserviceaccount.com" in capsys.readouterr().out

    def test_set_rejects_non_service_account(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        key = tmp_path / "k.json"
        key.write_text(json.dumps({"type": "authorized_user"}))
        assert cli.run_cli(["set", str(key)]) == 2
        assert "authorized_user" in capsys.readouterr().err

    def test_set_rejects_non_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        key = tmp_path / "k.json"
        key.write_text("nope")
        assert cli.run_cli(["set", str(key)]) == 2
        assert "not JSON" in capsys.readouterr().err

    def test_set_missing_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.run_cli(["set", str(tmp_path / "absent.json")]) == 2
        assert "No such file" in capsys.readouterr().err

    def test_set_from_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io

        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(SA_KEY)))
        assert cli.run_cli(["set", "-"]) == 0
        assert keychain.load() is not None

    def test_set_with_no_input_on_a_tty_explains(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class Tty:
            def isatty(self) -> bool:
                return True

        monkeypatch.setattr(cli.sys, "stdin", Tty())
        assert cli.run_cli(["set"]) == 2
        assert "Nothing to read" in capsys.readouterr().err

    def test_set_reports_unavailable_keychain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(keychain, "_keyring", lambda: FakeKeyring(fail=True))
        key = tmp_path / "k.json"
        key.write_text(json.dumps(SA_KEY))
        assert cli.run_cli(["set", str(key)]) == 1
        assert "keychain" in capsys.readouterr().err.lower()

    def test_show_reports_state_without_secrets(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        keychain.store(json.dumps(SA_KEY))
        monkeypatch.setenv(cli.CREDENTIALS_ENV, "/some/path.json")
        assert cli.run_cli(["show"]) == 0
        out = capsys.readouterr().out
        assert "bot@demo.iam.gserviceaccount.com" in out
        assert "private_key" not in out
        assert "BEGIN PRIVATE KEY" not in out
        assert "set" in out

    def test_show_when_nothing_stored(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.run_cli(["show"]) == 0
        assert "none" in capsys.readouterr().out

    def test_delete(self, capsys: pytest.CaptureFixture[str]) -> None:
        keychain.store("x")
        assert cli.run_cli(["delete"]) == 0
        assert cli.run_cli(["delete"]) == 1
        assert "Nothing stored" in capsys.readouterr().err

    def test_adc_prints_scoped_login(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.run_cli(["adc"]) == 0
        out = capsys.readouterr().out
        assert "gcloud auth application-default login" in out
        # The scopes are the whole point: a default login cannot reach these APIs.
        for scope in creds_mod.ALL_SCOPES:
            assert scope in out
        assert creds_mod.USE_ADC_ENV in out


class TestServerRouting:
    def test_credentials_subcommand_is_routed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import google_play_console_mcp.server as server

        monkeypatch.setattr(server, "run_credentials_cli", lambda _argv: 5)
        with pytest.raises(SystemExit) as excinfo:
            server.main(["credentials", "show"])
        assert excinfo.value.code == 5

    def test_use_adc_flag_sets_the_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import google_play_console_mcp.server as server

        monkeypatch.delenv(creds_mod.USE_ADC_ENV, raising=False)
        monkeypatch.setattr(server.mcp, "run", lambda **_kw: None)
        server.main(["--use-adc"])
        assert creds_mod.adc_enabled() is True
