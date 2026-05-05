import os
import json
import re
import math
from pathlib import Path
from collections import defaultdict
from mistralai.client.sdk import Mistral
from dotenv import load_dotenv

load_dotenv()

client = Mistral(api_key=os.getenv("MISTRAL_API_KEY"), timeout_ms=120_000)

ERROR_PATTERN = re.compile(r"error|exception|timeout|fail", re.IGNORECASE)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _perpendicular_distance(point: tuple, line_start: tuple, line_end: tuple) -> float:
    if line_start == line_end:
        return math.sqrt((point[0] - line_start[0]) ** 2 + (point[1] - line_start[1]) ** 2)
    
    x0, y0 = point
    x1, y1 = line_start
    x2, y2 = line_end
    
    numerator = abs((y2 - y1) * x0 - (x2 - x1) * y0 + x2 * y1 - y2 * x1)
    denominator = math.sqrt((y2 - y1) ** 2 + (x2 - x1) ** 2)
    
    return numerator / denominator if denominator != 0 else 0


def _douglas_peucker(points: list, epsilon: float) -> list:
    """
    Apply Douglas-Peucker algorithm to reduce number of points while preserving shape.
    Points should be tuples of (timestamp/x, value/y).
    """
    if len(points) < 3:
        return points
    
    # Find point with maximum distance from line segment
    max_distance = 0
    max_index = 0
    for i in range(1, len(points) - 1):
        distance = _perpendicular_distance(points[i], points[0], points[-1])
        if distance > max_distance:
            max_distance = distance
            max_index = i
    
    # If max distance exceeds epsilon, recursively simplify
    if max_distance > epsilon:
        left_points = _douglas_peucker(points[:max_index + 1], epsilon)
        right_points = _douglas_peucker(points[max_index:], epsilon)
        return left_points[:-1] + right_points
    
    return [points[0], points[-1]]


def _aggregate_metrics_with_simplification(metrics_data: list, epsilon: float = 1.0) -> list:
    if not metrics_data:
        return metrics_data
    
    aggregated = []
    for series in metrics_data:
        values = series.get("values", [])
        if len(values) < 3:
            aggregated.append(series)
            continue
        
        # Convert to points (timestamp, value) for Douglas-Peucker
        points = []
        for timestamp_str, value_str in values:
            try:
                timestamp = float(timestamp_str)
                value = float(value_str)
                points.append((timestamp, value))
            except (ValueError, TypeError):
                continue
        
        if len(points) < 3:
            aggregated.append(series)
            continue
        
        # Apply Douglas-Peucker simplification
        simplified_points = _douglas_peucker(points, epsilon)
        
        # Convert back to original format
        series_copy = series.copy()
        series_copy["values"] = [[str(int(p[0])), str(p[1])] for p in simplified_points]
        series_copy["original_point_count"] = len(values)
        series_copy["simplified_point_count"] = len(simplified_points)
        aggregated.append(series_copy)
    
    return aggregated


def _is_relevant_span(span: dict) -> bool:
    """Determine if a span is relevant for RCA analysis."""
    # Keep spans that are errors
    if span.get("status", {}).get("code") != 0:
        return True
    
    # Keep spans with significant duration (>50ms)
    duration_ns = span.get("durationNanos", 0)
    if duration_ns > 50_000_000:  # 50ms in nanoseconds
        return True
    
    # Keep spans from critical services
    service_name = span.get("localEndpoint", {}).get("serviceName", "")
    critical_services = {"api-gateway", "auth-service", "payment-service", "database"}
    if any(svc in service_name.lower() for svc in critical_services):
        return True
    
    # Keep spans that have errors in tags or logs
    tags = span.get("tags", {})
    if any("error" in str(v).lower() for v in tags.values()):
        return True
    
    logs = span.get("logs", [])
    if any("error" in str(log).lower() or "exception" in str(log).lower() for log in logs):
        return True
    
    # Filter out very short spans from non-critical services
    return False


def _filter_trace_spans(trace: dict) -> dict:
    """Filter out non-relevant spans from a trace to reduce bloat."""
    trace_copy = trace.copy()
    spans = trace.get("spans", [])
    
    if not spans:
        return trace_copy
    
    # Always keep the root span
    root_span_id = trace.get("traceID")
    parent_ids = set()
    
    # First pass: identify relevant spans and their parents
    relevant_spans = []
    for span in spans:
        if _is_relevant_span(span):
            relevant_spans.append(span)
            parent_id = span.get("parentSpanId")
            if parent_id:
                parent_ids.add(parent_id)
    
    # Second pass: keep all relevant spans + their direct parents to maintain trace structure
    final_spans = []
    for span in spans:
        span_id = span.get("spanId")
        if span in relevant_spans or span_id in parent_ids:
            final_spans.append(span)
    
    trace_copy["spans"] = final_spans
    trace_copy["original_span_count"] = len(spans)
    trace_copy["filtered_span_count"] = len(final_spans)
    
    return trace_copy


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
    """Parse Prometheus JSON exports; skip error responses and apply Douglas-Peucker simplification."""
    results = {}
    for f in sorted(metrics_dir.glob("*.json")):
        raw = json.loads(f.read_text())
        if raw.get("status") != "success":
            results[f.stem] = {"error": raw.get("error", "unknown error")}
            continue
        result_data = raw.get("data", {}).get("result", [])
        # Apply Douglas-Peucker simplification to reduce similar data points
        simplified_data = _aggregate_metrics_with_simplification(result_data, epsilon=1.0)
        results[f.stem] = simplified_data
    return results


def _load_traces(traces_dir: Path) -> dict:
    """Summarise trace data: per-service counts, avg/max latency, slow traces. Filter non-relevant spans."""
    summary = {}
    for f in sorted(traces_dir.glob("*.json")):
        try:
            raw = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        traces = raw.get("traces", [])
        
        # Filter non-relevant spans from each trace
        filtered_traces = [_filter_trace_spans(t) for t in traces]
        
        by_service = defaultdict(list)
        for t in filtered_traces:
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

        for t in filtered_traces:
            if t.get("durationMs", 0) > 500:
                slow_traces.append({
                    "service": t["rootServiceName"],
                    "operation": t.get("rootTraceName"),
                    "durationMs": t["durationMs"],
                    "traceID": t["traceID"],
                    "original_spans": t.get("original_span_count", 0),
                    "filtered_spans": t.get("filtered_span_count", 0),
                })

        summary[f.stem] = {
            "total_traces": len(filtered_traces),
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


_SYSTEM_PROMPT = """You are an expert Site Reliability Engineer and incident responder with deep experience in distributed systems observability. You diagnose production incidents using three signal types: metrics (Prometheus), logs (Loki), and traces (Tempo).

Reasoning process — work through these steps in order:
1. Per-signal analysis: for each signal, identify what is anomalous, when it started, and which services are involved
2. Cross-signal correlation: find temporal alignment and shared services across signals to build a causal chain
3. Hypothesis ranking: list candidate root causes ordered by evidence strength
4. Conclusion: select the best-supported hypothesis and cite exact evidence

Hard rules:
- Only cite evidence present in the provided data (specific log lines, trace IDs, metric names, timestamps)
- If evidence is insufficient to reach a conclusion, state your confidence level and explain what is missing
- Prefer one well-evidenced root cause over multiple speculative ones
- Never suggest fixes unrelated to the observed evidence"""


def _build_prompt(metadata: dict, logs: dict, metrics: dict, traces: dict) -> str:
    logs_text = ""
    for service, info in logs.items():
        if info["error_count"] == 0:
            continue
        logs_text += f"\n[{service}] {info['error_count']} errors / {info['total_lines']} total lines\n"
        for e in info["unique_errors"][:10]:
            logs_text += f"  • {e}\n"

    metrics_text = ""
    for name, data in metrics.items():
        if isinstance(data, dict) and "error" in data:
            metrics_text += f"\n[{name}] EXPORT FAILED: {data['error']}\n"
        elif data:
            metrics_text += f"\n[{name}] — {len(data)} time series\n"
            for series in data[:5]:
                labels = series.get("metric", {})
                values = series.get("values", [])
                if not values:
                    continue
                label_str = ", ".join(f"{k}={v}" for k, v in labels.items() if k != "__name__")
                numeric_vals = []
                for v in values:
                    try:
                        numeric_vals.append(float(v[1]))
                    except (IndexError, ValueError, TypeError):
                        pass
                if numeric_vals:
                    mn, mx, avg = min(numeric_vals), max(numeric_vals), sum(numeric_vals) / len(numeric_vals)
                    metrics_text += f"  {label_str or '(no labels)'}: min={mn:.3g}, avg={avg:.3g}, max={mx:.3g} ({len(numeric_vals)} pts)\n"
        else:
            metrics_text += f"\n[{name}] no data\n"

    traces_text = ""
    for source, info in traces.items():
        traces_text += f"\n[{source}] {info['total_traces']} total traces\n"
        for svc, stats in info["services"].items():
            traces_text += f"  {svc}: {stats['count']} calls, avg {stats['avg_ms']}ms, max {stats['max_ms']}ms\n"
        if info["slow_traces"]:
            traces_text += "  Slow traces (>500ms):\n"
            for t in info["slow_traces"]:
                span_info = f" [{t['filtered_spans']}/{t['original_spans']} spans]" if t.get("original_spans") else ""
                traces_text += f"    • [{t['service']}] {t['operation']} — {t['durationMs']}ms  traceID={t['traceID']}{span_info}\n"

    anomaly_id = metadata.get("anomaly_id", "unknown")
    window_start = metadata.get("expanded_window_start", "?")
    window_end = metadata.get("expanded_window_end", "?")

    return f"""Anomaly ID: {anomaly_id}
Incident window: {window_start} → {window_end}
Observability stack: Prometheus (metrics), Loki (logs), Tempo (traces)

---
## LOGS (errors only)
{logs_text.strip() or "No error-level log lines found."}

---
## METRICS
{metrics_text.strip() or "No metric data available."}

---
## TRACES
{traces_text.strip() or "No trace data available."}

---

Using the observability data above, produce a Root Cause Analysis in the following format:

### Signal Analysis
Summarize what each signal (logs, metrics, traces) reveals independently — which services are affected and what the anomaly looks like per signal.

### Cross-Signal Correlation
Identify temporal and service-level patterns that connect the signals. Point to the causal chain.

### Hypothesis Ranking
List the top candidate root causes, each tagged [HIGH / MEDIUM / LOW] confidence, with the specific evidence that supports or undermines each.

### Root Cause
State the single most likely root cause in one or two sentences. Cite the exact evidence (log line, trace ID, metric name) that clinches it.

### Impact
Describe the end-user or system-level impact during the incident window.

### Confidence
Rate overall confidence*: HIGH / MEDIUM / LOW. If MEDIUM or LOW, state what additional data would raise confidence.

### Remediation
- **Immediate:** actions to stop the bleeding right now
- **Short-term:** fixes to prevent recurrence
- **Long-term:** structural improvements to observability or architecture"""


def analyze_root_cause(dataset_path: str, output_path: str | None = None) -> str:
    """Run RCA on a full observability dataset directory and optionally save to JSON."""
    metadata, logs, metrics, traces = load_dataset(dataset_path)
    prompt = _build_prompt(metadata, logs, metrics, traces)

    response = client.chat.complete(
        model="mistral-large-latest",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    )
    analysis = response.choices[0].message.content

    output_file = Path(output_path) if output_path else Path(dataset_path) / "rca_output.json"
    result = {
        "anomaly_id": metadata.get("anomaly_id"),
        "window_start": metadata.get("expanded_window_start"),
        "window_end": metadata.get("expanded_window_end"),
        "model": "mistral-large-latest",
        "analysis": analysis,
    }
    output_file.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"RCA saved to {output_file}")

    return analysis
