import json
import time
from pathlib import Path
from collections import defaultdict
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
                print(f"  [TraceAgent] rate-limited, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


def _is_relevant_span(span: dict) -> bool:
    if span.get("status", {}).get("code") != 0:
        return True
    if span.get("durationNanos", 0) > 50_000_000:
        return True
    service_name = span.get("localEndpoint", {}).get("serviceName", "")
    critical_services = {"api-gateway", "auth-service", "payment-service", "database"}
    if any(svc in service_name.lower() for svc in critical_services):
        return True
    tags = span.get("tags", {})
    if any("error" in str(v).lower() for v in tags.values()):
        return True
    logs = span.get("logs", [])
    if any("error" in str(log).lower() or "exception" in str(log).lower() for log in logs):
        return True
    return False


def _filter_trace_spans(trace: dict) -> dict:
    trace_copy = trace.copy()
    spans = trace.get("spans", [])
    if not spans:
        return trace_copy
    parent_ids: set = set()
    relevant_spans = []
    for span in spans:
        if _is_relevant_span(span):
            relevant_spans.append(span)
            parent_id = span.get("parentSpanId")
            if parent_id:
                parent_ids.add(parent_id)
    final_spans = [
        span for span in spans
        if span in relevant_spans or span.get("spanId") in parent_ids
    ]
    trace_copy["spans"] = final_spans
    trace_copy["original_span_count"] = len(spans)
    trace_copy["filtered_span_count"] = len(final_spans)
    return trace_copy


def load_traces(traces_dir: Path) -> dict:
    summary = {}
    for f in sorted(traces_dir.glob("*.json")):
        try:
            raw = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(raw, list):
            continue
        traces = raw.get("traces", [])
        if not traces:
            continue
        filtered_traces = [_filter_trace_spans(t) for t in traces]
        by_service: dict = defaultdict(list)
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


def format_traces(traces: dict) -> str:
    text = ""
    for source, info in traces.items():
        text += f"\n[{source}] — {info['total_traces']} total traces\n"
        for svc, stats in info["services"].items():
            text += f"  {svc}: count={stats['count']}, avg={stats['avg_ms']}ms, max={stats['max_ms']}ms\n"
        if info["slow_traces"]:
            text += "  Slow traces (>500ms):\n"
            for t in info["slow_traces"]:
                span_info = (
                    f" ({t['filtered_spans']}/{t['original_spans']} spans after filtering)"
                    if t.get("original_spans") else ""
                )
                text += (
                    f"    • [{t['service']}] {t['operation']} — "
                    f"{t['durationMs']}ms (traceID: {t['traceID']}){span_info}\n"
                )
    return text or "No trace data available."


class TraceAgent:
    """Agent 2: Reads and analyzes all distributed traces."""

    def __init__(self, client: Mistral):
        self.client = client
        self._traces_text: str = ""

    def analyze(self, traces_dir: Path) -> str:
        traces = load_traces(traces_dir)
        self._traces_text = format_traces(traces)

        prompt = f"""You are a Distributed Tracing specialist supporting a Root Cause Analysis team.
Analyze the trace data below and produce a concise structured report.

=== DISTRIBUTED TRACES ===
{self._traces_text}

Report structure:
1. **Service latency summary** — per-service avg/max latency and request counts
2. **Slow traces** — traces exceeding 500ms with service, operation, and duration
3. **Latency hotspots** — services with the highest max or avg latency
4. **Error traces** — any traces indicating failures or errors
5. **Key findings** — top 3 most significant trace-based observations for root cause identification
"""
        return _chat(self.client, [{"role": "user", "content": prompt}])

    def investigate(self, question: str) -> str:
        """Answer a targeted follow-up question using already-loaded trace data."""
        prompt = f"""You are a Distributed Tracing specialist. Answer the following targeted question using the trace data below.

=== DISTRIBUTED TRACES ===
{self._traces_text}

=== QUESTION ===
{question}

Provide a precise, evidence-based answer citing specific trace IDs, service names, and latency values.
If the traces don't contain enough information to answer, say so explicitly.
"""
        return _chat(self.client, [{"role": "user", "content": prompt}])
