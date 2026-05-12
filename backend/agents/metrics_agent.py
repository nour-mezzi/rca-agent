import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from mistralai.client.sdk import Mistral

_MODEL = "mistral-small-latest"


def _chat(client: Mistral, messages: list) -> str:
    for attempt in range(5):
        try:
            resp = client.chat.complete(model=_MODEL, messages=messages)
            return resp.choices[0].message.content
        except Exception as exc:
            if any(c in str(exc) for c in ("429", "503")) and attempt < 4:
                wait = 2 ** attempt * 5
                print(f"  [MetricsAgent] rate-limited, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise

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
    if len(points) < 3:
        return points
    max_distance = 0
    max_index = 0
    for i in range(1, len(points) - 1):
        d = _perpendicular_distance(points[i], points[0], points[-1])
        if d > max_distance:
            max_distance = d
            max_index = i
    if max_distance > epsilon:
        left = _douglas_peucker(points[:max_index + 1], epsilon)
        right = _douglas_peucker(points[max_index:], epsilon)
        return left[:-1] + right
    return [points[0], points[-1]]


def _simplify_series(metrics_data: list, epsilon: float = 1.0) -> list:
    aggregated = []
    for series in metrics_data:
        values = series.get("values", [])
        if len(values) < 3:
            aggregated.append(series)
            continue
        points = []
        for ts_str, val_str in values:
            try:
                ts = float(ts_str)
                v = float(val_str)
                if not math.isnan(v):
                    points.append((ts, v))
            except (ValueError, TypeError):
                continue
        if len(points) < 3:
            aggregated.append(series)
            continue
        vals = [p[1] for p in points]
        data_range = max(vals) - min(vals)
        adaptive_eps = data_range * 0.05 if data_range > 0 else epsilon
        simplified = _douglas_peucker(points, adaptive_eps)
        copy = series.copy()
        copy["values"] = [[str(int(p[0])), str(p[1])] for p in simplified]
        copy["original_point_count"] = len(values)
        copy["simplified_point_count"] = len(simplified)
        aggregated.append(copy)
    return aggregated


def _compute_original_stats(series: dict) -> dict:
    """Compute min/avg/max/spike stats from the full series before any simplification."""
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
        return {"all_nan": True, "total": nan_count + len(series.get("values", []))}
    vals = [v for _, v in numeric]
    avg_v = sum(vals) / len(vals)
    max_v = max(vals)
    min_v = min(vals)
    peak_ts, _ = max(numeric, key=lambda x: x[1])
    spike_ratio = round(max_v / avg_v, 1) if avg_v > 0.001 else None
    is_spike = spike_ratio is not None and spike_ratio > 3
    peak_time = datetime.fromtimestamp(peak_ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
    return {
        "all_nan": False,
        "min": round(min_v, 5),
        "avg": round(avg_v, 5),
        "max": round(max_v, 5),
        "peak_time": peak_time,
        "data_points": len(numeric),
        "nan_count": nan_count,
        "is_spike": is_spike,
        "spike_ratio": spike_ratio,
    }


def load_metrics(metrics_dir: Path) -> dict:
    results = {}
    for f in sorted(metrics_dir.glob("*.json")):
        raw = json.loads(f.read_text())
        if raw.get("status") != "success":
            results[f.stem] = {"error": raw.get("error", "unknown error")}
            continue
        result_data = raw.get("data", {}).get("result", [])
        # Stats must come from the full series — simplification skews the average.
        label_to_stats = {
            ", ".join(f"{k}={v}" for k, v in sorted(s.get("metric", {}).items())): _compute_original_stats(s)
            for s in result_data
        }
        simplified = _simplify_series(result_data)
        for s in simplified:
            label_key = ", ".join(f"{k}={v}" for k, v in sorted(s.get("metric", {}).items()))
            if label_key in label_to_stats:
                s["_original_stats"] = label_to_stats[label_key]
        results[f.stem] = simplified
    return results


def _extract_series_stats(data: list) -> list:
    stats = []
    for series in data:
        label_str = ", ".join(f"{k}={v}" for k, v in sorted(series.get("metric", {}).items()))
        if "_original_stats" in series:
            stats.append({"labels": label_str, **series["_original_stats"]})
            continue
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
        spike_ratio = round(max_v / avg_v, 1) if avg_v > 0.001 else None
        is_spike = spike_ratio is not None and spike_ratio > 3
        peak_time = datetime.fromtimestamp(peak_ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
        stats.append({
            "labels": label_str,
            "all_nan": False,
            "min": round(min_v, 5),
            "avg": round(avg_v, 5),
            "max": round(max_v, 5),
            "peak_time": peak_time,
            "data_points": len(numeric),
            "nan_count": nan_count,
            "is_spike": is_spike,
            "spike_ratio": spike_ratio,
        })
    return stats


def format_metrics(metrics: dict) -> str:
    text = ""
    for name, data in metrics.items():
        if isinstance(data, dict) and "error" in data:
            text += f"\n[{name}]: export failed — {data['error']}\n"
            continue
        if not data:
            text += f"\n[{name}]: no data\n"
            continue
        desc = _METRIC_DESCRIPTIONS.get(name, "")
        text += f"\n[{name}]{(' — ' + desc) if desc else ''}:\n"
        for s in _extract_series_stats(data):
            if s["all_nan"]:
                text += f"  {s['labels']}: NO DATA — all {s['total']} points are NaN/missing\n"
            else:
                spike_flag = f" *** SPIKE ({s['spike_ratio']}×avg) ***" if s["is_spike"] else ""
                nan_note = f" ({s['nan_count']} NaN pts omitted)" if s["nan_count"] > 0 else ""
                text += (
                    f"  {s['labels']}: "
                    f"min={s['min']}, avg={s['avg']}, max={s['max']}"
                    f" (peak at {s['peak_time']}, {s['data_points']} pts{nan_note})"
                    f"{spike_flag}\n"
                )
    return text or "No metrics data available."


class MetricsAgent:
    """Agent 3: Reads and analyzes all Prometheus metrics."""

    def __init__(self, client: Mistral):
        self.client = client
        self._metrics_text: str = ""

    def analyze(self, metrics_dir: Path) -> str:
        metrics = load_metrics(metrics_dir)
        self._metrics_text = format_metrics(metrics)

        prompt = f"""You are a Metrics Analysis specialist supporting a Root Cause Analysis team.
Analyze the Prometheus metrics below and produce a concise structured report.

=== METRICS ===
Format: label=value | min/avg/max | peak timestamp (UTC) | *** SPIKE (N×avg) *** if max > 3× avg
"NO DATA — all NaN/missing" = no valid measurements (treat as absent, NOT healthy/stable).
4xx/5xx in the request-rate metric (01) are throughput counts, NOT dedicated error rates.
{self._metrics_text}

Report structure:
1. **Service health overview** — which services are up/down, any availability gaps
2. **Latency anomalies** — services with high avg/max response times or spikes
3. **Error rate anomalies** — services with elevated 5xx rates (metric 06)
4. **Resource anomalies** — CPU/memory spikes (metrics 03, 04)
5. **Key findings** — top 3 most significant metric-based observations for root cause identification
"""
        return _chat(self.client, [{"role": "user", "content": prompt}])

    def investigate(self, question: str) -> str:
        """Answer a targeted follow-up question using already-loaded metrics data."""
        prompt = f"""You are a Metrics Analysis specialist. Answer the following targeted question using the Prometheus metrics below.

=== METRICS ===
{self._metrics_text}

=== QUESTION ===
{question}

Provide a precise, evidence-based answer citing specific metric values, labels, timestamps, and spike ratios.
If the metrics don't contain enough information to answer, say so explicitly.
"""
        return _chat(self.client, [{"role": "user", "content": prompt}])
