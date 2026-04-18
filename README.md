# clawsession-insights

A Claude Code skill that analyzes session logs and produces a structured Markdown report. Supports four session formats:
- **OpenClaw** JSONL logs
- **Claude Code CLI** JSONL logs
- **Langfuse trace** JSON arrays
- **Hermes Agent** JSON/JSONL logs

Point it at a session file (`.jsonl` or `.json`) and get a breakdown of what the agent did, where it got stuck, how time was spent, and what it cost.

---

## Example output

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Session: abc123
Date:    2026-03-27T14:05:32Z
Model:   claude-sonnet-4-6 (anthropic)
User:    alice
CWD:     /home/alice/myproject
Duration: 4m 12s

Stats
  Turns: 18  Tool calls: 34  Errors: 3

Timing
  LLM:   2m 44s  (65%)  avg 4120ms  max 18300ms
  CLI:   0m 47s  (19%)  avg 1380ms  max 8200ms
  User:  0m 22s  (9%)
  Idle:  0m 19s  (7%)

Loops detected: 1
  • pytest tests/ × 5 (error_loop) — 1m 23s

Errors: 3
  • [1] pytest tests/test_auth.py -v
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

The skill then asks 2–3 targeted questions based on what it found (loops, high error rates, unexpected session ends), and writes a full Markdown report next to your input file.

---

## Output File Naming Convention

The parser and skill follow a consistent naming scheme: all output files are placed **in the same directory as the input file**, using the input's basename as a prefix.

| Input format | Phase 1 output | Report output |
|---|---|---|
| `trace-XXXX.json` (Langfuse) | `trace-XXXX_parser_output.json` | `trace-XXXX_report.md` |
| `session.jsonl` (OpenClaw / Claude Code CLI) | `session_parser_output.json` | `session_report.md` |

**Example:**

```
Input:   trace-01993580-234a-4a55-b848-c83afb5861d3.json
Output:  trace-01993580-234a-4a55-b848-c83afb5861d3_parser_output.json
Report:  trace-01993580-234a-4a55-b848-c83afb5861d3_report.md
```

**Saving parser output manually:**

When running `analyze_session.py` directly, redirect stdout to a file using the same convention:

```bash
python3 analyze_session.py input.json > input_parser_output.json
```

---

## Requirements

- Python 3.10+ (stdlib only, no extra packages)
- Claude Code with skill support
- Session files in one of these formats:
  - OpenClaw JSONL (`.jsonl`)
  - Claude Code CLI JSONL (`.jsonl`)
  - Langfuse trace JSON array (`.json`)
  - Hermes Agent JSON/JSONL (`.json` or `.jsonl`)

---

## Installation

```bash
git clone https://github.com/jarosik9/clawsession-insights/ ~/.claude/skills/clawsession-insights
```

> **Note:** The destination path must be exactly `~/.claude/skills/clawsession-insights`. The skill has this path hardcoded — cloning elsewhere or renaming the directory will cause a "Parser not found" error.

Restart Claude Code after cloning.

---

## Usage

In a Claude Code session:

```
/clawsession-insights path/to/session.jsonl
```

The skill will:

1. **Parse** the session file and display a stats summary in the terminal.
2. **Ask questions** — 2–3 targeted questions based on the data (repeated commands, high error rates, abandoned tasks).
3. **Write a report** to `path/to/session_analysis.md` incorporating your answers.

---

## Report contents

| Section | What it covers |
|---------|---------------|
| Summary | Narrative of what the user tried to do and whether it succeeded |
| Conversation log | Timestamped user/assistant exchanges |
| UX friction points | Where the user or agent got stuck, with timestamps |
| Loops | Commands repeated 3+ times in a sliding window, classified as polling or error loops |
| Errors | Commands that exited non-zero, with truncated error output |
| Tool usage | Call counts per tool |
| Command log | Full chronological list of shell commands with status and duration |
| Performance & timing | LLM inference / CLI execution / user response / idle breakdown |

---

## How it works

`analyze_session.py` is a dependency-free Python script that parses session logs in multiple formats (OpenClaw, Claude Code CLI, Langfuse, Hermes). It extracts:

- **Session metadata** — model, user, working directory, duration
- **Conversation** — user and assistant text turns (tool calls stripped)
- **Commands** — all `exec` tool calls with exit codes and durations
- **Timing** — LLM inference time estimated from toolResult→assistant intervals; CLI time from command durations; user time from assistant→user intervals
- **Loops** — sliding-window detection of repeated normalized commands
- **Stats** — turn counts, token usage, and cost from assistant message metadata

The script outputs a single JSON object to stdout. The skill reads this and drives the interactive report generation.

---

## License

MIT
