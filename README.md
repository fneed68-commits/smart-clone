# smart-clone

> Intelligent repository cloner — analyzes before downloading.

Zero-dependency Python CLI that inspects a GitHub repository before
cloning it. It reports size, largest folders, binaries, LFS usage, and
available disk space, then recommends the smallest clone strategy that
still fits your intent.

## Why

Cloning a large repo (like n8n-io/n8n, ~640 MB) on a phone, VPS, or
metered connection is painful. Most of that size is:

- Full git history you probably don't need
- Blobs of every version of every file
- Binary assets (images, videos, fonts) you'll never touch

smart-clone inspects the repository before downloading, checks your
disk, and recommends the smallest clone that still does what you need.

## Install

    git clone https://github.com/fneed68-commits/smart-clone.git
    cd smart-clone
    chmod +x smart-clone.py

## Usage

    # Analyze only (recommended first)
    python3 smart-clone.py n8n-io/n8n --intent read --dry-run

    # Clone with your intent
    python3 smart-clone.py n8n-io/n8n --intent contribute

    # JSON output (for automation)
    python3 smart-clone.py n8n-io/n8n --json

    # Skip confirmation
    python3 smart-clone.py caddyserver/caddy --intent build --yes

## Intents

- read       - Reading code only           - Sparse (single folder)
- build      - Running / building locally  - Partial (blob:none, depth=1)
- contribute - Opening PRs, using git log  - History-preserving partial
- audit      - Security review (all files) - Partial (blob:none, depth=1)

## Options

- --intent INTENT  - read | build | contribute | audit
- --dry-run        - Analyze only, don't clone
- --json           - Output JSON (for scripts)
- --no-color       - Disable ANSI colors
- --path PATH      - Target disk to check (default: .)
- --token TOKEN    - GitHub token (or set GITHUB_TOKEN env)
- -y, --yes        - Skip the confirmation prompt

## Tips

Set a GitHub token to raise the API rate limit from 60 to 5,000 req/hour:

    export GITHUB_TOKEN=ghp_xxxxxxxxxxxx

## Roadmap

- Go rewrite (single binary, no Python needed)
- Interactive sparse-folder picker
- GitLab / Bitbucket support
- Post-clone size verification

## License

MIT — see LICENSE.
