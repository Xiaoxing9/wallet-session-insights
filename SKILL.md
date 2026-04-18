---
name: wallet-session-insights
description: "Analyze OpenClaw, Claude Code CLI, Langfuse trace, or Hermes Agent session logs. Optimized for Cobo Agentic Wallet sessions. MUST use when: user says 'analyze session', 'review my session', 'session report', 'what happened in this session', 'session quality', or provides a session file (.jsonl or .json). Segments tasks, scores agent quality (0-100), surfaces loops/errors/friction, asks targeted questions, writes a Markdown report. Supports four formats: OpenClaw JSONL, Claude Code CLI JSONL, Langfuse trace JSON, and Hermes Agent JSON/JSONL."
---

# Session Analyzer

**Turns a raw session log into an actionable quality report — so you know what the agent did well, where it got stuck, and what to fix.**

## Who This Is For

You are helping an AI agent developer or product manager understand what happened in a session. They want to know: did the agent complete the tasks? Where did it struggle? What patterns of failure or inefficiency occurred?

## When to Use / When NOT to Use

✅ Use when:
- User provides a session file: `.jsonl` (OpenClaw, Claude Code CLI, or Hermes) or `.json` (Langfuse trace or Hermes)
- User asks to "analyze", "review", or "report on" a session
- User asks about session quality or agent performance
- User wants to understand tool execution patterns, timing, or errors

❌ Do NOT use for:
- Live session monitoring (this is post-hoc analysis)
- Comparing two sessions side-by-side (use openclaw-eval-skill for A/B)
- Files not in supported formats (OpenClaw JSONL, Claude Code CLI JSONL, Langfuse JSON trace, Hermes JSON/JSONL)

## Prerequisites

- Python 3.10+ available on PATH
- Parser: `<SKILL_BASE_DIR>/analyze_session.py` (base directory is shown at the top of this skill)
- No external Python dependencies required (stdlib only)

---

## 🚨 Critical Rules

1. **Execute phases in order: 1 → 2 → 3 → 4 → 5 → 6 → 7. Do not skip or reorder.**
2. **Phase 4 is conditional** — only run it when triggered (see conditions below).
3. **Ask questions one at a time** in Phase 5 — wait for each answer before the next.
4. **Never invent friction points** — only report what the data shows.

---

## Quick Reference

```
/analyze-session <path-to-session.jsonl> [--since HH:MM] [--until HH:MM] [--silent]
```

| Parameter | Required | Description |
|-----------|----------|-------------|
| `<path>` | ✅ | Absolute or relative path to `.jsonl` session file |
| `--since HH:MM` | ❌ | Only analyze events after this time |
| `--until HH:MM` | ❌ | Only analyze events before this time |
| `--silent` | ❌ | Skip questions (Phase 5-6), go straight to report |

---

## Phase 1 — Run the parser

Run the Python parser to extract structured data from the JSONL:

```bash
python3 <SKILL_BASE_DIR>/analyze_session.py <input_path> [--since <arg>] [--until <arg>]
```

⚠️ **If the parser file doesn't exist**, tell the user:
> "Parser not found. Please install the skill first: https://github.com/jarosik9/clawsession-insights"

⚠️ **If the parser exits non-zero**, show the error and stop.

⚠️ **If the file is not a valid session format** (not OpenClaw, Claude Code CLI, Langfuse, or Hermes), the parser will error. Tell the user: "This file doesn't look like a supported session log format."

Capture the full JSON output. It contains: `session`, `stats`, `timing`, `loops`, `write_iterations`, `wasted_calls`, `recovery`, `hallucinations`, `errors`, `tool_usage`, `timeline[]`.

`timeline[]` is the single stream for all agent activity — interleaved and labeled. Each entry has:
- `kind`: `"thought"` (agent/user text) or `"command"` (tool call)
- `label` (thoughts only): `setup` | `pre_action` | `post_failure` | `recovery_plan` | `completion_claim` | `reasoning` | `thinking` | `visual_reply` | `user_input`
- `seq`: chronological sequence number
- `ts`: ISO timestamp

Use `timeline[]` for all downstream phases. Filter by `kind` and `label` as needed:
- Transcript for Phase 2: `kind == "thought"`, `role` in (`user`, `assistant`)
- Command log for Phase 3/7: `kind == "command"`
- Failure window for Phase 4: any `kind`, filtered by `ts` range
- Final summaries: `label == "visual_reply"`

---

## Phase 2 — Segment the conversation into tasks

You make one LLM call to identify what the user was working on.

**Read the full prompt template from:** `references/prompts.md` → "Task Segmentation Prompt"

**Transcript formatting rules** are also in `references/prompts.md` → "Transcript Formatting Rules"

**If the LLM returns invalid JSON**, retry once. If it fails again, set tasks to `[]` and note: "Task segmentation failed — proceeding with session-level metrics only."

**If the LLM returns `[]`** (no tasks detected), that's valid — some sessions are exploratory.

---

## Phase 3 — Enrich with math

Now that you have task time ranges from Phase 2, compute per-task and session-level metrics.

For each task, filter `timeline[]` where `kind == "command"` and `ts` falls in `[start_time, end_time]`.

### Efficiency

**efficiency_pct** — from command entries in timeline[]: exclude read-only (`ls`, `cat`, `head`, `tail`, `echo`, `pwd`, `which`, `grep`, `find`, `rg`). Of remaining: `round(exit_0_count / total × 100)`. No meaningful commands → `null` (display `—`).

### Quality score (4 dimensions, 0–100 each)

**dim_execution** — session-wide command success rate:
```
commands = timeline[] where kind == "command"
meaningful = exclude read-only (ls, cat, head, tail, echo, pwd, which, grep, find, rg)
             AND exclude process tool where action in (poll, log)
dim_execution = round(exit_0 / len(meaningful) × 100) if meaningful else 100
```

**dim_completion** — task completion rate:
```
dim_completion = round(completed / total × 100) if tasks else 100
```

**dim_depth** — session-level structural complexity. Computed once, shared by all tasks.

```
⚠️ Normalise tool names before computing:
  "exec" = "Bash", "read_file" = "Read", "write_file" = "Write",
  "edit_file" = "Edit", "web_search" = "WebSearch", "web_fetch" = "WebFetch"

A: tool_breadth (0–50)
  tool_types = distinct normalised tool names in tool_usage
  1 type   →  0  (but if total commands in timeline[] > 50: floor at 10)
  2–3      → 20
  4–5      → 35
  6+       → 50

B: external_call_density (0–25)
  Count timeline[] where kind=="command" and command text contains "http://", "https://", or "api."
  Exclude: "localhost", "127.0.0.1", "0.0.0.0"
  Add normalised "WebFetch" + "WebSearch" from tool_usage (deduplicate)
  0    →  0
  1–5  → 15
  6+   → 25

C: write_ratio (0–25)
  write_calls = normalised tool_usage["Write"] + ["Edit"]
  total_calls = sum(tool_usage.values())
  ratio = write_calls / total_calls if total > 0 else 0
  < 0.10   →  0
  0.10–0.30 → 15
  > 0.30   → 25

dim_depth = min(100, A + B + C)

depth_label:
  types ≥ 5, ext ≥ 6  → "deep integration"
  types ≥ 5, write > 0.30  → "heavy authoring"
  types ≥ 5             → "multi-tool"
  types ≥ 4, ext ≥ 1   → "multi-tool + external"
  types ≥ 4             → "multi-tool"
  types 2–3, write > 0.10 → "read-write"
  types 2–3             → "basic ops"
  types = 1             → "read-only"
```

**dim_ux** — user experience smoothness:
```
dim_ux = 100
  - confirmed context_loss: -10 each, cap -30
  - abandoned tasks: -8 each, cap -20
  - error_loop count > 10: -15 each, cap -30
  - error_loop 3–10: -5 each, cap -20
  - debugging_loop: -3 each, cap -15  (less harsh: agent is actively editing between retries)
  ⚠️ polling_loop, exploration_loop, and debugging_loop (with edits) do NOT count as error_loop.
  ⚠️ polling_loop and exploration_loop do NOT affect dim_ux.
dim_ux = max(0, dim_ux)
```

**Final score:**
```
quality_score = round((dim_execution + dim_completion + dim_depth + dim_ux) / 4)
Grade: 90–100 = A, 75–89 = B, 60–74 = C, 45–59 = D, <45 = F
```

If no tasks detected: `dim_completion = 100`. Note in report: "score based on session-level signals — no tasks detected."

### Additional metrics (from parser output)

The parser also outputs three operational metrics. You read them directly from the JSON — no computation needed:

**`wasted_calls`** — unnecessary CLI invocations (blind retries, help exploration, version checks, flag trial-and-error, env probing). Key fields: `waste_ratio`, `total`, `by_type`, `details[]`.

**`recovery`** — for each failed operation, did the same operation succeed within 2 subsequent attempts? Key fields: `recovery_rate`, `resolved`, `unresolved`, `correctly_abandoned`, `details[]`.

**`hallucinations`** — completion claims that contradict recent command results (agent says "done ✅" but last command failed). Key fields: `hallucination_rate`, `total_claims`, `hallucinations`, `details[]`.

---

## Phase 4 — Reasoning chain analysis (conditional)

This phase answers: **why did the agent make this decision?** Not just what happened.
It reads `timeline[]` — the interleaved stream of labeled agent thoughts and commands —
to extract the agent's stated beliefs, compare them to actual outcomes, and identify
what was missing at the system level.

**Trigger: any of these present**
- any `loops[]` with `loop_type == "error_loop"` (any count)
- any `recovery[].details` entry with `outcome == "unresolved"`
- `hallucinations.hallucinations > 0`

❌ Skip for: `polling_loop`, `exploration_loop`, `debugging_loop` only.

**Cap: max 4 LLM calls.** Priority order:
1. `error_loop` (most signal-rich — agent actively repeated a failing action)
2. `unresolved` recovery (agent gave up or mis-diagnosed)
3. `hallucination` (agent claimed success despite failure)
4. `abandoned` task (if task segmentation found one)

**How to extract the timeline window for each failure event:**

1. For `error_loop`: find the `start_time` and `end_time` from `loops[]`.
   Collect all `timeline[]` entries where `ts` falls in `[start_time - 90s, end_time + 60s]`.

2. For `unresolved` recovery: find the failing command timestamp from `recovery[].details`.
   Collect timeline entries in `[cmd_ts - 90s, cmd_ts + 120s]`.

3. For `hallucination`: find the claim timestamp from `hallucinations.details`.
   Collect timeline entries in `[claim_ts - 120s, claim_ts + 30s]`.

**Format the window** as described in `references/prompts.md` → "Timeline Formatting Rules for Phase 4".

**Run the prompt** from `references/prompts.md` → "Reasoning Chain Analysis Prompt".

**Store the result** as `root_cause` on the relevant loop/recovery/hallucination entry:
```json
{
  "what_happened": "...",
  "agent_belief": "...",
  "belief_correct": false,
  "missing_info": "...",
  "root_cause_category": "tool output gap",
  "root_cause_explanation": "..."
}
```

**If `timeline[]` contains entries with `label == "thinking"`:** supplement with legacy thinking prompts from
`references/prompts.md` → "Legacy Thinking Analysis Prompts" for any remaining
context_loss signals not covered above. Filter: `[e for e in timeline if e.get('label') == 'thinking']`.
These are lower priority.

---

## Phase 5 — Display stats and ask questions

**If `--silent` was passed**, display the stats block then skip to Phase 7.

### Stats block

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Session: <session.id>
Date:    <session.start_time>
Model:   <session.model> (<session.provider>)
User:    <session.user>
CWD:     <session.cwd>
Duration: <Xm Ys>

Quality: <score>/100 (<grade>)  ·  <N>/<N> tasks
  Execution  <bar>  <dim_execution>
  Completion <bar>  <dim_completion>
  Depth      <bar>  <dim_depth>  (<depth_label>)
  UX         <bar>  <dim_ux>
(bar = "█" × round(score/10) + "░" × (10 − round(score/10)))

Stats
  Turns: <total_turns>  Tool calls: <tool_calls>  Errors: <tool_errors>

Timing
  LLM:   <Xm Ys>  (<pct>%)  avg <avg>ms  max <max>ms
  CLI:   <Xm Ys>  (<pct>%)  avg <avg>ms  max <max>ms
  User:  <Xm Ys>  (<pct>%)
  Idle:  <Xm Ys>  (<pct>%)

Tasks detected: <N>  (covers <Xm> of <Ym session>)
  N. <title>  HH:MM→HH:MM  Xm  eff:XX%  [x]/[ ]

Loops detected: <count>
  • <command_normalized> × <count> (<loop_type>) — <Xm Ys>

Operational Metrics
  Waste:         <waste_ratio>% (<total>/<cmds> — <by_type summary>)
  Recovery:      <recovery_rate>% (<resolved> resolved, <unresolved> unresolved, <correctly_abandoned> abandoned)
  Hallucination: <hallucinations>/<total_claims> claims (<hallucination_rate>%) [🚨 if > 0]

Errors: <count>
  • [<exit_code>] <command truncated to 60 chars>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Questions

Ask **one at a time**, wait for each answer. Pick top 2–5 from this priority pool:

1. 🔴 Abandoned task: "Task N ('[title]') appears abandoned. What happened?"
2. 🔴 Error loop (error_loop only): "The agent repeated `<cmd>` × N times. Was this expected or a bug?"
3. 🟡 Low efficiency (<50%): "Task N had X% command success rate. Was the agent struggling?"
4. 🟡 Context loss: "Task N shows context loss: [desc]. Did you notice the agent losing track?"
6. ⚪ Fallback: "What was the goal of this session, and was it achieved?"

---

## Phase 6 — Collect answers

Hold all answers in conversation context. Do not write to any file.

**Skip if `--silent` was passed.**

---

## Phase 7 — Write the report

**Read the full report template from:** `references/report-template.md`

**Generate narrative sections** (Summary + UX Friction Points) with one LLM call. **Read the prompt from:** `references/prompts.md` → "Narrative Sections Prompt"

---

## ⚠️ Troubleshooting

| Problem | Cause | Fix |
|---------|-------|-----|
| `Parser not found` | Skill not installed | `pip install` or clone repo |
| Parser exits with `JSONDecodeError` | Not a valid JSONL file | Check the file is one-JSON-per-line |
| Parser exits with `KeyError: 'type'` | Unsupported format | Only OpenClaw, Claude Code CLI, Langfuse, and Hermes formats are supported |
| Phase 2 returns invalid JSON | LLM parsing error | Retry once; if still fails, proceed with `tasks = []` |
| All dim_depth values are low (10–20) | CLI-heavy session (all work in Bash) | Expected — structural signals measure tool diversity, not command complexity |
| Session has >500 commands | Very long session | Parser handles it, but Phase 2 transcript may be thinned |
| `--since`/`--until` returns empty | Time range doesn't overlap session | Check timestamps are in HH:MM local time matching session timezone |
