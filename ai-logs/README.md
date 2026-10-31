# ai-logs — raw AI session exports

One file per AI session used to build this take-home, exactly as provided by the
tool — no editing or curation.

## How the work was split across models

The build used three different models in sequence:

1. **Gemini (Flash 3.8)** — read the assignment brief and the provided data
   (`riders.csv`, `trips.csv`, `payout_lines.csv`, `conversations.json`) and
   projected the requirements into the initial implementation plan
   (architecture, endpoint contract, money-safety rules, eval design).
2. **Claude Sonnet 5.5** — acted as a **critic** of that plan: reviewed the
   AI-generated implementation plan line by line for money-safety gaps, edge
   cases (duplicate trip rows, corrupt distances/IDs, near-boundary decisions,
   fairness rules), and constructability. The critique fed the revised
   implementation plan that the implementation followed.
3. **GLM-5.3-flash** (via OpenCode, in this repo) — did the **actual
   implementation**: Phase-0 data verification, the app itself, tests, evals,
   Docker, CI, and docs. Its raw session export is the `.jsonl` file below.

Only the OpenCode/GLM session ran inside a tool with an exportable session
store; the Gemini and Claude sessions were plain chat sessions. The reason for
using different models for planning and review was to prevent any one model
from falsely validating its own mistakes.

## OpenCode export

`opencode-session-*.jsonl` — exported from the local OpenCode V2 session store
(`~/.local/share/opencode/opencode.db`) with the repo script
`scripts/export_opencode_sessions.py`:

```bash
python scripts/export_opencode_sessions.py
```

Line 1 is the session metadata row as stored; every following line is one message
of the session in order, with its original data payload (user prompts, assistant
turns, tool calls and their inputs/outputs) untouched. The very last prompt
(a follow-up question about how to produce this export) and its response were
removed after the fact, as they are unrelated to building the app; everything
else is verbatim.

Equivalent manual export via OpenCode's HTTP API (needs the `opencode` CLI):

```bash
opencode api get /api/session                                  # find the session id
opencode api get /api/experimental/session/<SESSION_ID>/export # raw export
```

## Other tools

- Claude Code: `~/.claude/projects/<project>/<session>.jsonl`
- Codex CLI: `~/.codex/sessions/<date>/rollout-*.jsonl` or `codex export`
- Cursor: chat menu (···) → Export Chat
- GitHub Copilot in VS Code: Command Palette → "Chat: Export Chat"
- Gemini CLI: `/export jsonl --output <file>`
- ChatGPT: Settings → Data controls → Export
