#!/usr/bin/env python3
"""
Multi-format session analyzer: OpenClaw JSONL, Claude Code CLI JSONL, Langfuse trace JSON, and Hermes JSON/JSONL.

Supports four input formats:
  - OpenClaw JSONL: traditional log format from OpenClaw platform
  - Claude Code CLI JSONL: native Claude Code CLI session logs
  - Langfuse trace JSON: JSON array of SPAN/GENERATION items from Langfuse platform
  - Hermes JSON/JSONL: Cobo Hermes agent sessions (OpenAI function-calling format)

Usage:
  python3 analyze_session.py <path-to-session.jsonl|trace.json> [--since HH:MM] [--until HH:MM]

Output:
  JSON summary to stdout with fields: session, stats, timing, loops, errors, commands, tool_usage

Requirements:
  - Python 3.10+ (stdlib only)
  - Input file must be valid JSONL or JSON array
"""
import json
import sys
import re
import shlex
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta


# Hermes tool name → internal tool type mapping
HERMES_TOOL_MAP = {
    'terminal':         'exec',
    'read_file':        'read',
    'write_file':       'write',
    'patch':            'edit',
    'search_files':     'read',
    'execute_code':     'exec',
    'skill_view':       'exec',
    'skills_list':      'exec',
    'skill_manage':     'exec',
    'browser_navigate': 'web_fetch',
    'browser_scroll':   'web_fetch',
    'browser_click':    'web_fetch',
    'browser_console':  'web_fetch',
    'browser_snapshot': 'web_fetch',
    'browser_get_images': 'web_fetch',
    'todo':             'exec',
    'memory':           'exec',
    'clarify':          'exec',
    'cronjob':          'exec',
}


# FIX: BUG-09 — map caw.* span names (CAW CLI operations in Langfuse format) to tool types
# caw.* spans are recognized by prefix match (BUG-12), no explicit map needed.
# All caw CLI operations map to tool type 'exec'.


def parse_timestamp(ts_str):
    """Parse ISO timestamp string to epoch milliseconds."""
    if ts_str.endswith('Z'):
        ts_str = ts_str[:-1] + '+00:00'
    return int(datetime.fromisoformat(ts_str).timestamp() * 1000)


def detect_format(path):
    """
    Detect file format: JSONL, JSON array (Langfuse trace), or Hermes JSON object.
    Returns 'jsonl', 'trace', or 'hermes-json'.

    Hermes JSON files start with '{' + newline (multi-line JSON object).
    JSONL files have a complete JSON object on each line.
    Langfuse trace files start with '['.
    """
    with open(path, 'r', encoding='utf-8') as f:
        first_line = f.readline()
    first_stripped = first_line.strip()
    if first_stripped.startswith('['):
        return 'trace'
    elif first_stripped == '{':
        # Opening brace alone on a line → multi-line JSON object (Hermes JSON)
        return 'hermes-json'
    elif first_stripped.startswith('{'):
        # Try to parse the first line as complete JSON (JSONL check)
        try:
            json.loads(first_stripped)
            return 'jsonl'
        except json.JSONDecodeError:
            return 'hermes-json'
    raise ValueError(f"Unrecognized format in {path}: expected JSONL or JSON array")


def detect_jsonl_subformat(events):
    """
    Detect JSONL subformat: OpenClaw, Claude Code CLI, or Hermes.
    Returns 'openclaw', 'claude-code-cli', or 'hermes'.

    Priority order:
    1. Hermes: events use 'role' field (user/assistant/tool/session_meta) without 'type',
       and assistant messages use tool_calls[] OpenAI function-calling format.
    2. Claude Code CLI: has 'permission-mode', 'tool_use', or 'tool_result' event types.
    3. OpenClaw: has 'session' event type or 'toolCall' in message content.
    """
    if not events:
        return 'openclaw'

    # First pass: check for Hermes signals (highest priority)
    for event in events[:20]:
        event_role = event.get('role', '')
        event_type = event.get('type', '')
        if event_role in ('session_meta', 'tool') and not event_type:
            return 'hermes'
        if event_role == 'assistant' and 'tool_calls' in event and not event_type:
            return 'hermes'
        if event_role == 'user' and not event_type and 'message' not in event:
            # Hermes user events: {role, content, timestamp} — no 'type', no nested 'message'
            # Peek further for assistant with tool_calls to confirm hermes
            for e2 in events[:20]:
                if e2.get('role') == 'assistant' and 'tool_calls' in e2:
                    return 'hermes'
            break  # user-without-type but no tool_calls found → not hermes

    # Second pass: check for CLI signals
    for event in events[:20]:
        event_type = event.get('type', '')
        if event_type in ('permission-mode', 'tool_use', 'tool_result'):
            return 'claude-code-cli'

    # Third pass: check for OpenClaw signals
    for event in events[:20]:
        event_type = event.get('type', '')
        if event_type == 'session':
            return 'openclaw'
        if event_type == 'message':
            msg = event.get('message', {})
            content = msg.get('content', [])
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get('type') == 'toolCall':
                        return 'openclaw'

    session_id_count = sum(1 for e in events[:10] if 'sessionId' in e)
    if session_id_count > 5:
        return 'claude-code-cli'

    return 'openclaw'


def convert_trace_to_events(trace_data):
    """
    Convert Langfuse trace array to OpenClaw-compatible event format.
    Extracts session metadata, turns, and token costs from trace.
    """
    events = []
    session_start = None
    session_end = None
    model = "unknown"
    provider = "unknown"
    user = "unknown"

    # First pass: extract session metadata and build turn map
    turn_by_id = {}
    for item in trace_data:
        item_type = item.get('type', '')
        name = item.get('name', '')

        # Capture session bounds
        if item_type == 'SPAN' and name.startswith('session:'):
            session_start = item.get('startTime')
            session_end = item.get('endTime')
            # Extract model/provider from metadata
            try:
                metadata_str = item.get('metadata', '{}')
                if isinstance(metadata_str, str):
                    metadata = json.loads(metadata_str)
                else:
                    metadata = metadata_str
                model = metadata.get('attributes', {}).get('langfuse.trace.metadata.model', 'unknown')
                provider = metadata.get('attributes', {}).get('langfuse.trace.metadata.provider', 'unknown')
                user_id = metadata.get('attributes', {}).get('langfuse.trace.user_id', 'unknown')
                if user_id != 'unknown':
                    user = user_id
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Index turns by ID
        if item_type == 'SPAN' and name.startswith('turn:'):
            turn_by_id[item.get('id')] = item

    # Create session event
    if session_start:
        events.append({
            "type": "session",
            "timestamp": session_start,
            "id": "trace-session",
            "cwd": "",
            "model": model,
            "provider": provider,
            "user": user,
        })

    # Convert turns to message events
    for item in trace_data:
        item_type = item.get('type', '')
        name = item.get('name', '')

        if item_type == 'SPAN' and name.startswith('turn:'):
            try:
                input_str = item.get('input', '{}')
                if isinstance(input_str, str):
                    input_data = json.loads(input_str)
                else:
                    input_data = input_str

                output_str = item.get('output', '{}')
                if isinstance(output_str, str):
                    output_data = json.loads(output_str)
                else:
                    output_data = output_str

                # Extract role and content
                role = input_data.get('role', 'unknown')
                content = input_data.get('content', '')
                output_content = output_data.get('content', '') if isinstance(output_data, dict) else output_data

                # Create message event
                msg_event = {
                    "type": "message",
                    "timestamp": item.get('startTime'),
                    "message": {
                        "role": role,
                        "content": [{"type": "text", "text": str(content)}] if content else [],
                    }
                }
                events.append(msg_event)

                # TURN output is the agent's synthesized visual reply to the user.
                # Long outputs (>120 chars) are unique markdown summaries, not duplicates
                # of GENERATION spans (which are brief pre-tool-call statements).
                # Add as label "visual_reply" so Phase 2/4 can distinguish final summaries
                # from intermediate reasoning.
                if output_content and role == 'user' and len(str(output_content)) > 120:
                    events.append({
                        "type": "message",
                        "timestamp": item.get('endTime'),
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": str(output_content),
                                         "label": "visual_reply"}],
                        }
                    })
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Extract text + token costs from GENERATION spans
        if item_type == 'GENERATION':
            try:
                # FIX: BUG-08 — extract agent reasoning text from each GENERATION span
                output_raw = item.get('output', '')
                gen_text = ''
                if output_raw:
                    try:
                        parsed = json.loads(output_raw) if isinstance(output_raw, str) else output_raw
                        if isinstance(parsed, list) and parsed:
                            # Format: ["agent text", "tool_name"]
                            gen_text = str(parsed[0]) if parsed[0] else ''
                        elif isinstance(parsed, str):
                            gen_text = parsed
                        elif isinstance(parsed, dict):
                            gen_text = parsed.get('content', parsed.get('text', ''))
                    except Exception:
                        gen_text = str(output_raw) if output_raw else ''

                if gen_text and gen_text.strip():
                    events.append({
                        "type": "message",
                        "timestamp": item.get('startTime'),
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": gen_text.strip()}],
                        }
                    })

                # Extract token costs
                metadata_str = item.get('metadata', '{}')
                if isinstance(metadata_str, str):
                    metadata = json.loads(metadata_str)
                else:
                    metadata = metadata_str

                attrs = metadata.get('attributes', {})
                input_tokens = int(attrs.get('gen_ai.usage.input_tokens', 0))
                output_tokens = int(attrs.get('gen_ai.usage.output_tokens', 0))

                if input_tokens > 0 or output_tokens > 0:
                    events.append({
                        "type": "message",
                        "timestamp": item.get('startTime'),
                        "message": {
                            "role": "assistant",
                            "content": [],
                            "usage": {
                                "totalTokens": input_tokens + output_tokens,
                                "cost": {"total": 0.0}
                            }
                        }
                    })
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # M4: Extract tool execution data from SPAN items
        # Tool SPANs have format: "tool_type:description (id)" in name
        if item_type == 'SPAN' and ':' in name and not name.startswith(('session:', 'turn:')):
            try:
                tool_type = name.split(':')[0].lower()

                # Recognize tool types from trace
                # FIX: BUG-10 — network_curl:exec and network_*:exec spans now recognized.
                # tool_type is the prefix before the first colon (e.g. "network_curl" from
                # "network_curl:exec (uuid)"). Map any network_* prefix to exec so it
                # flows through the standard SPAN handling. tool_name in metadata (or the
                # tool_type itself) is preserved so timeline shows "network_curl" not "exec".
                _is_network = tool_type.startswith('network_')
                if tool_type in ('exec', 'edit', 'read', 'write', 'web_fetch', 'web_search', 'skill_install', 'file_read', 'memory_search', 'process_poll', 'env_bootstrap') or _is_network:
                    # Parse metadata
                    metadata = {}
                    if 'metadata' in item:
                        try:
                            meta_str = item['metadata']
                            metadata = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass

                    # Parse input
                    input_data = item.get('input', {})
                    if isinstance(input_data, str):
                        try:
                            input_data = json.loads(input_data)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            input_data = {'raw': input_data}

                    # Normalize tool name for internal format.
                    # FIX: BUG-10 — for network_* spans, prefer the span's tool_type prefix
                    # (e.g. "network_curl") over metadata.tool_name (which is "exec") so
                    # that timeline entries display the specific network tool, not generic exec.
                    if _is_network:
                        # Use category from metadata if present (e.g. "network_curl"), else tool_type
                        _net_tool = metadata.get('category', tool_type)
                        normalized_tool = normalize_tool_name(_net_tool)
                    else:
                        normalized_tool = normalize_tool_name(metadata.get('tool_name', tool_type))

                    # Create tool_use event
                    tool_call_id = metadata.get('tool_call_id', item.get('id', ''))
                    tool_event = {
                        "type": "tool_use",
                        "toolCallId": tool_call_id,
                        "toolName": normalized_tool,
                        "input": input_data,
                        "timestamp": item.get('startTime')
                    }
                    events.append(tool_event)

                    # Create tool_result event (same SPAN)
                    if item.get('output') is not None:
                        result_event = {
                            "type": "tool_result",
                            "toolCallId": tool_call_id,
                            "exitCode": metadata.get('exit_code', 0),
                            "output": item.get('output', ''),
                            "timestamp": item.get('endTime')
                        }
                        events.append(result_event)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # FIX: BUG-09 — capture caw.* spans (CAW CLI operations in Langfuse format)
        # FIX: BUG-12 — prefix match instead of exact match for caw.* spans
        if item_type == 'SPAN' and '.' in name and ':' not in name:
            caw_tool = name.split(' ')[0]  # strip trailing " (uuid)"
            # Sanitize malformed span names (e.g. "caw.>/dev/null.2>&1;")
            caw_tool_clean = caw_tool.split('>')[0].split('|')[0].split(';')[0].split('&')[0].strip()
            if not caw_tool_clean or caw_tool_clean == 'caw.':
                caw_tool_clean = 'caw.unknown'
            if caw_tool_clean.startswith('caw.'):
                try:
                    output_str = item.get('output', '')
                    output_text = output_str if isinstance(output_str, str) else json.dumps(output_str)

                    # Infer exit code from output content
                    exit_code = 0
                    if output_text:
                        if '"error": true' in output_text or 'INVALID_PARAMETER' in output_text or \
                           'UNKNOWN_ERROR' in output_text or 'Command exited with code 1' in output_text:
                            exit_code = 1

                    # Build command string: use caw_tool name + abbreviated input as context
                    command_str = caw_tool_clean  # e.g. "caw.tx.call"
                    input_raw = item.get('input', '')
                    if input_raw:
                        input_str = input_raw if isinstance(input_raw, str) else json.dumps(input_raw)
                        command_str = f"{caw_tool_clean} {input_str[:200]}"

                    events.append({
                        "type": "tool_result",
                        "timestamp": item.get('startTime'),
                        "end_timestamp": item.get('endTime'),
                        "tool": "exec",  # all caw.* spans are exec-type
                        "tool_name": caw_tool_clean,
                        "command": command_str,
                        "output": output_text[:500] if output_text else '',
                        "exit_code": exit_code,
                        "duration_ms": int((
                            parse_timestamp(item.get('endTime', '')) -
                            parse_timestamp(item.get('startTime', ''))
                        )) if item.get('startTime') and item.get('endTime') else 0,
                        "status": "ok" if exit_code == 0 else "error",
                    })
                except Exception:
                    pass

    # Add final event for session end
    if session_end:
        events.append({
            "type": "session-end",
            "timestamp": session_end,
        })

    return events if events else []


def convert_hermes_json_to_events(path):
    """
    Convert Hermes JSON object format to a flat event list compatible with the rest of the parser.
    Hermes JSON: single dict with session_id, model, platform, messages[], etc.
    Returns a list of events in hermes-JSONL-compatible format.
    """
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    base_ts = data.get('session_start', '')
    session_id = data.get('session_id', Path(path).stem)

    # Synthetic session_meta event (mirrors hermes JSONL first line)
    events = [{
        'role': 'session_meta',
        'model': data.get('model', ''),
        'platform': data.get('platform', ''),
        'timestamp': base_ts,
        'session_id': session_id,
        '_hermes_json': True,
    }]

    for msg in data.get('messages', []):
        event = dict(msg)
        # JSON format has no per-message timestamps — use session_start as placeholder
        if 'timestamp' not in event:
            event['timestamp'] = base_ts
        events.append(event)

    return events


def load_events(path):
    """Load events from JSONL, trace JSON, or Hermes JSON file. Returns list of dicts."""
    file_format = detect_format(path)

    if file_format == 'trace':
        with open(path, 'r', encoding='utf-8') as f:
            trace_data = json.load(f)
        return convert_trace_to_events(trace_data)

    elif file_format == 'hermes-json':
        return convert_hermes_json_to_events(path)

    else:
        # JSONL: OpenClaw, Claude Code CLI, or Hermes JSONL
        events = []
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))

        # Hermes JSONL: first event may be session_meta (no timestamp) or user (has timestamp).
        # Keep all events — session_meta is needed for metadata extraction.
        # For non-hermes: filter to first event with timestamp (Claude Code CLI fix).
        sub_format = detect_jsonl_subformat(events)
        if sub_format != 'hermes':
            first_ts_idx = 0
            for i, e in enumerate(events):
                if 'timestamp' in e:
                    first_ts_idx = i
                    break
            events = events[first_ts_idx:]

        if not events:
            raise ValueError(f"No events found in {path}")

        return events


def extract_user_claude_code(events):
    """
    Extract user identity from Claude Code CLI session.

    Priority order:
    1. Check for explicit 'user' field (from trace conversion)
    2. Check 'entrypoint' field (cli, web, etc.)
    3. Regex extraction from message content
    """
    for e in events:
        # Priority 1: explicit user field
        if 'user' in e and e.get('user') not in ('unknown', None):
            return e['user']
        # Priority 2: entrypoint field
        if e.get('entrypoint') == 'cli':
            return 'cli'

    # Priority 3: Try to extract from message content
    for e in events:
        if e.get('type') != 'message':
            continue
        msg = e.get('message', {})
        if msg.get('role') != 'user':
            continue
        content = msg.get('content', [])
        if isinstance(content, str):
            # Sometimes Claude Code CLI embeds user in message string
            if 'User:' in content or 'user:' in content:
                return 'user'
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'text':
                    text = item.get('text', '')
                    # Check for sender pattern like in OpenClaw
                    match = re.search(r'"sender"\s*:\s*"([^"]+)"', text)
                    if match:
                        return match.group(1)

    return 'unknown'


def extract_session_meta(events):
    """
    Extract session metadata from events.
    Supports both OpenClaw JSONL and Claude Code CLI JSONL formats.
    """
    if not events:
        raise ValueError("No events found in session file")

    # Detect format
    sub_format = detect_jsonl_subformat(events)

    # Find last event with a real (non-placeholder) timestamp
    last_event = events[-1]
    end_ms = parse_timestamp(last_event["timestamp"])

    if sub_format == 'hermes':
        meta = events[0] if events[0].get('role') == 'session_meta' else {}
        session_id = meta.get('session_id', '')
        model = meta.get('model', 'unknown')
        platform = meta.get('platform', 'unknown')
        base_ts = meta.get('timestamp', '') or events[0].get('timestamp', '')
        # Find first non-meta event with timestamp for start
        start_ts = next(
            (e['timestamp'] for e in events if e.get('role') != 'session_meta' and e.get('timestamp')),
            base_ts
        )
        start_ms = parse_timestamp(start_ts) if start_ts else end_ms
        cwd = ''
        provider = 'hermes'
        user = f'hermes/{platform}' if platform and platform != 'unknown' else 'hermes'

    elif sub_format == 'claude-code-cli':
        # Claude Code CLI format
        session_id = ""
        for e in events:
            if 'sessionId' in e:
                session_id = e['sessionId']
                break

        start_ms = parse_timestamp(events[0]["timestamp"])
        cwd = ""
        for e in events:
            if 'cwd' in e:
                cwd = e['cwd']
                break

        model = "unknown"
        provider = "unknown"
        for e in events:
            if e.get('type') == 'model-snapshot':
                model = e.get('data', {}).get('modelId', 'unknown')
                provider = e.get('data', {}).get('provider', 'unknown')
                break

        user = extract_user_claude_code(events)

    else:
        # OpenClaw format
        session_event = events[0]
        session_id = session_event.get('id', '')
        start_ms = parse_timestamp(session_event["timestamp"])
        cwd = session_event.get("cwd", "")

        model = "unknown"
        provider = "unknown"
        for e in events:
            if e.get("type") == "custom" and e.get("customType") == "model-snapshot":
                model = e.get("data", {}).get("modelId", "unknown")
                provider = e.get("data", {}).get("provider", "unknown")
                break

        # Extract user from OpenClaw message metadata block
        user = "unknown"
        for e in events:
            if e.get("type") != "message":
                continue
            msg = e.get("message", {})
            if msg.get("role") != "user":
                continue
            content = msg.get("content", [])
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = item.get("text", "")
                match = re.search(r'"sender"\s*:\s*"([^"]+)"', text)
                if match:
                    user = match.group(1)
                    break
            if user != "unknown":
                break

    # Redact home directory prefix to avoid leaking username in shared reports.
    # Matches /home/<user>/..., /Users/<user>/..., /root/...
    cwd = re.sub(r'^(/home/[^/]+|/Users/[^/]+|/root)', '~', cwd)

    return {
        "id": session_id,
        "start_time": events[0]["timestamp"],
        "end_time": last_event["timestamp"],
        "duration_ms": end_ms - start_ms,
        "cwd": cwd,
        "model": model,
        "provider": provider,
        "user": user,
    }


def extract_text_from_content(content):
    """Extract plain text from a message content list, ignoring toolCall items."""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item["text"].strip())
    return " ".join(parts).strip()


def label_conversation_message(text):
    """
    Classify an assistant message by its semantic role in the execution flow.
    Used to build timeline labels that help Phase 4 reasoning chain analysis.

    Labels:
      setup           - initial orientation: reading docs, checking status
      pre_action      - agent states intent to execute something
      post_failure    - agent reacts to a failed command (diagnosis attempt)
      recovery_plan   - agent proposes alternative approach after failure
      completion_claim - agent claims task success
      reasoning       - general thinking that doesn't fit above
    """
    t = text.lower()

    completion_signals = ['✅', '✓', 'success', 'successfully', 'completed',
                          'transaction confirmed', 'transfer complete',
                          '成功', '完成了', '已完成']
    recovery_signals   = ['try another', 'try a different', 'instead', 'alternatively',
                          'switch to', 'let me try', 'another approach',
                          '换', '尝试另', '改用', '换一种', '换一个']
    failure_signals    = ['failed', 'error', 'invalid', "doesn't work", 'not work',
                          'incorrect', 'wrong', 'issue', 'problem', 'unable',
                          'cannot', "can't",
                          '失败', '错误', '不对', '问题', '无法', '不能']
    action_signals     = ["let me", "i'll", "i will", "now i", "next i",
                          "going to", "about to", "i'm going",
                          '让我', '我来', '我会', '现在', '接下来', '接下来我']
    setup_signals      = ['skill.md', 'onboarding', 'first', 'start by', 'begin',
                          'let me check', 'checking', 'reading',
                          '先查', '首先', '让我先', '先了解']

    if any(s in text for s in completion_signals):
        return 'completion_claim'
    if any(s in t for s in recovery_signals):
        return 'recovery_plan'
    if any(s in t for s in failure_signals):
        return 'post_failure'
    if any(s in t for s in action_signals):
        return 'pre_action'
    if any(s in t for s in setup_signals):
        return 'setup'
    return 'reasoning'


def build_timeline(conversation, commands, thinking=None):
    """
    Merge conversation and commands into a single chronological timeline.
    Each entry is tagged with kind (thought/command) and a semantic label.
    This is the primary input for Phase 4 reasoning chain analysis.

    Thought labels: setup | pre_action | post_failure | recovery_plan |
                    completion_claim | reasoning | user_input
    Command flags:  is_critical=True for tx/pact submit calls
    """
    CRITICAL_TOOL_PREFIXES = ('caw.tx.', 'caw.pact.submit')
    CRITICAL_TOOLS = {'caw.pact.submit', 'caw.pact.show', 'caw.tx.call',
                      'caw.tx.transfer', 'caw.tx.speedup'}

    events = []

    for msg in conversation:
        role = msg.get('role', 'assistant')
        text = msg.get('text', '')
        # Respect explicit label (e.g. visual_reply) over heuristic classification
        explicit = msg.get('label')
        if explicit:
            label = explicit
        elif role == 'assistant':
            label = label_conversation_message(text)
        else:
            label = 'user_input'
        events.append({
            'ts': msg['timestamp'],
            'kind': 'thought',
            'role': role,
            'label': label,
            'text': text[:800] if label == 'visual_reply' else text[:400],
        })

    for cmd in commands:
        tool_name = cmd.get('tool_name') or cmd.get('tool', '')
        is_critical = (
            any(tool_name.startswith(p) for p in CRITICAL_TOOL_PREFIXES) or
            tool_name in CRITICAL_TOOLS or
            (cmd.get('tool') == 'exec' and cmd.get('exit_code', 0) != 0)
        )
        events.append({
            'ts': cmd.get('timestamp', ''),
            'kind': 'command',
            'tool': tool_name,
            'command': cmd.get('command', '')[:120],
            'exit_code': cmd.get('exit_code', 0),
            'error_text': (cmd.get('error_text', '')[:200]
                           if cmd.get('exit_code', 0) != 0 else ''),
            'is_critical': is_critical,
        })

    # Merge thinking[] entries if provided (label: "thinking")
    if thinking:
        for t in thinking:
            events.append({
                'ts': t.get('timestamp', ''),
                'kind': 'thought',
                'role': 'assistant',
                'label': 'thinking',
                'text': t.get('text', '')[:400],
            })

    events.sort(key=lambda x: parse_timestamp(x['ts']) if x['ts'] else 0)
    for i, e in enumerate(events):
        e['seq'] = i + 1
    return events


def extract_conversation(events):
    """Extract ordered user/assistant text exchanges. Skips tool-call-only turns.
    Supports OpenClaw/CLI (type='message' with nested message.role) and
    Hermes (role='user'/'assistant' directly on event).
    Preserves 'label' field from content items (e.g. 'visual_reply' from TURN spans)."""
    sub_format = detect_jsonl_subformat(events)
    conv = []

    if sub_format == 'hermes':
        for e in events:
            role = e.get('role', '')
            if role not in ('user', 'assistant'):
                continue
            content = e.get('content', '')
            text = content if isinstance(content, str) else ''
            if not text and isinstance(content, list):
                text = extract_text_from_content(content)
            if not text:
                continue
            conv.append({
                'role': role,
                'text': text,
                'timestamp': e.get('timestamp', ''),
            })
        return sorted(conv, key=lambda x: parse_timestamp(x['timestamp']) if x['timestamp'] else 0)

    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content", [])
        text = extract_text_from_content(content)
        if not text:
            continue
        # Preserve explicit label if set on any content item (e.g. visual_reply)
        explicit_label = None
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("label"):
                    explicit_label = item["label"]
                    break
        entry = {
            "role": role,
            "text": text,
            "timestamp": e["timestamp"],
        }
        if explicit_label:
            entry["label"] = explicit_label
        conv.append(entry)
    return sorted(conv, key=lambda x: parse_timestamp(x["timestamp"]))


def normalize_tool_name(name):
    """
    Map tool names from Claude Code CLI format to internal exec format.
    Claude Code: Bash, Read, Write, Edit
    Internal: exec, read, write, edit
    Also handles trace format tool names.
    """
    mapping = {
        'Bash': 'exec',
        'Read': 'read',
        'Write': 'write',
        'Edit': 'edit',
        'WebSearch': 'web_search',
        'WebFetch': 'web_fetch',
        'env_bootstrap': 'exec',  # env_bootstrap is effectively exec
    }
    return mapping.get(name, name.lower())


def extract_tool_calls_openclaw(events):
    """
    Extract all tool calls from OpenClaw format.
    Handles process-chain for long-running commands:
      exec toolCall -> running toolResult -> [process toolCall -> running]* -> completed
    Returns: (commands, tool_usage, errors)
    """
    tool_results_by_call_id = {}

    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "toolResult":
            continue
        tool_call_id = msg.get("toolCallId", "")
        if tool_call_id not in tool_results_by_call_id:
            tool_results_by_call_id[tool_call_id] = []
        tool_results_by_call_id[tool_call_id].append(e)

    exec_calls = []
    tool_usage = {}

    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "assistant":
            continue
        for item in msg.get("content", []):
            if not isinstance(item, dict) or item.get("type") != "toolCall":
                continue
            tool_name = item.get("name", "")
            tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1
            if tool_name == "exec":
                exec_calls.append({
                    "tool_call_id": item["id"],
                    "command": item.get("arguments", {}).get("command", ""),
                    "call_ts": e["timestamp"],
                })

    commands = []
    errors = []

    for call in exec_calls:
        call_id = call["tool_call_id"]
        call_ts_ms = parse_timestamp(call["call_ts"])

        results = tool_results_by_call_id.get(call_id, [])
        # Follow process chain: find the last completed result
        completed_result = None
        for r in results:
            details = r.get("message", {}).get("details", {})
            if details.get("status") == "completed":
                completed_result = r
                break

        if completed_result is None and results:
            completed_result = results[-1]

        if completed_result is None:
            continue

        details = completed_result.get("message", {}).get("details", {})
        result_ts_ms = parse_timestamp(completed_result["timestamp"])
        exit_code = details.get("exitCode", 0)
        duration_ms = result_ts_ms - call_ts_ms
        status = "ok" if exit_code == 0 else "error"

        # Get output text (and error text if non-zero exit)
        output_text = ""
        error_text = ""
        content = completed_result.get("message", {}).get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                output_text = item["text"][:500]
                if exit_code != 0:
                    error_text = item["text"][:200]
                break

        entry = {
            "tool": "exec",
            "command": call["command"],
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "status": status,
            "timestamp": call["call_ts"],
            "output_text": output_text,
        }
        # IMPROVE: Improvement-4 — for process tool commands, add process_action field
        if entry.get('tool') == 'process':
            try:
                action = json.loads(entry.get('command', '{}')).get('action', '')
                entry['process_action'] = action
            except:
                entry['process_action'] = ''
        commands.append(entry)

        if exit_code != 0:
            errors.append({
                "command": call["command"],
                "exit_code": exit_code,
                "error_text": error_text,
                "timestamp": call["call_ts"],
            })

    return commands, tool_usage, errors


def extract_tool_calls_claude_code(events):
    """
    Extract tool calls from Claude Code CLI format.
    Claude Code embeds tool calls in message.content:
      type: "assistant" with message.content[].type == "tool_use"
      Tool results come from subsequent user messages or system messages
    Returns: (commands, tool_usage, errors)
    """
    tool_usage = {}
    tool_calls_by_id = {}
    commands = []
    errors = []

    # First pass: collect all tool calls from assistant messages
    for event in events:
        if event.get('type') != 'assistant':
            continue

        msg = event.get('message', {})
        content = msg.get('content', [])
        if not isinstance(content, list):
            continue

        # Look for tool_use items in content
        for item in content:
            if not isinstance(item, dict) or item.get('type') != 'tool_use':
                continue

            tool_call_id = item.get('id', '')
            tool_name = item.get('name', '')

            # Normalize tool name
            normalized_name = normalize_tool_name(tool_name)
            tool_usage[normalized_name] = tool_usage.get(normalized_name, 0) + 1

            # Extract command
            command = ''
            input_data = item.get('input', {})
            if isinstance(input_data, dict):
                command = input_data.get('command', '')
                if not command:
                    # For non-exec tools, try to get a string representation
                    command = json.dumps(input_data)[:200]
            elif isinstance(input_data, str):
                command = input_data

            call_ts = event.get('timestamp', '')
            if not call_ts:
                # Try to infer timestamp from parent event
                continue

            call_ts_ms = parse_timestamp(call_ts)

            tool_calls_by_id[tool_call_id] = {
                'tool': normalized_name,
                'command': command,
                'call_ts': call_ts,
                'call_ts_ms': call_ts_ms,
            }

    # Second pass: match tool results from user/system messages
    for i, event in enumerate(events):
        event_type = event.get('type')
        if event_type not in ('user', 'system'):
            continue

        msg = event.get('message', {})
        if not msg:
            continue

        # Check if this is a tool result (contains output from a previous tool call)
        content = msg.get('content', '')
        if not content:
            continue

        # Try to match this with preceding tool calls
        # Look backwards for the most recent tool call
        for j in range(i - 1, -1, -1):
            prev_event = events[j]
            if prev_event.get('type') != 'assistant':
                continue

            prev_msg = prev_event.get('message', {})
            prev_content = prev_msg.get('content', [])
            if not isinstance(prev_content, list):
                continue

            # Find the last tool_use in this assistant message
            for item in reversed(prev_content):
                if item.get('type') == 'tool_use':
                    tool_call_id = item.get('id', '')
                    if tool_call_id in tool_calls_by_id:
                        call_info = tool_calls_by_id[tool_call_id]

                        # Parse the content as potential exit code and output
                        result_ts = event.get('timestamp', call_info['call_ts'])
                        result_ts_ms = parse_timestamp(result_ts)

                        # Try to extract exit code from content
                        exit_code = 0
                        output_text = str(content)[:500] if isinstance(content, str) else ''
                        error_text = ''

                        # Look for exit code markers
                        if isinstance(content, str):
                            if 'exit status' in content.lower() or 'error' in content.lower():
                                exit_code = 1
                            if 'not found' in content.lower() or 'command not found' in content.lower():
                                exit_code = 127

                        duration_ms = result_ts_ms - call_info['call_ts_ms']
                        status = 'ok' if exit_code == 0 else 'error'

                        entry = {
                            'tool': call_info['tool'],
                            'command': call_info['command'],
                            'exit_code': exit_code,
                            'duration_ms': duration_ms,
                            'status': status,
                            'timestamp': call_info['call_ts'],
                            'output_text': output_text,
                        }
                        # IMPROVE: Improvement-4 — for process tool commands, add process_action field
                        if entry.get('tool') == 'process':
                            try:
                                action = json.loads(entry.get('command', '{}')).get('action', '')
                                entry['process_action'] = action
                            except:
                                entry['process_action'] = ''
                        commands.append(entry)

                        if exit_code != 0:
                            errors.append({
                                'command': call_info['command'],
                                'exit_code': exit_code,
                                'error_text': output_text[:200],
                                'timestamp': call_info['call_ts'],
                            })

                        # Remove from pending calls
                        del tool_calls_by_id[tool_call_id]
                    break
            break

    # Add remaining tool calls that didn't have results matched
    for tool_call_id, call_info in tool_calls_by_id.items():
        entry = {
            'tool': call_info['tool'],
            'command': call_info['command'],
            'exit_code': 0,
            'duration_ms': 0,
            'status': 'ok',
            'timestamp': call_info['call_ts'],
            'output_text': '',
        }
        # IMPROVE: Improvement-4 — for process tool commands, add process_action field
        if entry.get('tool') == 'process':
            try:
                action = json.loads(entry.get('command', '{}')).get('action', '')
                entry['process_action'] = action
            except:
                entry['process_action'] = ''
        commands.append(entry)

    return commands, tool_usage, errors


def extract_tool_calls_trace(events):
    """
    Extract tool calls from trace-converted events (standalone tool_use/tool_result).
    Used when events are created from Langfuse trace conversion.
    Returns: (commands, tool_usage, errors)
    """
    tool_usage = {}
    tool_results_by_id = {}
    tool_calls_by_id = {}
    commands = []
    errors = []

    # First pass: collect tool_result events by toolCallId
    for event in events:
        if event.get('type') == 'tool_result':
            call_id = event.get('toolCallId', '')
            if call_id:
                tool_results_by_id[call_id] = event

    # Second pass: collect tool_use events and match with results
    for event in events:
        if event.get('type') != 'tool_use':
            continue

        call_id = event.get('toolCallId', '')
        tool_name = event.get('toolName', '')

        if not tool_name:
            continue

        # Track tool usage
        tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1

        # Get tool call details
        input_data = event.get('input', {})
        call_ts = event.get('timestamp', '')

        if not call_ts:
            continue

        call_ts_ms = parse_timestamp(call_ts)

        # Build command string
        command = ''
        if tool_name == 'exec' and isinstance(input_data, dict):
            command = input_data.get('command', '')
        else:
            command = json.dumps(input_data)[:200] if input_data else ''

        # Match with tool_result
        result = tool_results_by_id.get(call_id)
        exit_code = 0
        output_text = ''
        duration_ms = 0

        if result:
            exit_code = result.get('exitCode', 0)
            if isinstance(exit_code, str):
                try:
                    exit_code = int(exit_code)
                except (json.JSONDecodeError, TypeError, ValueError):
                    exit_code = 0
            output_text = result.get('output', '')[:500]
            result_ts = result.get('timestamp', call_ts)
            if result_ts:
                result_ts_ms = parse_timestamp(result_ts)
                duration_ms = result_ts_ms - call_ts_ms

        status = 'ok' if exit_code == 0 else 'error'

        entry = {
            'tool': tool_name,
            'command': command,
            'exit_code': exit_code,
            'duration_ms': duration_ms,
            'status': status,
            'timestamp': call_ts,
            'output_text': output_text,
        }
        # IMPROVE: Improvement-4 — for process tool commands, add process_action field
        # extracted from the command JSON so downstream consumers can distinguish
        # poll/log (monitoring) from list/kill/create_pact/submit (execution).
        if tool_name == 'process':
            try:
                action = json.loads(entry.get('command', '{}')).get('action', '')
                entry['process_action'] = action
            except:
                entry['process_action'] = ''
        commands.append(entry)

        if exit_code != 0:
            errors.append({
                'command': command,
                'exit_code': exit_code,
                'error_text': output_text[:200] if output_text else '',
                'timestamp': call_ts,
            })

    # FIX: BUG-09 — third pass: collect standalone caw.* tool_result events
    # These are emitted by convert_trace_to_events with tool_name but no toolCallId.
    for event in events:
        if event.get('type') != 'tool_result':
            continue
        tool_name = event.get('tool_name', '')
        if not tool_name or not tool_name.startswith('caw.'):
            continue
        # Skip events that were already processed as paired tool_result (have toolCallId)
        if event.get('toolCallId'):
            continue

        call_ts = event.get('timestamp', '')
        if not call_ts:
            continue

        tool_usage[tool_name] = tool_usage.get(tool_name, 0) + 1

        exit_code = event.get('exit_code', 0)
        output_text = event.get('output', '')[:500]
        command = event.get('command', tool_name)
        duration_ms = event.get('duration_ms', 0)
        status = event.get('status', 'ok' if exit_code == 0 else 'error')

        entry = {
            'tool': event.get('tool', 'exec'),
            'tool_name': tool_name,
            'command': command,
            'exit_code': exit_code,
            'duration_ms': duration_ms,
            'status': status,
            'timestamp': call_ts,
            'output_text': output_text,
        }
        commands.append(entry)

        if exit_code != 0:
            errors.append({
                'command': command,
                'exit_code': exit_code,
                'error_text': output_text[:200] if output_text else '',
                'timestamp': call_ts,
            })

    return commands, tool_usage, errors


def extract_tool_calls_hermes(events):
    """
    Extract tool calls from Hermes format (OpenAI function-calling style).
    - assistant messages: tool_calls[].function.{name, arguments}
    - tool result messages: role="tool", tool_call_id, content (JSON string)
    - terminal results: content is JSON with {output, exit_code, error}
    Returns: (commands, tool_usage, errors)
    """
    result_map = {}  # tool_call_id → result event
    for event in events:
        if event.get('role') == 'tool':
            tcid = event.get('tool_call_id', '')
            if tcid:
                result_map[tcid] = event

    commands = []
    tool_usage = {}
    errors = []

    for event in events:
        if event.get('role') != 'assistant':
            continue
        call_ts = event.get('timestamp', '')
        for tc in event.get('tool_calls', []):
            fn = tc.get('function', {})
            tool_name_raw = fn.get('name', '')
            if not tool_name_raw:
                continue

            tool_usage[tool_name_raw] = tool_usage.get(tool_name_raw, 0) + 1
            tool_type = HERMES_TOOL_MAP.get(tool_name_raw, 'exec')

            try:
                args = json.loads(fn.get('arguments', '{}'))
            except (json.JSONDecodeError, TypeError):
                args = {}

            command_str = _hermes_command_str(tool_name_raw, args)
            tcid = tc.get('id', '')

            exit_code = 0
            error_text = ''
            result_ts = call_ts
            duration_ms = 0

            if tcid in result_map:
                result_event = result_map[tcid]
                result_ts = result_event.get('timestamp', call_ts)
                if call_ts and result_ts and call_ts != result_ts:
                    try:
                        duration_ms = parse_timestamp(result_ts) - parse_timestamp(call_ts)
                    except (ValueError, KeyError):
                        duration_ms = 0

                content = result_event.get('content', '')
                if content:
                    try:
                        result_data = json.loads(content) if isinstance(content, str) else content
                        if isinstance(result_data, dict):
                            exit_code = result_data.get('exit_code', 0)
                            err = result_data.get('error', '')
                            if err:
                                error_text = str(err)[:200]
                    except (json.JSONDecodeError, TypeError):
                        content_lower = str(content).lower()
                        if any(s in content_lower for s in ('error:', 'exception:', 'not found', 'permission denied')):
                            exit_code = 1

            cmd_entry = {
                'tool': tool_type,
                'tool_name': tool_name_raw,
                'command': command_str,
                'exit_code': exit_code,
                'duration_ms': duration_ms,
                'status': 'ok' if exit_code == 0 else 'error',
                'timestamp': call_ts,
                'output_text': '',
            }
            commands.append(cmd_entry)

            if exit_code != 0:
                errors.append({
                    'command': command_str,
                    'exit_code': exit_code,
                    'error_text': error_text,
                    'timestamp': call_ts,
                })

    return commands, tool_usage, errors


def _hermes_command_str(tool_name, args):
    """Build a human-readable command string from Hermes tool arguments."""
    if tool_name == 'terminal':
        return args.get('command', '')
    elif tool_name in ('read_file', 'search_files'):
        return args.get('path', args.get('query', ''))
    elif tool_name in ('write_file', 'patch'):
        return args.get('path', '')
    elif tool_name in ('skill_view', 'skill_manage', 'skills_list'):
        name = args.get('name', '')
        return f"{tool_name} {name}".strip()
    elif tool_name == 'browser_navigate':
        return args.get('url', '')
    elif tool_name == 'execute_code':
        lang = args.get('language', 'python')
        code = args.get('code', '')[:60]
        return f"execute_code ({lang}): {code}"
    elif tool_name == 'cronjob':
        return f"cronjob {args.get('action', '')} {args.get('name', '')}".strip()
    else:
        for v in args.values():
            if isinstance(v, str) and v:
                return f"{tool_name}: {v[:80]}"
        return tool_name


def extract_tool_calls(events):
    """
    Extract all tool calls with their results.
    Supports OpenClaw, Claude Code CLI, Hermes, and trace-converted formats.
    Returns: (commands, tool_usage, errors)
    """
    # Check for Hermes format first (role-based, no 'type' field)
    sub_format = detect_jsonl_subformat(events)
    if sub_format == 'hermes':
        return extract_tool_calls_hermes(events)

    # Single pass: check for format signals
    has_standalone_tool_events = False
    has_embedded_tool_events = False

    for e in events[:50]:
        event_type = e.get('type')
        if event_type in ('tool_use', 'tool_result'):
            has_standalone_tool_events = True
        elif event_type == 'message' and e.get('message', {}).get('role') == 'assistant':
            if any(item.get('type') == 'tool_use' for item in e.get('message', {}).get('content', [])):
                has_embedded_tool_events = True

        # Early exit if we've found what we need
        if has_standalone_tool_events and has_embedded_tool_events:
            break

    # If we have standalone tool events but not embedded, it's trace format
    if has_standalone_tool_events and not has_embedded_tool_events:
        return extract_tool_calls_trace(events)

    # Otherwise use format detection
    sub_format = detect_jsonl_subformat(events)

    if sub_format == 'claude-code-cli':
        return extract_tool_calls_claude_code(events)
    else:
        return extract_tool_calls_openclaw(events)


def calculate_timing(events, commands, total_ms):
    """
    LLM time: last toolResult in preceding batch -> assistant message.
    Preceding batch = all toolResults between previous assistant and current assistant, in document order.
    CLI time: sum of exec command durations.
    User time: preceding assistant -> user message.
    Idle: residual.
    """
    msg_events = [e for e in events if e.get("type") == "message"]

    llm_intervals = []
    user_intervals = []

    prev_assistant_ts = None
    last_tool_result_ts = None
    last_anchor_ts = None

    for e in msg_events:
        msg = e.get("message", {})
        role = msg.get("role")
        ts = parse_timestamp(e["timestamp"])

        if role == "toolResult":
            last_tool_result_ts = ts

        elif role == "assistant":
            start = last_tool_result_ts if last_tool_result_ts is not None else last_anchor_ts
            if start is None:
                # First assistant turn — credit from session start
                start = parse_timestamp(events[0]["timestamp"]) if events else None
            if start is not None:
                llm_intervals.append((start, ts))
            last_tool_result_ts = None
            prev_assistant_ts = ts
            last_anchor_ts = ts

        elif role == "user":
            if prev_assistant_ts is not None:
                user_intervals.append((prev_assistant_ts, ts))
            last_anchor_ts = ts
            last_tool_result_ts = None

    def total_ms_from(intervals):
        return sum(max(0, end - start) for start, end in intervals)

    def avg_ms_from(intervals):
        vals = [max(0, end - start) for start, end in intervals]
        return int(sum(vals) / len(vals)) if vals else 0

    def max_ms_from(intervals):
        vals = [max(0, end - start) for start, end in intervals]
        return max(vals) if vals else 0

    cli_durations = [c["duration_ms"] for c in commands]
    cli_ms = sum(cli_durations)
    cli_avg = int(sum(cli_durations) / len(cli_durations)) if cli_durations else 0
    cli_max = max(cli_durations) if cli_durations else 0

    llm_ms = total_ms_from(llm_intervals)
    user_ms = total_ms_from(user_intervals)
    idle_ms = max(0, total_ms - llm_ms - cli_ms - user_ms)

    def pct(val):
        return round(val * 100 / total_ms) if total_ms > 0 else 0

    return {
        "total_ms": total_ms,
        "llm_ms": llm_ms,
        "llm_pct": pct(llm_ms),
        "llm_avg_ms": avg_ms_from(llm_intervals),
        "llm_max_ms": max_ms_from(llm_intervals),
        "cli_ms": cli_ms,
        "cli_pct": pct(cli_ms),
        "cli_avg_ms": cli_avg,
        "cli_max_ms": cli_max,
        "user_ms": user_ms,
        "user_pct": pct(user_ms),
        "idle_ms": idle_ms,
        "idle_pct": pct(idle_ms),
    }


def strip_shell_prefix(cmd):
    """
    Strip leading shell setup statements (export VAR=value, env VAR=value)
    and return the first real executable command.

    Splits on ';', '&&', and newlines, skips segments that are pure export/env
    assignments, returns the first non-export/non-env segment.
    """
    segments = re.split(r';|&&|\n', cmd)
    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        # Skip comment lines
        if segment.startswith('#'):
            continue
        # Skip pure "export VAR=value" statements
        if re.match(r'^export\s+\w+=', segment):
            continue
        # Handle "env VAR=val cmd args" — strip leading VAR=val tokens.
        # Note: if a real command arg contains '=', it will also be stripped.
        # This is accepted as a rare edge case.
        if re.match(r'^env\s+\w+=', segment):
            tokens = segment.split()
            real_tokens = [t for t in tokens[1:] if '=' not in t]
            if real_tokens:
                return ' '.join(real_tokens)
            continue
        # Skip VAR=value inline assignments (e.g. "CALLDATA=$(caw ...)")
        if re.match(r'^[A-Z_][A-Z0-9_]*=', segment):
            # Extract the value part after '='
            eq_idx = segment.index('=')
            value = segment[eq_idx + 1:].strip()
            # If value starts with $( ... ), extract the inner command
            if value.startswith('$('):
                inner = value[2:]
                if inner.endswith(')'):
                    inner = inner[:-1]
                return inner.strip() if inner.strip() else segment
            continue
        # First non-prefix segment is the real command
        return segment
    return ''  # all segments were prefixes/comments — no real command


def normalize_command(cmd):
    """
    Normalize command for loop detection.
    Strip shell setup prefixes (export/env/VAR=), comments, then strip
    'npx'/'npx -y', flags (tokens starting with '-'), and flag value tokens.
    Return first two remaining tokens.
    """
    cmd = strip_shell_prefix(cmd.strip())

    # Skip comment-only lines
    if cmd.lstrip().startswith('#'):
        return ''

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    # Strip 'npx' and optional following '-y'
    if tokens and tokens[0] == "npx":
        tokens.pop(0)
        if tokens and tokens[0] == "-y":
            tokens.pop(0)

    # Skip flags and their value tokens (v0.3 fix)
    filtered = []
    skip_next = False
    for t in tokens:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            skip_next = True
            continue
        filtered.append(t)

    return " ".join(filtered[:2]) if len(filtered) >= 2 else " ".join(filtered)


def detect_loops(commands, window=10, threshold=3):
    """
    Detect loops: same normalized command >= threshold times in any window-sized slice.
    Classification uses toolResult output_text:
    - polling_loop: exit_code==0 AND output contains "pending"/"waiting"/"running"/"in progress"
    - error_loop: at least one exit_code != 0
    - exploration_loop: all exit_code==0 and no polling keywords found (NEW: BUG-01)

    FIX: BUG-02 — write/edit/read tool calls are excluded from loop detection.
    They are tracked separately as write_iterations.
    """
    if not commands:
        return [], []  # loops, write_iterations

    # FIX: BUG-02 — exclude write/edit/read tools from loop detection candidates
    LOOP_EXCLUDED_TOOLS = {"write", "edit", "read"}
    loop_commands = [(i, c) for i, c in enumerate(commands) if c.get("tool", "") not in LOOP_EXCLUDED_TOOLS]

    # Build normalized list aligned to loop_commands indices
    normalized_lc = [normalize_command(c["command"]) for _, c in loop_commands]

    loops = []
    reported = set()
    polling_keywords = ("pending", "waiting", "running", "in progress")

    for li in range(len(normalized_lc)):
        if normalized_lc[li] in reported:
            continue
        window_slice = normalized_lc[li:li + window]
        count = window_slice.count(normalized_lc[li])
        if count < threshold:
            continue

        matching_li = [lj for lj, n in enumerate(normalized_lc) if n == normalized_lc[li]]
        group_li = [lj for lj in matching_li if li <= lj < li + window]

        start_cmd = loop_commands[group_li[0]][1]
        end_cmd = loop_commands[group_li[-1]][1]

        all_zero_exit = all(loop_commands[lj][1]["exit_code"] == 0 for lj in group_li)

        loop_type = "error_loop"
        if all_zero_exit:
            found_polling = False
            for lj in group_li:
                output_text = loop_commands[lj][1].get("output_text", "").lower()
                if any(kw in output_text for kw in polling_keywords):
                    loop_type = "polling_loop"
                    found_polling = True
                    break
            # FIX: BUG-01 — all-zero exit with no polling keywords → exploration_loop
            if not found_polling:
                loop_type = "exploration_loop"

        # IMPROVE: Improvement-3 — refine error_loop into debugging_loop when edit/write commands
        # are interspersed between repeated commands in the window.
        # debugging_loop: agent is actively modifying code/config between retries (expected behavior).
        # error_loop: agent retries without any modification (blind retry — worse behavior).
        if loop_type == "error_loop":
            # Get original indices of all commands in the window (group_li are loop_commands indices)
            orig_start = loop_commands[group_li[0]][0]
            orig_end = loop_commands[group_li[-1]][0]
            # Check if any write/edit tool calls exist between first and last repeated command
            has_edit_between = any(
                commands[oi].get("tool", "") in ("write", "edit")
                for oi in range(orig_start, orig_end + 1)
            )
            if has_edit_between:
                loop_type = "debugging_loop"

        # IMPROVE: Improvement-4 — for process tool loops, add process_action to example_command context
        # Also ensure process:poll/log loops remain polling_loop (they won't have polling keywords
        # in output_text, but their action field makes intent clear).
        loop_entry = {
            "command_normalized": normalized_lc[li],
            "example_command": start_cmd["command"],
            "loop_type": loop_type,
            "count": len(group_li),
            "start_time": start_cmd["timestamp"],
            "end_time": end_cmd["timestamp"],
            "duration_ms": (
                parse_timestamp(end_cmd["timestamp"]) -
                parse_timestamp(start_cmd["timestamp"])
            ),
        }
        if start_cmd.get("tool") == "process":
            # Extract process_action from the start command (already populated by extractor)
            pa = start_cmd.get("process_action", "")
            if not pa:
                try:
                    pa = json.loads(start_cmd.get("command", "{}" )).get("action", "")
                except:
                    pa = ""
            loop_entry["process_action"] = pa
            # Ensure process:poll/log loops are classified as polling_loop
            if pa in ("poll", "log") and loop_type not in ("polling_loop",):
                loop_entry["loop_type"] = "polling_loop"
        loops.append(loop_entry)
        reported.add(normalized_lc[li])

    # FIX: BUG-02 — detect write_iterations (same path written >= 3 times)
    # Use regex instead of json.loads because command strings may be truncated
    from collections import Counter
    import re as _re
    path_counts = Counter()
    for cmd in commands:
        if cmd.get("tool", "") in ("write", "edit"):
            m = _re.search(r'"path"\s*:\s*"([^"]+)"', cmd.get("command", ""))
            if m:
                path_counts[m.group(1)] += 1

    write_iterations = [
        {"path": path, "count": count, "note": "iterative development"}
        for path, count in path_counts.items()
        if count >= 3
    ]

    return loops, write_iterations


def is_meaningful_command(cmd):
    """
    Return True if command counts as a meaningful action for efficiency/waste calculations.
    Excludes:
    - 'read' tool calls (read-only file access)
    - 'process' tool calls where action is 'poll' or 'log' (background process monitoring)
    """
    tool = cmd.get("tool", "")
    # FIX: BUG-07 — exclude process:poll and process:log from meaningful command counts
    if tool == "read":
        return False
    if tool == "process":
        try:
            action = json.loads(cmd.get("command", "{}")).get("action")
            if action in ("poll", "log"):
                return False
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return True


def detect_wasted_calls(commands):
    """
    Scan commands[] for unnecessary calls.
    Returns dict with total, by_type counts, waste_ratio, and details[].

    Wasted call types:
    - blind_retry: same normalised command fails consecutively (same raw command)
    - help_exploration: --help or help subcommand
    - flag_trial_error: same normalised command, different raw command (flag changes), all fail
    - env_probing: which <tool> or find -name <tool>

    NOT wasted: --version, polling_loop, exploration_loop, successful duplicates, read-only.
    """
    # read-only commands to skip — note: 'which' and 'find' are NOT here
    # because they are checked as env_probing (type 4) instead
    read_only = {'ls', 'cat', 'head', 'tail', 'echo', 'pwd', 'grep', 'rg'}

    normalized = [normalize_command(c.get("command", "")) for c in commands]
    wasted = []

    i = 0
    while i < len(commands):
        cmd = commands[i]
        norm = normalized[i]
        raw = cmd.get("command", "")
        exit_code = cmd.get("exit_code", 0)
        base_cmd = norm.split()[0] if norm else ""

        # Skip empty / read-only
        if not norm or base_cmd in read_only:
            i += 1
            continue

        # === Type 1: blind_retry ===
        # Same normalised AND same raw command, consecutive failures
        if exit_code != 0:
            j = i + 1
            while (j < len(commands)
                   and normalized[j] == norm
                   and commands[j].get("command", "") == raw  # same raw = truly identical
                   and commands[j].get("exit_code", 0) != 0):
                j += 1
            if j - i >= 2:
                for k in range(i + 1, j):
                    wasted.append({
                        "index": k,
                        "type": "blind_retry",
                        "command": commands[k]["command"][:80],
                        "reason": f"identical command failed {j - i}x in a row"
                    })
                i = j
                continue

        # === Type 2: help_exploration ===
        if "--help" in raw or raw.rstrip().endswith(" help"):
            wasted.append({
                "index": i,
                "type": "help_exploration",
                "command": raw[:80],
                "reason": "agent exploring CLI usage"
            })
            i += 1
            continue

        # === Type 2b: version_check ===
        if "--version" in raw or norm.endswith(" version") or norm == "version":
            wasted.append({
                "index": i,
                "type": "version_check",
                "command": raw[:80],
                "reason": "agent probing tool version"
            })
            i += 1
            continue

        # === Type 3: flag_trial_error ===
        # Same normalised, different raw (flag variations), consecutive failures
        if exit_code != 0:
            j = i + 1
            while (j < len(commands)
                   and normalized[j] == norm
                   and commands[j].get("command", "") != raw
                   and commands[j].get("exit_code", 0) != 0):
                j += 1
            if j - i >= 2:
                for k in range(i + 1, j):
                    wasted.append({
                        "index": k,
                        "type": "flag_trial_error",
                        "command": commands[k]["command"][:80],
                        "reason": f"same operation, different flags, {j - i} consecutive failures"
                    })
                i = j
                continue

        # === Type 4: env_probing ===
        if base_cmd == "which" or (base_cmd == "find" and "-name" in raw):
            wasted.append({
                "index": i,
                "type": "env_probing",
                "command": raw[:80],
                "reason": "agent searching for tool location"
            })
            i += 1
            continue

        i += 1

    # Compute summary
    # FIX: BUG-07 — use is_meaningful_command to exclude process:poll/log
    meaningful_count = sum(
        1 for i, c in enumerate(commands)
        if normalized[i] and normalized[i].split()[0] not in read_only
        and is_meaningful_command(c)
    )
    by_type = {}
    for w in wasted:
        by_type[w["type"]] = by_type.get(w["type"], 0) + 1

    return {
        "total": len(wasted),
        "by_type": by_type,
        "waste_ratio": round(len(wasted) / meaningful_count, 3) if meaningful_count > 0 else 0,
        "details": wasted,
    }


# FIX: BUG-04 — terminal error patterns that warrant correctly_abandoned classification
TERMINAL_ERROR_PATTERNS = [
    r"404 Not Found.*registry\.npmjs\.org",
    r"npm error 404.*is not in this registry",
    r"error: externally-managed-environment",
    r"Permission denied.*(/usr|/opt|/etc)",
    r"Read-only file system",
]


def is_terminal_error(error_text):
    """Return True if error_text matches a known terminal/unrecoverable error pattern."""
    return any(re.search(p, error_text, re.IGNORECASE) for p in TERMINAL_ERROR_PATTERNS)


def detect_recovery_quality(commands, errors=None, max_attempts=2):
    """
    For each failed command, check if the same operation succeeds within
    max_attempts subsequent tries. Measures outcome, not just "did something different."

    Resolution rules:
    - Same normalised operation succeeds within max_attempts → resolved
    - Same normalised operation does NOT succeed within max_attempts → unresolved
    - Permission errors (403/forbidden) where agent stops trying → correctly_abandoned
      (excluded from both numerator and denominator)
    - Terminal errors (npm 404, pip externally-managed) + different approach → correctly_abandoned
    - REQUIRE_APPROVAL → excluded (not an error)
    - Read-only command failures → excluded
    - Last error with no subsequent commands → excluded

    recovery_rate = resolved / (resolved + unresolved)
    """
    if errors is None:
        errors = []
    read_only = {'ls', 'cat', 'head', 'tail', 'echo', 'pwd', 'grep', 'rg'}
    approval_kw = ['require_approval', 'pending_approval', 'approval_required']
    permission_kw = ['403', 'permission', 'forbidden']

    normalized = [normalize_command(c.get("command", "")) for c in commands]
    details = []
    # Track which error indices we've already evaluated (to avoid double-counting
    # when the same operation fails multiple times)
    evaluated_ops = set()

    for i, cmd in enumerate(commands):
        exit_code = cmd.get("exit_code", 0)
        if exit_code == 0:
            continue

        norm = normalized[i]
        raw = cmd.get("command", "")
        base = norm.split()[0] if norm else ""
        error_text = cmd.get("output_text", "").lower()

        # Skip empty / read-only failures
        if not norm or base in read_only:
            continue

        # Skip approval (not an error)
        if any(kw in error_text for kw in approval_kw):
            continue

        # Skip if we already evaluated this normalised operation from an earlier failure
        if norm in evaluated_ops:
            continue
        evaluated_ops.add(norm)

        # Check for permission error — see if agent correctly stopped
        search_text = error_text + " " + raw.lower()
        is_permission = any(kw in search_text for kw in permission_kw)

        # Look forward: count subsequent attempts at the same normalised operation
        attempts_after = []
        for j in range(i + 1, len(commands)):
            if normalized[j] == norm:
                attempts_after.append(j)

        if is_permission:
            if not attempts_after:
                # Agent stopped after permission error → correctly abandoned
                details.append({
                    "error_index": i,
                    "operation": norm,
                    "error_command": raw[:80],
                    "outcome": "correctly_abandoned",
                    "attempts": 0,
                    "reason": "permission error — agent correctly stopped",
                })
            else:
                # Agent retried after permission error → bad
                details.append({
                    "error_index": i,
                    "operation": norm,
                    "error_command": raw[:80],
                    "outcome": "unresolved",
                    "attempts": len(attempts_after),
                    "reason": f"permission error — agent retried {len(attempts_after)}x (should have stopped)",
                })
            continue

        # Non-permission error: did the same operation succeed within max_attempts?
        resolved = False
        attempts_used = 0
        for idx, j in enumerate(attempts_after):
            if idx >= max_attempts:
                break
            attempts_used = idx + 1
            if commands[j].get("exit_code", 0) == 0:
                resolved = True
                break

        if not attempts_after:
            # Operation never attempted again — check for terminal error first
            # FIX: BUG-04 — terminal errors with alternative approach → correctly_abandoned
            outcome = "unresolved"
            reason = "operation never retried"
            error_output = cmd.get("output_text", "") or error_text
            if is_terminal_error(error_output):
                # Check if subsequent commands take a different approach (different base command)
                subsequent_cmds = commands[i + 1:]
                has_different_approach = any(
                    normalize_command(c.get("command", "")).split()[0:1] != [base]
                    and c.get("tool", "") not in ("read",)
                    for c in subsequent_cmds
                ) if subsequent_cmds else False
                if has_different_approach:
                    outcome = "correctly_abandoned"
                    reason = "terminal error — alternative approach taken"
            details.append({
                "error_index": i,
                "operation": norm,
                "error_command": raw[:80],
                "outcome": outcome,
                "attempts": 0,
                "reason": reason,
            })
        elif resolved:
            details.append({
                "error_index": i,
                "operation": norm,
                "error_command": raw[:80],
                "outcome": "resolved",
                "attempts": attempts_used,
                "reason": f"resolved in {attempts_used} attempt{'s' if attempts_used > 1 else ''}",
            })
        else:
            total_after = len(attempts_after)
            # Check if it eventually succeeded (beyond window)
            eventually = any(commands[j].get("exit_code", 0) == 0 for j in attempts_after)
            if eventually:
                # FIX: BUG-05 — check for write/edit between first failure and eventual success
                success_idx = next(j for j in attempts_after if commands[j].get("exit_code", 0) == 0)
                intermediate_cmds = commands[i:success_idx]
                write_edits = [c for c in intermediate_cmds if c.get("tool", "") in ("write", "edit")]
                if write_edits:
                    details.append({
                        "error_index": i,
                        "operation": norm,
                        "error_command": raw[:80],
                        "outcome": "resolved",
                        "attempts": total_after,
                        "reason": f"resolved (iterative fix, {total_after} attempts, {len(write_edits)} edits)",
                    })
                else:
                    details.append({
                        "error_index": i,
                        "operation": norm,
                        "error_command": raw[:80],
                        "outcome": "unresolved",
                        "attempts": min(total_after, max_attempts),
                        "reason": f"succeeded on attempt {total_after} — brute-forced",
                    })
            else:
                details.append({
                    "error_index": i,
                    "operation": norm,
                    "error_command": raw[:80],
                    "outcome": "unresolved",
                    "attempts": min(total_after, max_attempts),
                    "reason": f"not resolved within {max_attempts} attempts",
                })

    resolved_count = sum(1 for d in details if d["outcome"] == "resolved")
    unresolved_count = sum(1 for d in details if d["outcome"] == "unresolved")
    abandoned_count = sum(1 for d in details if d["outcome"] == "correctly_abandoned")
    denominator = resolved_count + unresolved_count

    # FIX: BUG-03 — handle evaluated=0 case with explicit note instead of defaulting to 1.0
    total_evaluated = len(details)
    if denominator == 0:
        if total_evaluated == 0 and len(errors) == 0:
            # No failures occurred at all
            recovery_rate = 1.0
            recovery_rate_note = "no_failures"
        elif total_evaluated == 0:
            # Errors exist but none were categorized as evaluated (e.g. all abandoned/skipped)
            recovery_rate = None
            recovery_rate_note = "not_evaluated"
        else:
            recovery_rate = 1.0
            recovery_rate_note = "no_failures"
    else:
        recovery_rate = round(resolved_count / denominator, 3)
        recovery_rate_note = "evaluated"

    return {
        "resolved": resolved_count,
        "unresolved": unresolved_count,
        "correctly_abandoned": abandoned_count,
        "total_evaluated": total_evaluated,
        "recovery_rate": recovery_rate,
        "recovery_rate_note": recovery_rate_note,
        "details": details,
    }


def detect_hallucinations(conversation, commands):
    """
    Cross-check assistant completion claims against actual command results.

    Step 1: Find completion claims — requires BOTH an action verb AND a success
    indicator. Bare ✅ without a verb (e.g., "✅ New session started", table content)
    does not count as a claim.

    Step 2: For each claim, check the most recent meaningful commands.
    Flag as hallucination if last command failed or 60%+ of recent window failed.

    Note: conversation[].timestamp and commands[].timestamp come from the same
    JSONL source (both ISO format), so timezone alignment is guaranteed.
    """
    # Success indicators — must appear together with an action verb to be a claim.
    # "成功" (Chinese for "success") is a success indicator, not an action verb.
    success_indicators = ['✅', '✓', 'successfully', 'success', '成功']

    # Action verbs — pure verbs only. Do NOT include "成功" here (it's a success
    # indicator); otherwise any message with "成功" would self-trigger as a claim.
    action_verbs_en = ['completed', 'transferred', 'submitted', 'created',
                       'deployed', 'executed', 'sent', 'approved', 'confirmed']
    action_verbs_zh = ['完成', '提交', '创建', '转账', '部署',
                       '执行', '发送', '批准', '确认']

    # Standalone completion phrases (don't need a separate success indicator)
    standalone_phrases = [
        'transaction successful', 'operation complete', 'transfer complete',
        '交易成功', '转账成功', '操作成功', '操作完成',
    ]

    read_only = {'ls', 'cat', 'head', 'tail', 'echo', 'pwd', 'grep', 'rg'}

    timed_cmds = [c for c in commands if c.get("timestamp")]

    # Step 1: find claims — require success indicator + action verb
    claims = []
    for msg in conversation:
        if msg.get("role") != "assistant":
            continue
        text = msg.get("text", "")
        ts = msg.get("timestamp")
        if not ts or not text:
            continue

        head = text[:300].lower()

        # Check standalone phrases first (self-sufficient)
        is_claim = any(phrase in head for phrase in standalone_phrases)

        if not is_claim:
            # Check success indicator + action verb combination
            has_success = any(sig.lower() in head for sig in success_indicators)
            if has_success:
                has_action = (any(v in head for v in action_verbs_en)
                              or any(v in head for v in action_verbs_zh))
                is_claim = has_action

        if is_claim:
            claims.append({"timestamp": ts, "text": text[:120]})

    # Step 2: cross-check
    details = []
    for claim in claims:
        # FIX: BUG-13 — limit hallucination check to commands within 120s of claim
        claim_ts_ms = parse_timestamp(claim["timestamp"])
        recent = [c for c in timed_cmds
                  if c["timestamp"] <= claim["timestamp"]
                  and claim_ts_ms - parse_timestamp(c["timestamp"]) <= 120000  # 120 seconds
                  and (normalize_command(c.get("command", "")).split() or [""])[0] not in read_only]
        recent = recent[-5:]

        if not recent:
            continue

        last_cmd = recent[-1]
        fail_count = sum(1 for c in recent if c.get("exit_code", 0) != 0)

        is_hallucination = False
        reason = ""

        if last_cmd.get("exit_code", 0) != 0:
            is_hallucination = True
            reason = f"last command failed (exit {last_cmd['exit_code']}): {last_cmd.get('command','')[:60]}"
        elif fail_count >= 3:
            is_hallucination = True
            reason = f"{fail_count}/{len(recent)} recent commands failed"

        if is_hallucination:
            details.append({
                "claim_text": claim["text"][:100],
                "claim_timestamp": claim["timestamp"],
                "last_command": last_cmd.get("command", "")[:80],
                "last_exit_code": last_cmd.get("exit_code", 0),
                "recent_fail_count": fail_count,
                "recent_total": len(recent),
                "reason": reason,
            })

    total_claims = len(claims)
    hallucination_count = len(details)

    return {
        "total_claims": total_claims,
        "hallucinations": hallucination_count,
        "hallucination_rate": round(hallucination_count / total_claims, 3) if total_claims > 0 else 0,
        "details": details,
    }


def compute_goal_drift_warning(total_claims, recovery, wasted):
    """
    FIX: BUG-06 — add goal_drift_warning when task may be partial but agent claimed completion.
    Triggers when total_claims > 0 AND (unresolved > 0 OR waste_ratio > 0.20).
    """
    if total_claims <= 0:
        return None
    unresolved = recovery.get("unresolved", 0)
    waste_ratio = wasted.get("waste_ratio", 0)
    if unresolved > 0 or waste_ratio > 0.20:
        return "task may be partial — verify claim matches original goal"
    return None


def strip_internal_fields(commands):
    """Remove fields used only internally (output_text not part of output schema)."""
    return [{k: v for k, v in c.items() if k != "output_text"} for c in commands]


def calculate_stats(events, commands, errors):
    """Aggregate message counts and token/cost totals.
    Supports OpenClaw/CLI (type='message' with nested role) and Hermes (role at top level)."""
    user_messages = 0
    assistant_messages = 0
    total_tokens = 0
    total_cost = 0.0

    for e in events:
        # Hermes format: role directly on event
        if e.get('role') in ('user', 'assistant') and not e.get('type'):
            if e['role'] == 'user':
                user_messages += 1
            else:
                assistant_messages += 1
            continue

        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        role = msg.get("role")
        if role == "user":
            user_messages += 1
        elif role == "assistant":
            assistant_messages += 1
            usage = msg.get("usage", {})
            total_tokens += usage.get("totalTokens", 0)
            cost = usage.get("cost", {})
            total_cost += cost.get("total", 0.0)

    return {
        "user_messages": user_messages,
        "assistant_messages": assistant_messages,
        "total_turns": user_messages + assistant_messages,
        "tool_calls": len(commands),
        "tool_errors": len(errors),
        "total_tokens": total_tokens,
        "total_cost_usd": round(total_cost, 6),
    }


def extract_message_costs(events):
    """Per-assistant-message cost with timestamp, for per-task cost rollup."""
    result = []
    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "assistant":
            continue
        cost = msg.get("usage", {}).get("cost", {}).get("total", 0.0)
        if cost and cost > 0:
            result.append({
                "timestamp": e["timestamp"],
                "cost_usd": cost,
            })
    return result


def extract_thinking(events):
    """Thinking blocks per assistant turn, capped at 1000 chars each."""
    result = []
    for e in events:
        if e.get("type") != "message":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "assistant":
            continue
        for item in msg.get("content", []):
            if isinstance(item, dict) and item.get("type") == "thinking":
                result.append({
                    "timestamp": e["timestamp"],
                    "text": item.get("thinking", "")[:1000],
                })
    return result


def parse_time_arg(arg, session_start_ms):
    """
    Parse --since / --until arg to epoch milliseconds.
    Accepts HH:MM, HH:MM:SS, or full ISO 8601 timestamp.
    HH:MM is resolved relative to session start date in UTC.
    """
    arg = arg.strip()
    if "T" in arg:
        if not arg.endswith("Z") and "+" not in arg:
            arg += "Z"
        return parse_timestamp(arg)
    parts = arg.split(":")
    h, m = int(parts[0]), int(parts[1])
    s = int(parts[2]) if len(parts) > 2 else 0
    session_dt = datetime.fromtimestamp(session_start_ms / 1000, tz=timezone.utc)
    result_dt = session_dt.replace(hour=h, minute=m, second=s, microsecond=0)
    return int(result_dt.timestamp() * 1000)


def resolve_time_range(since_arg, until_arg, session_start_ms):
    """Resolve --since / --until args to (since_ts, until_ts) in epoch ms."""
    since_ts = parse_time_arg(since_arg, session_start_ms) if since_arg else None
    until_ts = parse_time_arg(until_arg, session_start_ms) if until_arg else None
    if since_ts and until_ts and until_ts < since_ts:
        until_ts += 86_400_000  # midnight crossing: add 24h
    return since_ts, until_ts


def apply_time_filter(events, since_ts=None, until_ts=None):
    """Filter events to those within [since_ts, until_ts]."""
    return [
        e for e in events
        if (since_ts is None or parse_timestamp(e["timestamp"]) >= since_ts)
        and (until_ts is None or parse_timestamp(e["timestamp"]) <= until_ts)
    ]


def analyze(path, since_arg=None, until_arg=None):
    """Single-file mode: run Phase 1 and print JSON to stdout."""
    try:
        output = analyze_to_dict(path, since_arg=since_arg, until_arg=until_arg)
    except ValueError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(output, indent=2))


def analyze_to_dict(path, since_arg=None, until_arg=None):
    """Run Phase 1 analysis and return result as dict (does not print)."""
    events = load_events(path)
    if not events:
        raise ValueError("Empty session file")

    if since_arg or until_arg:
        session_start_ms = parse_timestamp(events[0]["timestamp"])
        since_ts, until_ts = resolve_time_range(since_arg, until_arg, session_start_ms)
        events = apply_time_filter(events, since_ts, until_ts)
        if not events:
            raise ValueError("No events in specified time range")

    session = extract_session_meta(events)
    commands, tool_usage, errors = extract_tool_calls(events)
    timing = calculate_timing(events, commands, session["duration_ms"])
    loops, write_iterations = detect_loops(commands)
    wasted = detect_wasted_calls(commands)
    recovery = detect_recovery_quality(commands, errors=errors)
    conversation = extract_conversation(events)
    hallucinations = detect_hallucinations(conversation, commands)
    hallucinations["goal_drift_warning"] = compute_goal_drift_warning(
        hallucinations["total_claims"], recovery, wasted
    )
    stats = calculate_stats(events, commands, errors)
    message_costs = extract_message_costs(events)
    thinking = extract_thinking(events)
    stripped_commands = strip_internal_fields(commands)
    timeline = build_timeline(conversation, stripped_commands, thinking=thinking)

    return {
        "session": session,
        "stats": stats,
        "timing": timing,
        "loops": loops,
        "write_iterations": write_iterations,
        "wasted_calls": wasted,
        "recovery": recovery,
        "hallucinations": hallucinations,
        "errors": errors,
        "tool_usage": tool_usage,
        "timeline": timeline,
        # conversation[], commands[], thinking[], message_costs[] removed from output.
        # timeline[] is the single source: filter by kind/label for each use case.
        #   - Phase 2 transcript: kind=="thought", role in (user, assistant)
        #   - Phase 4 window:     any kind, filtered by timestamp
        #   - Phase 7 command log: kind=="command"
    }


def _extract_metrics_row(filename, data):
    """Extract a flat metrics dict for batch CSV output."""
    import csv as _csv
    tm = data.get("timing", {})
    st = data.get("stats", {})
    rec = data.get("recovery", {})
    hall = data.get("hallucinations", {})
    loops = data.get("loops", [])
    wasted = data.get("wasted_calls", {})
    wi = data.get("write_iterations", [])
    # commands[] removed from output — read from timeline[] instead
    timeline = data.get("timeline", [])
    cmds = [e for e in timeline if e.get("kind") == "command"]

    loop_counts = {}
    for l in loops:
        lt = l.get("loop_type", "unknown")
        loop_counts[lt] = loop_counts.get(lt, 0) + 1

    meaningful = [c for c in cmds if is_meaningful_command(c)]
    exit0 = sum(1 for c in meaningful if c.get("exit_code") == 0)
    efficiency = round(exit0 / len(meaningful) * 100) if meaningful else None
    caw_count = sum(1 for c in cmds if c.get("tool", "").startswith("caw.") or c.get("tool_name", "").startswith("caw."))

    return {
        "filename": filename,
        "duration_min": round(tm.get("total_ms", 0) / 60000, 1),
        "turns": st.get("total_turns", 0),
        "tool_calls": st.get("tool_calls", 0),
        "caw_commands": caw_count,
        "efficiency_pct": efficiency,
        "recovery_rate": rec.get("recovery_rate"),
        "recovery_note": rec.get("recovery_rate_note", ""),
        "hallucinations": hall.get("hallucinations", 0),
        "total_claims": hall.get("total_claims", 0),
        "goal_drift_warning": 1 if hall.get("goal_drift_warning") else 0,
        "error_loops": loop_counts.get("error_loop", 0),
        "exploration_loops": loop_counts.get("exploration_loop", 0),
        "debugging_loops": loop_counts.get("debugging_loop", 0),
        "polling_loops": loop_counts.get("polling_loop", 0),
        "write_iterations_count": len(wi),
        "waste_ratio": wasted.get("waste_ratio", 0),
    }


def run_batch(dir_path, pattern="*.json", skip_existing=False):
    """Batch Phase 1: process all matching files in dir, write _parser_output.json + batch_metrics.csv."""
    import csv as _csv

    files = sorted(Path(dir_path).glob(pattern))
    files = [f for f in files if not f.name.endswith("_parser_output.json")
             and not f.name.endswith("_report.md")]

    rows = []
    ok = failed = skipped = 0

    for f in files:
        out_path = f.with_name(f.stem + "_parser_output.json")

        if skip_existing and out_path.exists():
            try:
                data = json.loads(out_path.read_text())
                print(f"  skip  {f.name}", file=sys.stderr)
                skipped += 1
            except Exception as e:
                print(f"  ERROR reading {out_path.name}: {e}", file=sys.stderr)
                failed += 1
                continue
        else:
            try:
                data = analyze_to_dict(str(f))
                out_path.write_text(json.dumps(data, indent=2))
                print(f"  ✓     {f.name}", file=sys.stderr)
                ok += 1
            except Exception as e:
                print(f"  FAIL  {f.name}: {e}", file=sys.stderr)
                failed += 1
                continue

        rows.append(_extract_metrics_row(f.name, data))

    if rows:
        csv_path = Path(dir_path) / "batch_metrics.csv"
        with open(csv_path, "w", newline="") as fh:
            writer = _csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n{ok} ok, {skipped} skipped, {failed} failed → {csv_path}", file=sys.stderr)
    else:
        print(f"\n{ok} ok, {skipped} skipped, {failed} failed (no output)", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Analyze session files (single or batch).",
        usage="%(prog)s <path> [opts]  |  %(prog)s --dir <dir> [opts]"
    )
    parser.add_argument("path", nargs="?", help="Path to single session file")
    parser.add_argument("--dir", help="Directory for batch Phase 1 processing")
    parser.add_argument("--pattern", default="*.json",
                        help="Glob pattern for --dir mode (default: *.json)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip files that already have _parser_output.json")
    parser.add_argument("--since", help="Start time filter: HH:MM or ISO timestamp")
    parser.add_argument("--until", help="End time filter: HH:MM or ISO timestamp")
    args = parser.parse_args()

    # Validate: exactly one of path or --dir required
    if args.path and args.dir:
        parser.error("specify either path or --dir, not both")
    if not args.path and not args.dir:
        parser.error("specify either path or --dir")

    if args.dir:
        run_batch(args.dir, pattern=args.pattern, skip_existing=args.skip_existing)
    else:
        analyze(args.path, since_arg=args.since, until_arg=args.until)
