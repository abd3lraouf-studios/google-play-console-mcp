"""Tests for the write-safety gate: tokens, replay, audit, rollout policy."""

from __future__ import annotations

import json
import re
import stat
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import google_play_console_mcp.guards as guards
from google_play_console_mcp.errors import (
    GuardError,
    PlayConsoleError,
    ValidationError,
    redact_path,
    render_error,
)
from google_play_console_mcp.guards import (
    DEFAULT_TRACK,
    PRODUCTION_POLICY,
    Change,
    Operation,
    Preview,
    RolloutPolicy,
    audit,
    audit_execution,
    audit_log_path,
    audit_warning,
    clear_warnings,
    ensure_private_dir,
    ensure_private_file,
    is_production_track,
    issue_token,
    require_confirmation,
    resolve_track,
    scrub,
    target_for,
    unguarded,
    untrusted,
    verify_token,
)

PKG = "com.example.app"


def _op(**params: Any) -> Operation:
    return Operation(tool="deploy_app", target=target_for(PKG), params=params or {"track": "beta"})


def _token_from(text: str) -> str:
    match = re.search(r'confirmation_token="([^"]+)"', text)
    assert match, "no token in preview"
    return match.group(1)


def _audit_records() -> list[dict[str, Any]]:
    path = audit_log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------


def test_redact_path_keeps_only_the_last_two_segments():
    assert redact_path(None) == "<not set>"
    assert redact_path("key.json") == "key.json"
    assert redact_path("/Users/someone/secrets/key.json") == ".../secrets/key.json"


def test_error_to_dict_carries_remedy_and_optional_fields():
    exc = PlayConsoleError("boom", remedy="do this")
    assert exc.to_dict() == {"error": "error", "message": "boom", "remedy": "do this"}

    rich = ValidationError("bad", remedy="fix", doc_url="http://d", details={"k": 1})
    data = rich.to_dict()
    assert data["error"] == "validation_error"
    assert data["doc_url"] == "http://d"
    assert data["details"] == {"k": 1}


def test_render_error_degrades_for_unexpected_exceptions():
    assert render_error(ValidationError("x", remedy="y"))["error"] == "validation_error"
    rendered = render_error(RuntimeError("kaboom"))
    assert rendered["error"] == "unexpected_error"
    assert "RuntimeError: kaboom" in rendered["message"]


# ---------------------------------------------------------------------------
# state directory permissions
# ---------------------------------------------------------------------------


def test_state_dir_follows_the_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path / "x"))
    assert guards.state_dir() == tmp_path / "x"


def test_state_dir_defaults_under_home(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLAY_MCP_STATE_DIR", raising=False)
    assert guards.state_dir() == Path.home() / ".google-play-mcp"


def test_ensure_private_dir_narrows_every_level_it_creates(tmp_path):
    """mkdir(parents=True, mode=...) only applies the mode to the last component."""
    target = tmp_path / "a" / "b" / "c"
    ensure_private_dir(target)
    for level in (tmp_path / "a", tmp_path / "a" / "b", target):
        assert stat.S_IMODE(level.stat().st_mode) == 0o700


def test_ensure_private_dir_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ensure_private_dir(blocker / "under-a-file")  # not a directory; must not raise


def test_ensure_private_file_narrows_a_wide_file(tmp_path):
    path = tmp_path / "k"
    path.write_text("x")
    path.chmod(0o644)
    ensure_private_file(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_ensure_private_file_ignores_a_missing_file(tmp_path):
    ensure_private_file(tmp_path / "nope")  # must not raise


def test_guard_secret_is_persisted_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    guards.reset_state_cache()
    first = guards._guard_secret()
    guards.reset_state_cache()
    assert guards._guard_secret() == first
    assert stat.S_IMODE((tmp_path / "guard.key").stat().st_mode) == 0o600


def test_guard_secret_warns_and_narrows_a_world_readable_key(tmp_path, monkeypatch):
    """A key another local user can read is a key they can mint tokens with."""
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    guards.reset_state_cache()
    guards._guard_secret()
    key = tmp_path / "guard.key"
    key.chmod(0o644)
    guards.reset_state_cache()
    clear_warnings()

    guards._guard_secret()

    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    warning = audit_warning()
    assert warning is not None
    assert "readable by other users" in warning


def test_guard_secret_regenerates_when_the_stored_key_is_too_short(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "guard.key").write_text("tooshort")
    guards.reset_state_cache()
    assert len(guards._guard_secret()) == 32


def test_guard_secret_falls_back_to_a_process_key_when_unpersistable(monkeypatch, tmp_path):
    """A read-only home must not break confirmation, only stop it surviving restart.

    The state dir is pointed *under a regular file*, so every write genuinely
    fails at the OS level rather than through a patched Path — patching
    Path.write_text globally would break pytest's own bookkeeping.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(blocker / "state"))
    guards.reset_state_cache()

    key = guards._guard_secret()

    assert len(key) == 32
    guards.reset_state_cache()
    assert guards._guard_secret() != key  # not persisted, so a fresh one each time


# ---------------------------------------------------------------------------
# redaction and untrusted text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "confirmation_token",
        "credentials_json",
        "api_key",
        "PASSWORD",
        "authorization",
        "signature",
    ],
)
def test_scrub_redacts_secret_shaped_keys(key):
    assert scrub({key: "hunter2"})[key] == "<redacted>"


def test_scrub_digests_long_values_so_the_log_stays_greppable():
    value = "x" * 500
    out = scrub({"release_notes": value})["release_notes"]
    assert out.startswith("x" * 240)
    assert "sha256:" in out
    assert "+260 chars" in out


def test_scrub_redacts_paths_and_recurses():
    assert scrub({"file_path": "/Users/a/b/app.aab"})["file_path"] == ".../b/app.aab"
    assert scrub({"p": Path("/Users/a/b/c.json")})["p"] == ".../b/c.json"
    nested = scrub({"outer": {"token": "s", "n": 1}, "items": ["a", "b"], "flag": True})
    assert nested["outer"]["token"] == "<redacted>"
    assert nested["items"] == ["a", "b"]
    assert nested["flag"] is True


def test_scrub_stringifies_anything_else():
    assert scrub({"obj": object()})["obj"].startswith("<object")


@pytest.mark.parametrize(
    "raw",
    ["line\nbreak", "carriage\rreturn", "vertical\vtab", "next\x85line", "para\u2029sep"],
)
def test_untrusted_flattens_every_line_boundary(raw):
    """A reviewer whose name contains a break could otherwise start a line."""
    assert len(untrusted(raw).splitlines()) == 1


def test_untrusted_strips_ansi_and_normalises_whitespace():
    assert untrusted("\x1b[31mred\x1b[0m   text") == "red text"


def test_untrusted_truncates_and_handles_none():
    assert untrusted(None) == ""
    assert untrusted("abcdefgh", limit=5) == "abcd…"


# ---------------------------------------------------------------------------
# Operation fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_is_stable_across_key_order():
    assert _op(a=1, b=2).fingerprint() == _op(b=2, a=1).fingerprint()


def test_fingerprint_changes_when_a_parameter_changes():
    assert _op(fraction=0.1).fingerprint() != _op(fraction=0.2).fingerprint()


def test_fingerprint_tolerates_float_representation_noise():
    assert _op(f=0.1).fingerprint() == _op(f=0.10000000000000001).fingerprint()


def test_fingerprint_normalises_collections_and_paths(tmp_path):
    assert _op(s={"b", "a"}).fingerprint() == _op(s={"a", "b"}).fingerprint()
    assert _op(items=[1, 2]).fingerprint() == _op(items=(1, 2)).fingerprint()
    f = tmp_path / "app.aab"
    f.write_text("x")
    assert _op(file=f).fingerprint() != _op(file=tmp_path / "missing.aab").fingerprint()
    assert _op(o=object()).fingerprint()


def test_fingerprint_uses_file_identity_never_contents(tmp_path):
    """Hashing a 100 MB bundle on every preview would be unacceptable."""
    f = tmp_path / "app.aab"
    f.write_text("a")
    before = _op(file=f).fingerprint()
    time.sleep(0.01)
    f.write_text("bb")
    assert _op(file=f).fingerprint() != before


def test_call_signature_echoes_arguments_and_protects_secrets():
    op = Operation(
        tool="t",
        target="x",
        params={},
        call_args={"package_name": PKG, "api_key": "s3cret", "notes": "y" * 300},
    )
    sig = op.call_signature(confirmed=True)
    assert "package_name='com.example.app'" in sig
    assert "s3cret" not in sig
    assert "300-character text" in sig
    assert "confirm=True," in sig


def test_call_signature_replaces_a_long_non_string_value():
    # A multi-kilobyte CSV arrives as a one-key dict, so the isinstance(str)
    # branch never sees it and repr() would render the whole file into the
    # block a human is supposed to read.
    payload = {"safetyLabels": "PSL_Q,R,TRUE,MULTIPLE_CHOICE,Label\n" * 400}
    op = Operation(tool="t", target="x", params={}, call_args={"safety_labels": payload})

    sig = op.call_signature()

    assert "PSL_Q" not in sig
    assert "the same dict you passed before" in sig
    assert "sha256:" in sig
    assert len(sig) < 400


def test_call_signature_still_echoes_a_short_non_string_value():
    op = Operation(tool="t", target="x", params={}, call_args={"releases": [{"status": "draft"}]})

    assert "releases=[{'status': 'draft'}]" in op.call_signature()


def test_call_signature_falls_back_to_params():
    assert "track='beta'" in _op(track="beta").call_signature()


def test_target_for_labels_the_store():
    assert target_for(PKG) == f"google_play:{PKG}"


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------


def test_token_round_trip():
    op = _op()
    verify_token(issue_token(op.fingerprint()), op.fingerprint(), tool="deploy_app")


def test_token_carries_no_recoverable_fingerprint():
    op = _op()
    assert op.fingerprint() not in issue_token(op.fingerprint())


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (None, "no confirmation_token"),
        ("", "no confirmation_token"),
        ("garbage", "malformed"),
        ("xx1.1.2.3", "malformed"),
        ("pp1.notanint.n.m", "unreadable expiry"),
    ],
)
def test_verify_token_rejects_bad_shapes(token, expected):
    with pytest.raises(GuardError, match=expected):
        verify_token(token, _op().fingerprint(), tool="deploy_app")


def test_verify_token_rejects_a_token_from_another_operation():
    other = issue_token(_op(track="alpha").fingerprint())
    with pytest.raises(GuardError, match="does not match these arguments"):
        verify_token(other, _op(track="production").fingerprint(), tool="deploy_app")


def test_verify_token_rejects_an_expired_token():
    op = _op()
    token = issue_token(op.fingerprint(), ttl_seconds=-1)
    with pytest.raises(GuardError, match="expired"):
        verify_token(token, op.fingerprint(), tool="deploy_app")


def test_verify_token_rejects_a_replayed_token():
    op = _op()
    token = issue_token(op.fingerprint())
    verify_token(token, op.fingerprint(), tool="deploy_app")
    with pytest.raises(GuardError, match="already been used"):
        verify_token(token, op.fingerprint(), tool="deploy_app")


def test_a_forged_token_cannot_be_computed_without_the_key():
    """The whole point of keying the MAC: the model cannot mint its own."""
    op = _op()
    import hashlib

    forged = f"pp1.{int(time.time() + 600)}.aaaaaaaaaaaaaaaa.{hashlib.sha256(op.canonical().encode()).hexdigest()[:32]}"
    with pytest.raises(GuardError, match="does not match these arguments"):
        verify_token(forged, op.fingerprint(), tool="deploy_app")


def test_expired_nonces_are_evicted_from_the_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    guards.reset_state_cache()
    ledger = tmp_path / "guard-nonces.json"
    ensure_private_dir(tmp_path)
    ledger.write_text(json.dumps({"stale": int(time.time()) - 10}))
    op = _op()
    verify_token(issue_token(op.fingerprint()), op.fingerprint(), tool="t")
    assert "stale" not in json.loads(ledger.read_text())


def test_a_corrupt_ledger_degrades_but_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    guards.reset_state_cache()
    clear_warnings()
    ensure_private_dir(tmp_path)
    (tmp_path / "guard-nonces.json").write_text("{ not json")
    op = _op()
    verify_token(issue_token(op.fingerprint()), op.fingerprint(), tool="t")
    warning = audit_warning()
    assert warning is not None
    assert "replay ledger unavailable" in warning


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------


def test_audit_writes_one_scrubbed_json_line_owner_only():
    audit(_op(track="beta", api_key="secret"), outcome="preview", detail="d", extra={"k": "v"})
    records = _audit_records()
    assert records[-1]["outcome"] == "preview"
    assert records[-1]["params"]["api_key"] == "<redacted>"
    assert records[-1]["extra"] == {"k": "v"}
    assert stat.S_IMODE(audit_log_path().stat().st_mode) == 0o600


def test_audit_truncates_a_huge_detail():
    audit(_op(), outcome="failed", detail="x" * 5000)
    assert len(_audit_records()[-1]["detail"]) == 1000


def test_audit_never_raises_when_the_log_is_unwritable(monkeypatch, tmp_path):
    """Losing the audit log must not break a write, but must never be silent."""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(blocker / "state"))
    guards.reset_state_cache()
    clear_warnings()

    audit(_op(), outcome="preview")  # must not raise

    warning = audit_warning()
    assert warning is not None
    assert "audit log not writable" in warning


def test_audit_execution_records_success_with_notes():
    op = _op()
    with audit_execution(op) as recorder:
        recorder.note("uploaded")
        recorder.set("version_code", 42)
    record = _audit_records()[-1]
    assert record["outcome"] == "executed"
    assert record["detail"] == "uploaded"
    assert record["extra"]["version_code"] == 42
    assert "elapsed_s" in record["extra"]


def test_audit_execution_records_failure_and_reraises():
    """A failed write is the most important line in the log."""
    with pytest.raises(RuntimeError), audit_execution(_op()):
        raise RuntimeError("api exploded")
    record = _audit_records()[-1]
    assert record["outcome"] == "failed"
    assert "api exploded" in record["detail"]


def test_unguarded_logs_immediate_then_executed():
    op = Operation(tool="halt_release", target=target_for(PKG), params={"track": "production"})
    with unguarded(op, reason="halting reduces blast radius"):
        pass
    outcomes = [r["outcome"] for r in _audit_records()[-2:]]
    assert outcomes == ["immediate", "executed"]


def test_audit_warning_is_none_when_healthy():
    clear_warnings()
    assert audit_warning() is None


# ---------------------------------------------------------------------------
# rollout policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("track", "expected"),
    [("production", True), ("wear:production", True), ("beta", False), (" PRODUCTION ", True)],
)
def test_is_production_track(track, expected):
    assert is_production_track(track) is expected


def test_resolve_track_defaults_to_internal():
    """A model that omits the track did not think about the track."""
    assert resolve_track(None) == DEFAULT_TRACK
    assert resolve_track("   ") == DEFAULT_TRACK
    assert resolve_track(" beta ") == "beta"


@pytest.mark.parametrize("name", ["prod", "live", "release", "MAIN"])
def test_resolve_track_rejects_names_that_only_look_like_production(name):
    """Accepting 'prod' would silently create an empty new track called prod."""
    with pytest.raises(ValidationError, match="not a Play track name"):
        resolve_track(name)


def test_policy_refuses_a_full_production_release_in_one_step():
    with pytest.raises(ValidationError, match="cannot release to 100%"):
        PRODUCTION_POLICY.decide("production", user_fraction=1.0)
    with pytest.raises(ValidationError, match="cannot release to 100%"):
        PRODUCTION_POLICY.decide("production", status="completed")


def test_policy_refuses_a_first_step_above_the_ceiling():
    with pytest.raises(ValidationError, match="above the"):
        PRODUCTION_POLICY.decide("production", user_fraction=0.5)


def test_policy_applies_a_safe_default_fraction_on_production():
    decision = PRODUCTION_POLICY.decide("production")
    assert decision.user_fraction == pytest.approx(0.1)
    assert decision.is_production is True
    assert "safe default" in decision.notes[0]


def test_policy_allows_production_draft_and_halted():
    assert PRODUCTION_POLICY.decide("production", status="draft").status == "draft"
    assert PRODUCTION_POLICY.decide("production", status="halted").status == "halted"


def test_policy_leaves_testing_tracks_alone():
    decision = PRODUCTION_POLICY.decide("beta")
    assert decision.is_production is False
    assert "testing track" in decision.notes[0]
    assert PRODUCTION_POLICY.decide("beta", status="draft").status == "draft"
    assert PRODUCTION_POLICY.decide("beta", user_fraction=0.5).user_fraction == pytest.approx(0.5)


def test_policy_rejects_an_unknown_status_and_a_bad_fraction():
    with pytest.raises(ValidationError, match="Unknown release status"):
        PRODUCTION_POLICY.decide("beta", status="rolling")
    with pytest.raises(ValidationError, match="between 0 and 1"):
        PRODUCTION_POLICY.decide("beta", user_fraction=50)


def test_expand_is_the_only_path_to_one_hundred_percent():
    completed = PRODUCTION_POLICY.expand("production", user_fraction=1.0)
    assert completed.status == "completed"
    assert completed.user_fraction is None
    widened = PRODUCTION_POLICY.expand("production", user_fraction=0.5)
    assert widened.status == "inProgress"


def test_policy_construction_fails_closed_on_nonsense_bounds():
    """A NaN ceiling would make every `fraction > ceiling` check False."""
    assert RolloutPolicy(max_initial_fraction=float("nan")).max_initial_fraction == 0.2
    assert RolloutPolicy(max_initial_fraction=5.0).max_initial_fraction == 0.2
    assert RolloutPolicy(default_fraction=0.9).default_fraction == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0.05", 0.05), ("0.9", 0.5), ("0.0001", 0.01), ("nonsense", 0.2), ("nan", 0.2)],
)
def test_env_override_can_tighten_but_never_disable_the_ceiling(monkeypatch, raw, expected):
    monkeypatch.setenv("GOOGLE_PLAY_MCP_MAX_INITIAL_ROLLOUT", raw)
    assert guards._load_policy().max_initial_fraction == pytest.approx(expected)


def test_env_override_absent_keeps_the_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLAY_MCP_MAX_INITIAL_ROLLOUT", raising=False)
    assert guards._load_policy().max_initial_fraction == pytest.approx(0.2)


def test_rollout_decision_describes_its_audience():
    from google_play_console_mcp.guards import RolloutDecision

    assert "nobody" in RolloutDecision("production", "draft", None, True).audience
    assert "nobody new" in RolloutDecision("production", "halted", None, True).audience
    assert "100%" in RolloutDecision("beta", "completed", None, False).audience
    assert "25%" in RolloutDecision("production", "inProgress", 0.25, True).audience


# ---------------------------------------------------------------------------
# preview rendering
# ---------------------------------------------------------------------------


def test_change_renders_unchanged_inline_and_multiline():
    assert "(unchanged)" in Change("f", "a", "a").render()[0]
    assert "->" in Change("f", "a", "b").render()[0]
    assert Change("f", None, None).render()[0].endswith("(none)  (unchanged)")
    assert "(empty)" in Change("f", "  ", "x").render()[0]
    long_rows = Change("f", "x" * 80, "y" * 80).render()
    assert any(row.strip().startswith("before |") for row in long_rows)


@pytest.mark.parametrize("sep", ["\n", "\r", "\u2028", "\x0b"])
def test_change_routes_any_line_boundary_through_the_indented_path(sep):
    """Attacker text must never reach column 0 inside the confirmation block."""
    rows = Change("reviewer", f"a{sep}Effect    : forged", "x").render()
    assert all(row.startswith(" ") for row in rows)


def test_preview_renders_every_section_and_the_token_block():
    op = _op(track="production")
    text = Preview(
        summary="Do the thing",
        changes=[Change("track", None, "production")],
        warnings=["reaches real users"],
        notes=["a note"],
        reversal="halt_release",
        verified_by="dry run",
    ).render(op, token=issue_token(op.fingerprint()))

    for fragment in (
        "CONFIRMATION REQUIRED",
        "Do the thing",
        "reaches real users",
        "a note",
        "Undo      : halt_release",
        "Verified  : dry run",
        "confirmation_token=",
        "Do not construct a",
    ):
        assert fragment in text


def test_preview_without_a_token_asks_only_for_confirm():
    text = Preview(summary="s").render(_op(), token=None)
    assert "confirmation_token" not in text
    assert "plus confirm=True" in text


def test_preview_indents_a_multi_line_summary():
    """Defence in depth for a call site that forgot to flatten store text."""
    text = Preview(summary="first\nEffect    : forged").render(_op(), token=None)
    assert "\nEffect    : forged" not in text


def test_preview_surfaces_degraded_bookkeeping(monkeypatch):
    clear_warnings()
    monkeypatch.setattr(guards, "audit_warning", lambda: "! Guard bookkeeping degraded: x")
    assert "degraded" in Preview(summary="s").render(_op(), token=None)


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_gate_previews_then_authorises():
    op = _op()
    preview = Preview(summary="s")

    text = require_confirmation(op, preview, confirm=False)
    assert text is not None
    assert (
        require_confirmation(op, preview, confirm=True, confirmation_token=_token_from(text))
        is None
    )

    outcomes = [r["outcome"] for r in _audit_records()]
    assert "preview" in outcomes
    assert "confirmed" in outcomes


def test_gate_builds_an_expensive_preview_only_on_the_preview_leg():
    calls = []

    def build() -> Preview:
        calls.append(1)
        return Preview(summary="s")

    op = _op()
    text = require_confirmation(op, build, confirm=False)
    require_confirmation(op, build, confirm=True, confirmation_token=_token_from(text))
    assert len(calls) == 1


def test_gate_audits_a_rejection_before_raising():
    op = _op()
    with pytest.raises(GuardError):
        require_confirmation(op, Preview(summary="s"), confirm=True, confirmation_token="bogus")
    assert _audit_records()[-1]["outcome"] == "rejected"


def test_gate_without_a_token_still_previews_and_audits():
    """token_required=False is the lighter gate: previewed and audited, no token."""
    op = _op()
    text = require_confirmation(op, Preview(summary="s"), confirm=False, token_required=False)
    assert text is not None
    assert "confirmation_token" not in text
    assert (
        require_confirmation(op, Preview(summary="s"), confirm=True, token_required=False) is None
    )


def test_parameter_drift_between_preview_and_confirm_is_refused():
    """The exact failure the token exists to catch."""
    previewed = _op(track="beta", fraction=0.1)
    text = require_confirmation(previewed, Preview(summary="s"), confirm=False)
    drifted = _op(track="production", fraction=1.0)
    with pytest.raises(GuardError, match="does not match these arguments"):
        require_confirmation(
            drifted, Preview(summary="s"), confirm=True, confirmation_token=_token_from(text)
        )


# ---------------------------------------------------------------------------
# degraded-filesystem paths
# ---------------------------------------------------------------------------


def test_ensure_private_file_swallows_a_chmod_failure(tmp_path, monkeypatch):
    """Narrowing permissions is best-effort; it must never break a write."""
    path = tmp_path / "k"
    path.write_text("x")
    path.chmod(0o644)
    monkeypatch.setattr(guards.Path, "chmod", MagicMock(side_effect=OSError("nope")))
    ensure_private_file(path)  # must not raise


def test_guard_secret_regenerates_when_the_key_file_is_unreadable(tmp_path, monkeypatch):
    """An unreadable key must fail closed to a fresh one, not crash the server."""
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    ensure_private_dir(tmp_path)
    key = tmp_path / "guard.key"
    key.write_text("a" * 64)
    key.chmod(0o200)  # write-only: exists, but read_text raises
    guards.reset_state_cache()
    try:
        assert len(guards._guard_secret()) == 32
    finally:
        key.chmod(0o600)


def test_a_ledger_holding_a_json_non_object_is_ignored(tmp_path, monkeypatch):
    """A JSON array parses fine but is not a nonce map; treat it as empty."""
    monkeypatch.setenv("GOOGLE_PLAY_MCP_STATE_DIR", str(tmp_path))
    guards.reset_state_cache()
    ensure_private_dir(tmp_path)
    (tmp_path / "guard-nonces.json").write_text("[1, 2, 3]")
    op = _op()
    verify_token(issue_token(op.fingerprint()), op.fingerprint(), tool="t")
    assert isinstance(json.loads((tmp_path / "guard-nonces.json").read_text()), dict)


def test_the_same_degradation_warning_is_only_recorded_once():
    clear_warnings()
    guards._note_warning("same message")
    guards._note_warning("same message")
    warning = audit_warning()
    assert warning is not None
    assert warning.count("same message") == 1
