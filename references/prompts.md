# LLM Prompts

## Task Segmentation Prompt (Phase 2)

```
Below is a transcript of a Claude Code session with timestamps.

TASK SEGMENTATION:
Identify the distinct tasks the user was working on. A task must have a clear,
actionable goal delegated to the agent. Do NOT create tasks for casual questions,
clarifications, or acknowledgements. Not all turns need to belong to a task.
Prefer fewer, broader tasks over over-splitting.

A new task starts when the user shifts to a clearly different actionable goal.
Retries, corrections, and follow-ups within the same goal are part of the same task.
Task time ranges must be non-overlapping. If two tasks share a boundary, end the
earlier task at the timestamp where the new one begins.

CONTEXT LOSS:
For each task, identify "repeated_question" signals only: cases where the agent
asked the user for the same information more than once within this task.

For each task output:
- index (1-based integer)
- title (short verb phrase, ≤8 words)
- start_time, end_time (ISO timestamps; non-overlapping)
- status: "completed" | "abandoned" | "unclear"
- context_loss: array of {type, description}, or []

Return JSON array only, no prose. Return [] if no clear tasks found.

TRANSCRIPT:
<formatted transcript>
```

## Transcript Formatting Rules

Build transcript from `timeline[]` where `kind == "thought"`:
- Format each entry as `[HH:MM] ROLE: text`
  - For `label == "visual_reply"`: prefix with `[SUMMARY] ASSISTANT:` — these are the agent's final markdown replies to the user
  - For `label == "thinking"`: prefix with `[THINKING] ASSISTANT:` — internal reasoning blocks
  - All others: `USER:` or `ASSISTANT:` based on `role`
- Truncate: `user_input` turns at 400 chars, assistant turns at 600 chars, `visual_reply` at 800 chars (more content needed for task understanding)
- If total transcript exceeds 20 000 chars, reduce to 300/350 for regular turns
- Always preserve the last `user_input` entry in full
- If >80 thought entries: keep all `user_input` and `visual_reply`, keep every other regular assistant turn, always keep last 10 entries
- Add note: "transcript thinned — some assistant turns omitted."

## Reasoning Chain Analysis Prompt (Phase 4)

This prompt is used once per failure event (error_loop, unresolved recovery, hallucination).
The timeline window is extracted from `timeline[]` around the failure — typically 4 thought
entries before, all commands in the failure window, and 4 thought entries after.

Timeline entry format:
- `[seq] [HH:MM] THOUGHT [label]: "text"` — agent reasoning, labeled by type
- `[seq] [HH:MM] COMMAND [tool] exit=N: "command" → "error"` — tool call result

```
You are a senior engineer doing root cause analysis on an AI agent failure.
Below is a timeline slice from a session. Read it carefully — it shows what the
agent said AND what actually happened at each step.

FAILURE EVENT: <type: error_loop | unresolved_recovery | hallucination>
DESCRIPTION: <e.g. "agent repeated caw.tx.call × 7 with exit=1">

TIMELINE:
<formatted timeline window — interleaved thoughts and commands>

Answer the following 5 questions. Be specific. Quote directly from the timeline.
Do NOT invent details not present in the data.

1. WHAT HAPPENED (mechanical)
   One sentence: what did the agent try to do, and what was the outcome?

2. AGENT'S STATED BELIEF
   Quote the timeline entry where the agent expressed its interpretation of the
   failure. What did the agent think was wrong?

3. WAS THAT BELIEF CORRECT?
   Compare the agent's stated diagnosis against the actual command output.
   If incorrect: what was the real problem the agent missed?

4. WHAT INFORMATION WAS MISSING?
   What specific output, preflight check, or tool response — if present —
   would have corrected the agent's belief before or immediately after the
   first failure? Be concrete: "caw tx.get should have returned decoded revert
   reason" is better than "better error handling".

5. ROOT CAUSE CATEGORY
   Classify the underlying cause as one of:
   (a) skill/prompt gap — agent lacked knowledge or instruction to handle this case
   (b) tool output gap — the tool returned insufficient information to act correctly
   (c) state machine gap — agent was not forced to stop/decide at a critical node
   (d) DeFi/protocol gap — agent lacked protocol-specific knowledge (quote/preflight/cap)
   Explain in one sentence why you chose this category.
```

### Timeline Formatting Rules for Phase 4

Extract from `timeline[]` a window around each failure event:
- Find the first command in the failure window (error_loop start / unresolved recovery command)
- Include: 4 `thought` entries immediately before that command
- Include: all `command` entries in the failure window (up to 10)
- Include: 4 `thought` entries immediately after the last failure command
- Format each entry as:
  - `[seq] [HH:MM] THOUGHT [label]: "text (truncated to 200 chars)"`
  - `[seq] [HH:MM] COMMAND [tool] exit=N [★ CRITICAL]: "cmd" → "error (truncated to 150 chars)"`
  - Mark is_critical=true commands with `★ CRITICAL`
- If window > 30 entries: keep all commands, thin thoughts to every other one

### Legacy Thinking Analysis Prompts (fallback when thinking[] available)

#### Loop trigger (thinking-based)
```
The agent repeated "<command_normalized>" × N times between <start> and <end>.
Below are the agent's thinking blocks during this period.
In 1–2 sentences: why did the agent keep repeating this? Was it aware of the loop?

<thinking entries>
```

#### Abandoned task trigger (thinking-based)
```
The agent was working on "<title>" but did not complete it.
Below are the agent's last 3 thinking blocks before the task ended.
In 1–2 sentences: what caused the agent to stop?

<last 3 thinking entries before end_time>
```

#### Context loss trigger (thinking-based)
```
The agent showed signs of context loss during "<title>": <description>.
Below are relevant thinking blocks.
In 1–2 sentences: does the thinking confirm genuine context loss or intentional re-check?

<thinking entries in task time range>
```

## Narrative Sections Prompt (Phase 7)

```
You are writing two sections of a session analysis report.

SESSION DATA:
- Quality: <score>/100 (<grade>)<quality_note if no tasks>
- Tasks: <N completed> completed, <N abandoned> abandoned
- Loops: <list with type and count, or "none">
- Error rate: <tool_errors>/<tool_calls> commands failed
- User answers: <answers, or "none — silent mode">

TASKS:
<task list: index, title, status, efficiency_pct, context_loss, root_cause>

WRITE:

## Summary
3–5 sentences. Cover: (1) what the user was trying to accomplish; (2) what the
agent did; (3) overall outcome referencing quality score. Do not list tasks.
If silent mode, note where intent was inferred rather than stated.

## UX Friction Points
Bullet points for friction only. Per bullet: **Task N [type]:** description.
Add > *Thinking: root_cause* blockquote if available.
Incorporate user answers where relevant.
Write "None detected." if nothing to report.
Do not invent friction points — only report what the data shows.
Signals to cover in order: context_loss, low efficiency (<50%), error_loops (NOT polling_loop or exploration_loop).
Skip signals the user confirmed were expected.
```
