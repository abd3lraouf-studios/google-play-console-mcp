# Releasing

Publishing uses **PyPI Trusted Publishing (OIDC)**. No API token exists in this
repository or in its Actions secrets — GitHub signs a short-lived identity claim
for the workflow run and PyPI verifies it. There is no long-lived credential to
leak, rotate, or accidentally print.

## One-time setup

On [pypi.org/manage/account/publishing](https://pypi.org/manage/account/publishing/),
add a pending publisher:

| Field | Value |
|---|---|
| PyPI Project Name | `google-play-console-mcp` |
| Owner | `abd3lraouf-studios` |
| Repository name | `google-play-console-mcp` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

"Pending" is correct while the project does not yet exist on PyPI; it becomes a
normal trusted publisher on the first successful upload.

The `pypi` GitHub environment must exist on the repository. It has no required
reviewer: pushing a tag publishes, with no approval step in between. That is
deliberate — the gate is the automated one below, which runs on every release
and cannot be clicked through by mistake.

### There is deliberately no TestPyPI rehearsal

An earlier version of this document prescribed one. It never worked: no trusted
publisher was ever registered on test.pypi.org, so the step failed with
`invalid-publisher` on every release for five releases while real publishing
succeeded. A documented step that always fails is worse than no step, because
the next person to follow it assumes the release is broken.

What the rehearsal was really protecting against is a distribution that does not
install. That is now checked directly — see below — before the version number is
spent, and without a second account and namespace to keep in sync.

## Cutting a release

```bash
# 1. bump the version — this is the single source of truth
$EDITOR pyproject.toml

# 2. move [Unreleased] in CHANGELOG.md to the new version, and add its link

# 3. optional dry run: the whole gate, with no publish and no version spent
gh workflow run release.yml

# 4. commit, then tag, which triggers the publish
git tag -a v0.7.0 -m "0.7.0"
git push origin main
git push origin v0.7.0
```

**Only a tag publishes.** Running the workflow by hand is always a dry run — it
verifies and builds, then stops. That is what the TestPyPI rehearsal used to be
for, minus the second account.

Pushing the tag publishes. There is no approval prompt, so make sure the version
in `pyproject.toml` and the tag agree before pushing — though the workflow
checks that too, and fails the release if they do not.

The tag must match the packaged version. The workflow reads the built
distribution's metadata and fails the release if `v0.7.0` and the version in
`pyproject.toml` disagree, so a mistagged release cannot reach PyPI.

## What the workflow enforces

Every release runs the full gate before anything is published:

- `ruff check` and `ruff format --check`
- `mypy src/`
- the test suite at **`--cov-fail-under=100`** — coverage has never dropped
  below 100% branch, and a release is not the place to start
- tag/version agreement
- `twine check --strict`, so the PyPI page cannot render broken
- **the wheel actually contains `LICENSE`**
- **the wheel installs into a clean environment, imports, and its console
  script runs**

The last two are not ceremony.

A wheel shipped without its licence breaches Apache-2.0 section 4(a). Failing
the build is the only way to be sure it never happens.

And every other check reads the wheel from the outside — its file list and its
metadata. None of them notices a module that failed to be packaged. A wheel
missing `data_safety_ids.py` passes the licence check *and* `twine check
--strict`, and then raises `ModuleNotFoundError` on the user's first import.
Installing the artefact is what catches that, and it has to happen before the
upload, because a PyPI version number can never be reused.

## Version scheme

`pyproject.toml` holds the version. `0.x` while the planes are still settling;
the first release that has run against a real Play Console account across every
plane earns `1.0.0`.
