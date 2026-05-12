import re
import time
from pathlib import Path
from mistralai.client.sdk import Mistral

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_LEVEL_RE = re.compile(r"\b(ERROR|SEVERE|CRITICAL|FATAL|WARN(?:ING)?|INFO|DEBUG|TRACE)\b")
_CONTENT_ERROR = re.compile(r"error|exception|timeout|fail", re.IGNORECASE)
_CONTENT_WARN = re.compile(r"\bwarn(ing)?\b", re.IGNORECASE)

_MODEL = "mistral-small-latest"


def _chat(client: Mistral, messages: list) -> str:
    """Call chat.complete with exponential back-off on 429."""
    for attempt in range(5):
        try:
            resp = client.chat.complete(model=_MODEL, messages=messages)
            return resp.choices[0].message.content
        except Exception as exc:
            if any(c in str(exc) for c in ("429", "503")) and attempt < 4:
                wait = 2 ** attempt * 5
                print(f"  [LogAgent] rate-limited, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def _log_severity(line: str) -> str:
    m = _LEVEL_RE.search(line[:80])
    if m:
        lvl = m.group(1).upper()
        if lvl in ("ERROR", "SEVERE", "CRITICAL", "FATAL"):
            return "error"
        if lvl in ("WARN", "WARNING"):
            return "warn"
        return "other"
    prefix = line[:120]
    if _CONTENT_ERROR.search(prefix):
        return "error"
    if _CONTENT_WARN.search(prefix):
        return "warn"
    return "other"


def _count_unique(lines: list, cap: int) -> list:
    counts: dict[str, int] = {}
    for line in lines:
        key = line.strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    return [{"line": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])[:cap]]


def load_logs(logs_dir: Path) -> dict:
    summary = {}
    for log_file in sorted(logs_dir.glob("*.log")):
        service = log_file.stem
        lines = log_file.read_text(errors="replace").splitlines()
        clean = [ANSI_ESCAPE.sub("", l) for l in lines]
        errors = [l for l in clean if _log_severity(l) == "error"]
        warnings = [l for l in clean if _log_severity(l) == "warn"]
        summary[service] = {
            "total_lines": len(lines),
            "error_count": len(errors),
            "unique_errors": _count_unique(errors, 20),
            "warn_count": len(warnings),
            "unique_warnings": _count_unique(warnings, 10),
        }
    return summary


def format_logs(logs: dict) -> str:
    text = ""
    for service, info in logs.items():
        if info["error_count"] == 0 and info.get("warn_count", 0) == 0:
            continue
        text += (
            f"\n[{service}] — {info['error_count']} errors, "
            f"{info.get('warn_count', 0)} warnings ({info['total_lines']} total lines)\n"
        )
        for e in info["unique_errors"][:10]:
            occ = f"×{e['count']}" if e["count"] > 1 else "×1 (single occurrence)"
            text += f"  [ERROR {occ}] {e['line']}\n"
        for w in info.get("unique_warnings", [])[:5]:
            occ = f"×{w['count']}" if w["count"] > 1 else "×1 (single occurrence)"
            text += f"  [WARN  {occ}] {w['line']}\n"
    return text or "No errors or warnings found in logs."


class LogAgent:
    """Agent 1: Reads and analyzes all service logs."""

    def __init__(self, client: Mistral):
        self.client = client
        self._logs_text: str = ""

    def analyze(self, logs_dir: Path) -> str:
        logs = load_logs(logs_dir)
        self._logs_text = format_logs(logs)

        prompt = f"""You are a Log Analysis specialist supporting a Root Cause Analysis team.
Analyze the service logs below and produce a concise structured report.

=== SERVICE LOGS (errors and warnings only) ===
Occurrence count shown as ×N. "×1 (single occurrence)" = likely startup/init, NOT a persistent issue.
{self._logs_text}

Report structure:
1. **Services with errors** — service name, count, most frequent error messages
2. **Services with warnings** — service name, count, most frequent warnings
3. **Anomaly patterns** — repeated errors, connection failures, timeouts, cascading failures
4. **Key findings** — top 3 most significant observations for root cause identification
"""
        return _chat(self.client, [{"role": "user", "content": prompt}])

    def investigate(self, question: str) -> str:
        """Answer a targeted follow-up question using already-loaded log data."""
        prompt = f"""You are a Log Analysis specialist. Answer the following targeted question using the service log data below.

=== SERVICE LOGS ===
{self._logs_text}

=== QUESTION ===
{question}

Provide a precise, evidence-based answer citing specific log lines and occurrence counts.
If the logs don't contain enough information to answer, say so explicitly.
"""
        return _chat(self.client, [{"role": "user", "content": prompt}])
