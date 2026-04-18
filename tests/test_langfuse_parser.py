#!/usr/bin/env python3
"""
Regression tests for Langfuse trace parsing.
Tests BUG-08 (GENERATION span conversation) and BUG-09 (caw.* span commands).

Usage: python3 tests/test_langfuse_parser.py <trace_dir>
  trace_dir: directory containing eval-apr-15 trace files
"""
import sys, json, subprocess, os

PARSER = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'analyze_session.py')

def run_parser(trace_file):
    result = subprocess.run(['python3', PARSER, trace_file], capture_output=True)
    assert result.returncode == 0, f"Parser failed: {result.stderr.decode()[:200]}"
    return json.loads(result.stdout)

def test_bug08_generation_spans(trace_dir):
    """BUG-08: conversation[] should include all GENERATION span texts, not just turn outputs."""
    # 08ffe389 has 51 GENERATION spans → expect 50+ assistant messages
    f = os.path.join(trace_dir, 'trace-08ffe389-7173-4845-8cc3-54a736d29689.json')
    if not os.path.exists(f): return "SKIP (file not found)"
    d = run_parser(f)
    conv = d.get('conversation', [])
    asst = [m for m in conv if m.get('role') == 'assistant']
    assert len(asst) >= 40, f"Expected 40+ assistant messages, got {len(asst)}"
    return f"PASS: {len(asst)} assistant messages"

def test_bug09_caw_spans(trace_dir):
    """BUG-09: caw.* spans should appear in commands[]."""
    # 161b5764 has caw.tx.call spans that were previously missing
    f = os.path.join(trace_dir, 'trace-161b5764-09d3-4ad2-b1a2-dd3c45ce94be.json')
    if not os.path.exists(f): return "SKIP (file not found)"
    d = run_parser(f)
    cmds = d.get('commands', [])
    caw_cmds = [c for c in cmds if c.get('tool_name', '').startswith('caw.')]
    assert len(caw_cmds) >= 10, f"Expected 10+ caw.* commands, got {len(caw_cmds)}"
    assert len(cmds) >= 80, f"Expected 80+ total commands, got {len(cmds)}"
    return f"PASS: {len(caw_cmds)} caw.* commands, {len(cmds)} total"

def test_bug01_exploration_loop(trace_dir):
    """BUG-01: consecutive successful ls calls should be exploration_loop, not error_loop."""
    f = os.path.join(trace_dir, 'trace-943c5f47-6385-43ab-a05b-7a6dd132e9e2.json')
    if not os.path.exists(f): return "SKIP (file not found)"
    d = run_parser(f)
    loops = d.get('loops', [])
    ls_loops = [l for l in loops if l.get('command_normalized','').startswith('ls')]
    for l in ls_loops:
        assert l['loop_type'] == 'exploration_loop', f"ls loop should be exploration_loop, got {l['loop_type']}"
    return f"PASS: {len(ls_loops)} ls loops all exploration_loop"

def test_bug04_correctly_abandoned(trace_dir):
    """BUG-04: npm 404 errors should be correctly_abandoned."""
    f = os.path.join(trace_dir, 'trace-3ecb0f8e-0990-4eb3-912c-e6b5f409109b.json')
    if not os.path.exists(f): return "SKIP (file not found)"
    d = run_parser(f)
    rec = d.get('recovery', {})
    details = rec.get('details', [])
    abandoned = [det for det in details if det.get('outcome') == 'correctly_abandoned']
    assert len(abandoned) >= 1, f"Expected correctly_abandoned for npm 404, got {details}"
    return f"PASS: {len(abandoned)} correctly_abandoned"

if __name__ == '__main__':
    trace_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    tests = [test_bug08_generation_spans, test_bug09_caw_spans,
             test_bug01_exploration_loop, test_bug04_correctly_abandoned]
    passed = failed = skipped = 0
    for test in tests:
        try:
            result = test(trace_dir)
            if result.startswith('SKIP'):
                print(f"  SKIP  {test.__name__}: {result}")
                skipped += 1
            else:
                print(f"  PASS  {test.__name__}: {result}")
                passed += 1
        except AssertionError as e:
            print(f"  FAIL  {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {test.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(0 if failed == 0 else 1)
