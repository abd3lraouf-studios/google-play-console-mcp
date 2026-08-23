# Getting Started

## Prerequisites

1. **Google Cloud Project** with the Google Play Developer API enabled
2. **Service Account** with access to your Play Console
3. **Python 3.11+**, `uvx`, or **Docker** installed

## Installation

The one-line installer registers the server with your coding agents:

```bash
uvx google-play-console-mcp install
```

It offers a checklist of the agents found on your machine — Claude Code, Claude
Desktop, Cursor, VS Code, Codex, Gemini CLI, Windsurf, Zed, Cline, Goose,
OpenCode and 11 more — and writes the correct config file for each. The wiring
is delegated to [add-mcp](https://github.com/neon-solutions/add-mcp), which runs
under `npx`; without Node.js installed, the command prints the config block to
paste by hand instead of failing.

| Flag | Effect |
|---|---|
| `-a, --agent AGENT` | Register with a named agent; repeat for several |
| `--all` | Register with every supported agent |
| `-y, --yes` | Skip confirmation prompts |
| `--project` | Write project-level config here instead of user-level |
| `--read-only` | Register the server with writes disabled |
| `--credentials PATH` | Pass a service account key as `GOOGLE_APPLICATION_CREDENTIALS` |
| `--name NAME` | Register under a different server name |
| `--uvx` | Register `uvx google-play-console-mcp` rather than this install's path |
| `--dry-run` | Print the `add-mcp` command and exit |

Run `npx add-mcp list-agents` for the full agent list.

### Getting the server itself

=== "uvx (Recommended)"

    ```bash
    uvx google-play-console-mcp
    ```

    No installation needed — uvx downloads and runs it directly.

=== "pip"

    ```bash
    pip install google-play-console-mcp
    google-play-console-mcp
    ```

=== "Docker"

    ```bash
    docker run -e GOOGLE_APPLICATION_CREDENTIALS=/creds/key.json \
      -v /path/to/service-account.json:/creds/key.json:ro \
      ghcr.io/abd3lraouf-studios/google-play-console-mcp:latest
    ```

=== "From Source"

    ```bash
    git clone https://github.com/abd3lraouf-studios/google-play-console-mcp.git
    cd google-play-console-mcp
    pip install -e .
    google-play-console-mcp
    ```

## Google Cloud Setup

### 1. Create a Service Account

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create or select a project
3. Enable the **Google Play Developer API**
4. Navigate to **IAM & Admin** → **Service Accounts**
5. Create a new service account
6. Download the JSON key file

### 2. Grant Play Console Access

1. Go to [Google Play Console](https://play.google.com/console/)
2. Navigate to **Users and permissions**
3. Click **Invite new users**
4. Enter the service account email (found in the JSON key file)
5. Grant permissions based on what you need:

| Permission | Required For |
|---|---|
| Release apps to testing tracks | Internal, alpha, beta deployments |
| Release apps to production | Production releases |
| Reply to reviews | Review management |
| View app information and download bulk reports | App details |

### 3. Set Credentials

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

Add this to your shell profile (`.bashrc`, `.zshrc`, etc.) to persist it.

## Verify Setup

Once configured with an MCP client, try a simple tool call:

```
validate_package_name("com.example.myapp")
```

If credentials are working, try:

```
get_app_details("com.your.actual.app")
```
