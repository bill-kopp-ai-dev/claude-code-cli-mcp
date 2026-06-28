import json
import subprocess
import sys
from pathlib import Path

# Add src to python path
src_dir = Path(__file__).resolve().parents[1] / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from claude_code_mcp.claude_runner import start_sync_run, wait_sync
from claude_code_mcp.models import ClaudeRunTaskRequest
from claude_code_mcp.settings import Settings

def main():
    # 1. Run bare claude CLI command to get exact JSON
    cmd = ["claude", "-p", "Reply with the single token: CLAUDE_SMOKE_OK", "--model", "sonnet", "--output-format", "json"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = proc.communicate()
    
    # 2. Parse stdout using the exact same parsing logic as server.py (claude_run_task)
    result_dict = {}
    parse_error = None
    try:
        result_dict = json.loads(stdout.strip())
    except json.JSONDecodeError as e:
        parse_error = str(e)
        result_dict = {"result": stdout, "parse_error": parse_error}

    if not isinstance(result_dict, dict):
        result_dict = {"result": stdout, "parse_error": "Parsed JSON was not a dictionary"}

    print("Parsed result_dict keys:", list(result_dict.keys()))
    print("result:", repr(result_dict.get("result")))
    print("parse_error:", repr(result_dict.get("parse_error")))

    assert parse_error is None, f"Expected no parse_error, got {parse_error}"
    assert result_dict.get("result") == "CLAUDE_SMOKE_OK", f"Expected 'CLAUDE_SMOKE_OK', got {result_dict.get('result')}"
    print("PARSE_SMOKE_SUCCESS")

if __name__ == "__main__":
    main()
