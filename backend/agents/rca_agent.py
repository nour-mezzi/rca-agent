import os
import json
import re
from pathlib import Path
from collections import defaultdict
from mistralai.client.sdk import Mistral
from dotenv import load_dotenv

load_dotenv()

client = Mistral(api_key=os.getenv("MISTRAL_API_KEY"), timeout_ms=120_000)

ERROR_PATTERN = re.compile(r"error|exception|timeout|fail", re.IGNORECASE)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _load_logs(logs_dir: Path) -> dict:
    """Read all .log files, extract error/warning lines per service."""
    summary = {}
    for log_file in sorted(logs_dir.glob("*.log")):
        service = log_file.stem
        lines = log_file.read_text(errors="replace").splitlines()
        errors = [ANSI_ESCAPE.sub("", l) for l in lines if ERROR_PATTERN.search(l)]
        # Deduplicate while preserving order
        seen = set()
        unique_errors = []
        for e in errors:
            key = e.strip()
            if key not in seen:
                seen.add(key)
                unique_errors.append(e.strip())
        summary[service] = {
            "total_lines": len(lines),
            "error_count": len(errors),
            "unique_errors": unique_errors[:20],  # cap at 20 distinct messages
        }
    return summary


def _load_metrics(metrics_dir: Path) -> dict:
    """Parse Prometheus JSON exports; skip error responses."""
    results = {}
    for f in sorted(metrics_dir.glob("*.json")):
        raw = json.loads(f.read_text())
        if raw.get("status") != "success":
            results[f.stem] = {"error": raw.get("error", "unknown error")}
            continue
        result_data = raw.get("data", {}).get("result", [])
        results[f.stem] = result_data
    return results


def _load_traces(traces_dir: Path) -> dict:
    """Summarise trace data: per-service counts, avg/max latency, slow traces."""
    summary = {}
    for f in sorted(traces_dir.glob("*.json")):
        try:
            raw = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        traces = raw.get("traces", [])
        by_service = defaultdict(list)
        for t in traces:
            by_service[t["rootServiceName"]].append(t.get("durationMs", 0))

        service_stats = {}
        slow_traces = []
        for svc, durations in by_service.items():
            avg = sum(durations) / len(durations) if durations else 0
            service_stats[svc] = {
                "count": len(durations),
                "avg_ms": round(avg, 1),
                "max_ms": max(durations) if durations else 0,
            }

        for t in traces:
            if t.get("durationMs", 0) > 500:
                slow_traces.append({
                    "service": t["rootServiceName"],
                    "operation": t.get("rootTraceName"),
                    "durationMs": t["durationMs"],
                    "traceID": t["traceID"],
                })

        summary[f.stem] = {
            "total_traces": len(traces),
            "services": service_stats,
            "slow_traces": slow_traces,
        }
    return summary


def load_dataset(dataset_path: str) -> tuple[dict, dict, dict, dict]:
    """Load all observability data from a dataset directory."""
    base = Path(dataset_path)
    metadata = json.loads((base / "export-metadata.json").read_text())
    logs = _load_logs(base / "logs")
    metrics = _load_metrics(base / "metrics")
    traces = _load_traces(base / "traces")
    return metadata, logs, metrics, traces


def _build_prompt(metadata: dict, logs: dict, metrics: dict, traces: dict) -> str:
    logs_text = ""
    for service, info in logs.items():
        if info["error_count"] == 0:
            continue
        logs_text += f"\n[{service}] — {info['error_count']} error lines ({info['total_lines']} total)\n"
        for e in info["unique_errors"][:10]:
            logs_text += f"  • {e}\n"

    metrics_text = ""
    for name, data in metrics.items():
        if isinstance(data, dict) and "error" in data:
            metrics_text += f"\n[{name}]: export failed — {data['error']}\n"
        elif data:
            metrics_text += f"\n[{name}]: {len(data)} time series returned\n"
        else:
            metrics_text += f"\n[{name}]: no data\n"

    traces_text = ""
    for source, info in traces.items():
        traces_text += f"\n[{source}] — {info['total_traces']} total traces\n"
        for svc, stats in info["services"].items():
            traces_text += f"  {svc}: count={stats['count']}, avg={stats['avg_ms']}ms, max={stats['max_ms']}ms\n"
        if info["slow_traces"]:
            traces_text += "  Slow traces (>500ms):\n"
            for t in info["slow_traces"]:
                traces_text += f"    • [{t['service']}] {t['operation']} — {t['durationMs']}ms (traceID: {t['traceID']})\n"

    return f"""You are an expert Site Reliability Engineer performing a Root Cause Analysis.

Anomaly ID: {metadata.get('anomaly_id')}
Window: {metadata.get('expanded_window_start')} → {metadata.get('expanded_window_end')}
Stack: Prometheus (metrics), Loki (logs), Tempo (traces)

=== LOGS (errors only) ===
{logs_text or 'No errors found in logs.'}

=== METRICS ===
{metrics_text}

=== TRACES ===
{traces_text}

Based on the above observability data, provide a structured Root Cause Analysis:

1. **Affected Services** — which services show anomalies and what kind
2. **Root Cause** — the most likely underlying cause
3. **Evidence** — specific log lines, trace IDs, or metric anomalies that support your conclusion
4. **Impact** — what end-user functionality is affected
5. **Suggested Fix** — concrete remediation steps
"""


def analyze_root_cause(dataset_path: str) -> str:
    """Run RCA on a full observability dataset directory."""
    metadata, logs, metrics, traces = load_dataset(dataset_path)
    prompt = _build_prompt(metadata, logs, metrics, traces)

    response = client.chat.complete(
        model="mistral-large-latest",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content
