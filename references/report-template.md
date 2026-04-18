# Report Template

Write the report to:
```
<absolute_directory_of_input_file>/<input_filename_without_extension>_analysis.md
```

If `--since` or `--until` was passed, include the range in the filename:
```
<stem>_14h30-16h00_analysis.md
```

## Report Structure

```markdown
# Session Analysis: <session.id>

**Quality: <score>/100 (<grade>)**  ·  <N>/<N> tasks
```
  Execution  <bar>  <dim_execution>
  Completion <bar>  <dim_completion>
  Depth      <bar>  <dim_depth>  (<depth_label>)
  UX         <bar>  <dim_ux>
```
**Date:** <date> | **Model:** <model> | **Duration:** <Xm Ys>
**User:** <user> | **CWD:** <cwd>
[> *Generated in silent mode — no user input collected.*]
[> *Time window: <HH:MM>–<HH:MM> — quality score covers this window only.*  (only when --since/--until)]

---

## Summary
<LLM-generated — see prompts.md>

## Task Breakdown
| # | Task | Duration | Efficiency | Status |
|---|------|----------|------------|--------|
<one row per task; efficiency = "—" for null>

## Issues

⚠️ Only include subsections that have content. If all are empty, write "No issues detected."

### Hallucinations
<only if hallucinations.hallucinations > 0>
| Claim | Last Command | Exit | Reason |
|-------|-------------|------|--------|
<one row per hallucination detail>

### Failed Recoveries
<only if recovery.unresolved > 0>
| Error | Operation | Attempts | Reason |
|-------|-----------|----------|--------|
<one row per unresolved detail>
<if correctly_abandoned > 0: "> 🔒 <N> permission errors correctly abandoned (not counted as failures).">

### Wasted Calls
<only if wasted_calls.total > 0>
Waste ratio: <waste_ratio>% (<total> calls — <by_type summary>)
| # | Type | Command | Reason |
|---|------|---------|--------|
<one row per wasted detail, max 10 rows — truncate with "... and N more">

### Loops
<only if loops exist>
| Normalized Command | Type | Count | Duration | Root Cause |
|--------------------|------|-------|----------|------------|

### UX Friction Points
<LLM-generated — see prompts.md>

## Conversation Log
<for each entry in conversation[]: "[HH:MM] ROLE: text truncated at 120 chars">
(Tool calls and tool results are not shown here.)

## Command Log
<partitioned by task if tasks detected; flat list otherwise>

### Task N — <title>
| Time | Command | Status | Duration |

### Other
| Time | Command | Status | Duration |

Duration for each row: `duration_ms` from `commands[]`, formatted as:
- <1000ms → `Xms` (e.g. `33ms`)
- ≥1000ms → `Xs` or `Xm Ys` (e.g. `10s`, `1m 35s`)
- Collapsed repeated rows (e.g. `×10`) → sum as `~Xs` or `~Xm Ys`
- No matching entry → `—`

## Appendix

### Performance & Timing

**Total duration:** <Xm Ys>

| Type | Total | % | Avg | Max |
|------|-------|---|-----|-----|
| LLM inference | <llm_ms as Xm Ys> | <pct>% | <avg>ms | <max>ms |
| CLI execution | <cli_ms as Xm Ys> | <pct>% | <avg>ms | <max>ms |
| User response | <user_ms as Xm Ys> | <pct>% | — | — |
| Idle / other  | <idle_ms as Xm Ys> | <pct>% | — | — |

**Tokens:** <total_tokens | "N/A" if tokens_unavailable>

### Tool Usage
| Tool | Calls |
|------|-------|

### Errors
<table if errors exist, else "None.">
| Time | Command | Exit Code | Error (first 80 chars) |
|------|---------|-----------|------------------------|

### Systemic Issues
<only include if any error matches a known pattern; omit entirely if none>
Flag generic patterns from `errors[]`:
- `error_text` contains "deprecated" → ⚠️ API deprecation
- `error_text` contains "permission denied" or "403" → ⚠️ Permission boundary
- `error_text` contains "timeout" or "ETIMEDOUT" → ⚠️ Network timeout
- `error_text` contains "not found" and ("command" or "module") → ⚠️ Missing dependency

For each match: `- **[pattern name]:** <command truncated to 60 chars> — <brief explanation>`
```

## Completion Message

After writing the report, print:
```
✓ Report written to <output_path>
  Quality: <score>/100 (<grade>)  ·  <N>/<N> tasks
  Issues: <hallucinations> hallucinations, <unresolved> failed recoveries, <wasted> wasted calls
  Duration breakdown: LLM <pct>% / CLI <pct>% / User <pct>% / Idle <pct>%
```
