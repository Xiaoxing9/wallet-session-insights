# wallet-session-insights

A Claude Code skill that turns raw agent session logs into actionable quality reports.

Paste in a session file, get back: what the agent did, where it struggled, how much it cost, and what to fix.

---

## What it does

Parses session logs from **OpenClaw**, **Claude Code CLI**, or **Langfuse** traces and produces:

- **Quality score** (0–100, A–F grade) across four dimensions: execution, task completion, session depth, and UX smoothness
- **Task segmentation** — automatically identifies what the user was trying to accomplish
- **Operational metrics** — waste ratio, recovery rate, hallucination rate
- **Loop detection** — polling loops, error loops, exploration loops with duration and command breakdown
- **Cost analysis** — per-task spend, burn rate, token usage
- **Markdown report** — saved alongside the session file for sharing or archiving

Optimized for **Cobo Agentic Wallet** sessions: recognizes `caw` CLI commands, classifies wallet operations (transfer, pact, onboard, query), and uses wallet-specific categories for depth scoring.

---

## Install

```bash
git clone <repo-url> ~/.claude/skills/wallet-session-insights
```

Restart Claude Code after cloning.

---

## Usage

```
/wallet-session-insights path/to/session.jsonl
```

Options:

| Flag | Description |
|------|-------------|
| `--since HH:MM` | Analyze only events after this time |
| `--until HH:MM` | Analyze only events before this time |
| `--silent` | Skip questions, write report immediately |

The skill will:

1. Parse the session file and display a stats summary
2. Ask 2–5 targeted questions based on what the data shows (loops, errors, abandoned tasks)
3. Write a full Markdown report to `path/to/session_analysis.md`

---

## Supported formats

| Format | File type | How to get it |
|--------|-----------|---------------|
| OpenClaw JSONL | `.jsonl` | OpenClaw session output |
| Claude Code CLI | `.jsonl` | `~/.claude/projects/.../sessions/` |
| Langfuse trace | `.json` | Exported from Langfuse UI |

---

## Quality score

Four dimensions, each 0–100:

- **Execution** — command success rate (excluding read-only ops)
- **Completion** — fraction of detected tasks marked complete
- **Depth** — structural complexity: tool diversity, external calls, write operations
- **UX** — smoothness: penalizes context loss, abandoned tasks, high-severity error loops

Grade scale: A ≥ 90 · B ≥ 75 · C ≥ 60 · D ≥ 45 · F < 45

---

## Operational metrics

Three signals parsed directly from the session log:

- **Waste ratio** — unnecessary CLI calls: blind retries, flag trial-and-error, help exploration, env probing
- **Recovery rate** — of failed operations, how many succeeded within 2 subsequent attempts
- **Hallucination rate** — completion claims ("done ✅") that contradict a recent failed command

---

## Requirements

- Python 3.10+ (standard library only, no pip install needed)
- Claude Code with skill support

---

## Version

Current: **v0.5.0** — supports OpenClaw, Claude Code CLI, and Langfuse trace formats.

See [CHANGELOG.md](CHANGELOG.md) for full history.
