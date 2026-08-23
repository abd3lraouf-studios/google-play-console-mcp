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

Repeat on [test.pypi.org](https://test.pypi.org/manage/account/publishing/) with
Environment name `testpypi`.

"Pending" is correct while the project does not yet exist on PyPI; it becomes a
normal trusted publisher on the first successful upload.

Both GitHub environments (`pypi`, `testpypi`) must exist on the repository.
Adding a required reviewer to the `pypi` environment turns every release into an
approval step, which is worth it.

## Cutting a release

```bash
# 1. bump the version — this is the single source of truth
$EDITOR pyproject.toml

# 2. rehearse against TestPyPI first
gh workflow run release.yml -f target=testpypi

# 3. tag, which triggers the real publish
git tag -a v0.7.0 -m "0.7.0"
git push origin v0.7.0
```

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
- **the wheel actually contains `LICENSE`, `NOTICE` and `LICENSES/`**

That last one is not ceremony. This project is a derivative of MIT-licensed
work, redistributed under Apache-2.0. A wheel shipped without those files
breaches both the Apache-2.0 NOTICE clause and the MIT terms of the code it
contains. Failing the build is the only way to be sure it never happens.

## Version scheme

`pyproject.toml` holds the version. `0.x` while the planes are still settling;
the first release that has run against a real Play Console account across every
plane earns `1.0.0`.

## Re-establishing the upstream link

Git history was squashed when the project was renamed and relicensed, which
removed the `upstream` remote. The fork point is recorded, so merging upstream
fixes is still possible:

```bash
git remote add upstream https://github.com/lusky3/play-store-mcp
git fetch upstream
# fork point: play-store-mcp v0.5.1, commit ebdd9fd0
git diff ebdd9fd0..upstream/stable -- src/play_store_mcp/
```

The package was renamed `play_store_mcp` -> `google_play_console_mcp`, so a
straight merge will not apply. Read the upstream diff and port deliberately.
