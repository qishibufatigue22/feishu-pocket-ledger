# Project workflow

- Keep financial amounts and expense records in Feishu. Do not commit credentials, runtime configuration, message receipts, or private logs.
- Changes must preserve exact calculations, replay protection, explicit withdrawal, and post-write verification.
- Run `python3 -m unittest discover -s tests -v` before releasing changes.
- The installed macOS runtime is separate from this checkout; update it with `python3 scripts/install_launch_agent.py` and verify the launchd service plus event listener.
- After each requested change, append the requirements and actual completed changes to the Feishu project log identified in `project_log.local.json` (ignored by Git). Use Asia/Shanghai date headings YYYY-MM-DD, without time-of-day. Read before appending; never overwrite prior entries. If work is unfinished or logging fails, state that explicitly.
- Keep a public, date-only CHANGELOG.md in sync, omitting private document links, account identifiers and ledger contents.
