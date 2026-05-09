import os
import json
import re
import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from mistralai.client.sdk import Mistral
from dotenv import load_dotenv

load_dotenv()

client = Mistral(api_key=os.getenv("MISTRAL_API_KEY"), timeout_ms=120_000)

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
# Match structured log level in the first 80 chars (before Java method signatures appear)
_LEVEL_RE = re.compile(r"\b(ERROR|SEVERE|CRITICAL|FATAL|WARN(?:ING)?|INFO|DEBUG|TRACE)\b")
# Fallback content patterns for unstructured logs (e.g. nginx, node)
_CONTENT_ERROR = re.compile(r"error|exception|timeout|fail", re.IGNORECASE)
_CONTENT_WARN = re.compile(r"\bwarn(ing)?\b", re.IGNORECASE)


def _log_severity(line: str) -> str:
    """Return 'error', 'warn', or 'other' for a log line."""
    m = _LEVEL_RE.search(line[:80])
    if m:
        lvl = m.group(1).upper()
        if lvl in ("ERROR", "SEVERE", "CRITICAL", "FATAL"):
            return "error"
        if lvl in ("WARN", "WARNING"):
            return "warn"
        return "other"
    # Unstructured log: fall back to content, but avoid false positives
    # from Java method signatures ("throws SomeException" deep in the line)
    prefix = line[:120]
    if _CONTENT_ERROR.search(prefix):
        return "error"
    if _CONTENT_WARN.search(prefix):
        return "warn"
    return "other"


def _perpendicular_distance(point: tuple, line_start: tuple, line_end: tuple) -> float:
    """Calculate perpendicular distance from point to line formed by line_start and line_end."""
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
    """Aggregate metrics using Douglas-Peucker to reduce similar points."""
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
                if not math.isnan(value):
                    points.append((timestamp, value))
            except (ValueError, TypeError):
                continue

        if len(points) < 3:
            aggregated.append(series)
            continue

        # Use a relative epsilon (5% of data range) so spikes in small-range
        # metrics (e.g. 0-1 error rates) are preserved alongside large-range ones
        # (e.g. memory in bytes). Fall back to the caller epsilon only when the
        # range is wide enough that relative scaling isn't needed.
        vals = [p[1] for p in points]
        data_range = max(vals) - min(vals)
        adaptive_epsilon = data_range * 0.05 if data_range > 0 else epsilon

        simplified_points = _douglas_peucker(points, adaptive_epsilon)

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


def _count_unique(lines: list, cap: int) -> list:
    """Deduplicate lines and return [{"line": str, "count": int}] sorted by count desc."""
    counts: dict[str, int] = {}
    for l in lines:
        key = l.strip()
        if key:
            counts[key] = counts.get(key, 0) + 1
    return [{"line": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: -x[1])[:cap]]


def _load_logs(logs_dir: Path) -> dict:
    """Read all .log files; classify lines by log level, count occurrences, and cap."""
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


def _load_jmeter_results(dataset_path: Path) -> dict:
    """Load JMeter test results and anomaly summary from dataset files."""
    results = {}

    # Try to load anomaly.log for test metadata
    anomaly_log_path = dataset_path / "anomaly.log"
    if anomaly_log_path.exists():
        try:
            lines = anomaly_log_path.read_text(errors="replace").splitlines()
            for line in lines[:10]:  # First 10 lines usually contain metadata
                if "Duration:" in line or "Users:" in line or "Ramp-up:" in line:
                    results["test_config"] = line.strip()
        except Exception:
            pass

    # Try to load comprehensive-results.csv for summary statistics
    csv_path = dataset_path / "comprehensive-results.csv"
    if csv_path.exists():
        try:
            with open(csv_path, 'r', errors='replace') as f:
                reader = csv.DictReader(f)
                error_count = 0
                request_count = 0
                services_with_issues = set()

                for row in reader:
                    if row.get('data_type') == 'jmeter_result':
                        request_count += 1
                        if row.get('error_message'):
                            error_count += 1
                            services_with_issues.add(row.get('service', 'unknown'))

                if request_count > 0:
                    results["jmeter"] = {
                        "total_requests": request_count,
                        "error_count": error_count,
                        "error_rate": f"{100 * error_count / request_count:.1f}%" if request_count > 0 else "0%",
                        "affected_services": list(services_with_issues),
                    }
        except Exception:
            pass

    return results


def _load_traces(traces_dir: Path) -> dict:
    """Summarise trace data: per-service counts, avg/max latency, slow traces. Filter non-relevant spans."""
    summary = {}
    for f in sorted(traces_dir.glob("*.json")):
        try:
            raw = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue

        # Handle different trace formats
        if isinstance(raw, list):
            # Skip list format (e.g., tempo-span-details.json)
            continue

        traces = raw.get("traces", [])
        if not traces:
            continue

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
    """Load all observability data from a dataset directory (observability/ subdirectory)."""
    base = Path(dataset_path)
    obs_base = base / "observability"

    metadata = json.loads((obs_base / "export-metadata.json").read_text())
    logs = _load_logs(obs_base / "logs")
    metrics = _load_metrics(obs_base / "metrics")
    traces = _load_traces(obs_base / "traces")
    return metadata, logs, metrics, traces


def _extract_metric_series_stats(data: list) -> list:
    """Return per-series statistics (min/avg/max/peak_time) with spike and NaN flags."""
    stats = []
    for series in data:
        label_str = ", ".join(
            f"{k}={v}" for k, v in sorted(series.get("metric", {}).items())
        )
        numeric, nan_count = [], 0
        for ts, val in series.get("values", []):
            try:
                v = float(val)
                if math.isnan(v):
                    nan_count += 1
                else:
                    numeric.append((float(ts), v))
            except (ValueError, TypeError):
                nan_count += 1

        if not numeric:
            stats.append({"labels": label_str, "all_nan": True, "total": nan_count + len(series.get("values", []))})
            continue

        vals = [v for _, v in numeric]
        avg_v = sum(vals) / len(vals)
        max_v = max(vals)
        min_v = min(vals)
        peak_ts, _ = max(numeric, key=lambda x: x[1])
        # Spike: max > 3× avg and average is non-trivial; include exact ratio
        spike_ratio = round(max_v / avg_v, 1) if avg_v > 0.001 else None
        is_spike = spike_ratio is not None and spike_ratio > 3
        # All timestamps are expressed in UTC to match Prometheus storage
        peak_time_utc = datetime.fromtimestamp(peak_ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
        stats.append({
            "labels": label_str,
            "all_nan": False,
            "min": round(min_v, 5),
            "avg": round(avg_v, 5),
            "max": round(max_v, 5),
            "peak_time": peak_time_utc,
            "data_points": len(numeric),
            "nan_count": nan_count,
            "is_spike": is_spike,
            "spike_ratio": spike_ratio,
        })
    return stats


# Human-readable descriptions for common metric file prefixes.
# Prevents the LLM from misinterpreting, e.g., 404 responses in a request-rate
# metric as an error-rate spike.
_METRIC_DESCRIPTIONS: dict[str, str] = {
    "01-http_requests_rate": (
        "HTTP request throughput (req/s) grouped by service + HTTP status code. "
        "IMPORTANT: 4xx/5xx here are response status counts, NOT a dedicated error rate — "
        "a 404 in this metric means the server returned 404 to callers, not that the service is broken."
    ),
    "02-http_response_times": (
        "HTTP response latency (seconds) per service. "
        "NaN series = no instrumentation data for that service."
    ),
    "03-container_memory": "Container memory usage in bytes.",
    "04-container_cpu": (
        "Container CPU usage (cores/s). Runs independently of HTTP traffic — "
        "a service can show CPU activity even with zero inbound HTTP requests "
        "(e.g. background threads, GC, health-check polling)."
    ),
    "05-service_health": (
        "Prometheus 'up' scrape metric (1=reachable, 0=unreachable). "
        "up=1 only means the metrics port responds — it does NOT confirm the service is handling HTTP traffic."
    ),
    "06-http_errors": "Dedicated HTTP 5xx error rate metric (errors/s).",
    "07-java_http_2xx_health": (
        "Java-specific 2xx success rate metric. "
        "0.0 means no successful HTTP responses were observed."
    ),
    "08-java_http_4xx": "Java-specific 4xx response rate metric.",
}


def _build_prompt(metadata: dict, logs: dict, metrics: dict, traces: dict, jmeter: dict = None) -> str:
    jmeter_text = ""
    if jmeter and "jmeter" in jmeter:
        jmeter_info = jmeter["jmeter"]
        jmeter_text = f"""
=== JMETER PERFORMANCE TEST ===
Total Requests: {jmeter_info["total_requests"]}
Failed Requests: {jmeter_info["error_count"]}
Error Rate: {jmeter_info["error_rate"]}
Affected Services: {", ".join(jmeter_info.get("affected_services", ["none"]))}
"""

    logs_text = ""
    for service, info in logs.items():
        if info["error_count"] == 0 and info.get("warn_count", 0) == 0:
            continue
        logs_text += f"\n[{service}] — {info['error_count']} errors, {info.get('warn_count', 0)} warnings ({info['total_lines']} total lines)\n"
        for e in info["unique_errors"][:10]:
            # Show occurrence count so single startup lines are distinguishable from
            # repeated runtime errors
            occ = f"×{e['count']}" if e["count"] > 1 else "×1 (single occurrence)"
            logs_text += f"  [ERROR {occ}] {e['line']}\n"
        for w in info.get("unique_warnings", [])[:5]:
            occ = f"×{w['count']}" if w["count"] > 1 else "×1 (single occurrence)"
            logs_text += f"  [WARN  {occ}] {w['line']}\n"

    metrics_text = ""
    for name, data in metrics.items():
        if isinstance(data, dict) and "error" in data:
            metrics_text += f"\n[{name}]: export failed — {data['error']}\n"
            continue
        if not data:
            metrics_text += f"\n[{name}]: no data\n"
            continue
        desc = _METRIC_DESCRIPTIONS.get(name, "")
        metrics_text += f"\n[{name}]{(' — ' + desc) if desc else ''}:\n"
        for s in _extract_metric_series_stats(data):
            if s["all_nan"]:
                metrics_text += f"  {s['labels']}: NO DATA — all {s['total']} points are NaN/missing\n"
            else:
                spike_flag = f" *** SPIKE ({s['spike_ratio']}×avg) ***" if s["is_spike"] else ""
                nan_note = f" ({s['nan_count']} NaN pts omitted)" if s["nan_count"] > 0 else ""
                metrics_text += (
                    f"  {s['labels']}: "
                    f"min={s['min']}, avg={s['avg']}, max={s['max']}"
                    f" (peak at {s['peak_time']}, {s['data_points']} pts{nan_note})"
                    f"{spike_flag}\n"
                )

    traces_text = ""
    for source, info in traces.items():
        traces_text += f"\n[{source}] — {info['total_traces']} total traces\n"
        for svc, stats in info["services"].items():
            traces_text += f"  {svc}: count={stats['count']}, avg={stats['avg_ms']}ms, max={stats['max_ms']}ms\n"
        if info["slow_traces"]:
            traces_text += "  Slow traces (>500ms):\n"
            for t in info["slow_traces"]:
                span_info = f" ({t['filtered_spans']}/{t['original_spans']} spans after filtering)" if t.get('original_spans') else ""
                traces_text += f"    • [{t['service']}] {t['operation']} — {t['durationMs']}ms (traceID: {t['traceID']}){span_info}\n"

    return f"""You are an expert Site Reliability Engineer performing a Root Cause Analysis.

Anomaly ID: {metadata.get('anomaly_id')}
Window (UTC): {metadata.get('expanded_window_start')} → {metadata.get('expanded_window_end')}
Stack: Prometheus (metrics), Loki (logs), Tempo (traces)
{jmeter_text}
IMPORTANT: All timestamps in this report are UTC. Metric peak times and log timestamps
must be verified to fall within the anomaly window above before being cited as evidence.

=== LOGS (errors and warnings) ===
Occurrence count is shown as ×N. A line marked "×1 (single occurrence)" happened only once
and is likely a startup or init event — do NOT treat it as a persistent runtime problem.
{logs_text or 'No errors or warnings found in logs.'}

=== METRICS ===
Format: label=value | min/avg/max | peak timestamp (UTC) | *** SPIKE (N×avg) *** if max > 3× avg
Read the description on each metric group before drawing conclusions — e.g. 4xx counts in
a throughput metric are NOT the same as a dedicated error-rate metric.
"NO DATA — all NaN/missing" = no valid measurements; treat as absent, NOT as healthy/stable.
{metrics_text}

=== TRACES ===
{traces_text}

Produce a structured Root Cause Analysis. Follow every rule below:

ACCURACY RULES (violations invalidate the analysis):
- Cite ONLY values that literally appear in the sections above. No invented numbers.
- Every timestamp you cite must fall within the anomaly window stated above (UTC).
- NaN/missing series mean "no data" — never call them "normal" or "stable".
- A log line with ×1 is a single event (often startup). Do not imply it recurred.
- `up=1` (service_health) means the metrics port responded — NOT that HTTP traffic is flowing.
- Container CPU (metric 04) runs independently of HTTP traffic. High CPU + zero HTTP traffic
  does NOT contradict itself; it may indicate GC, background threads, or health-check polling.
- The request-rate metric (01) counts ALL HTTP responses. 4xx/5xx there are throughput counts,
  NOT dedicated error rates — use metric 06 for actual error rates.

REASONING RULES:
- *** SPIKE (N×avg) *** markers show exact severity; state the ratio when citing them.
- If a metric spike and a log error share the same UTC timestamp, they are likely the same event.
- Distinguish confirmed root cause (supported by multiple signals) from hypotheses (single signal).
- If evidence is insufficient to determine root cause, say so explicitly instead of guessing.

1. **Affected Services** — list every service showing anomalies: type (error rate / latency / no data / CPU spike), values, and UTC peak times
2. **Root Cause** — most likely underlying cause; label it "Confirmed" or "Hypothesis" based on evidence strength
3. **Evidence** — cite exact metric values, UTC timestamps, log line occurrences, or trace IDs from the data above
4. **Impact** — what end-user functionality is affected
5. **Suggested Fix** — concrete remediation steps directly tied to the identified root cause
"""


def analyze_root_cause(dataset_path: str) -> str:
    """Run RCA on a full observability dataset directory and save result as text."""
    base = Path(dataset_path)
    metadata, logs, metrics, traces = load_dataset(dataset_path)
    jmeter = _load_jmeter_results(base)
    prompt = _build_prompt(metadata, logs, metrics, traces, jmeter)

    response = client.chat.complete(
        model="mistral-large-latest",
        messages=[{"role": "user", "content": prompt}],
    )
    analysis = response.choices[0].message.content

    # Save analysis as plain text file
    output_file = base / "rca-analysis.txt"
    output_file.write_text(analysis)
    print(f"RCA analysis saved to: {output_file}")

    return analysis
