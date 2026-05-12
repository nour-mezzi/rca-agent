import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from mistralai.client.sdk import Mistral
from dotenv import load_dotenv
import jsonschema

from .log_agent import LogAgent, load_logs, format_logs
from .trace_agent import TraceAgent, load_traces, format_traces
from .metrics_agent import MetricsAgent, load_metrics, format_metrics
from .reasoning_config import REASONING_PHASES, get_all_reasoning_prompts, LATENCY_ANALYSIS_PROTOCOL, CPU_MEMORY_ANALYSIS_PROTOCOL

load_dotenv()

_MODEL = "mistral-large-latest"
_MAX_TOOL_ROUNDS = 5

# Load JSON schema for RCA output validation
_SCHEMA_PATH = Path(__file__).parent / "rca_schema.json"
try:
    with open(_SCHEMA_PATH) as f:
        _RCA_SCHEMA = json.load(f)
    print(f"[Schema] Loaded RCA schema from {_SCHEMA_PATH}")
except FileNotFoundError:
    print(f"[Schema] Warning: rca_schema.json not found at {_SCHEMA_PATH}")
    _RCA_SCHEMA = None

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ask_log_agent",
            "description": (
                "Ask the Log Analysis Agent to investigate a specific question about service logs. "
                "Use when you need deeper analysis of a specific error pattern, want to correlate "
                "a log event with a metric spike, or need to establish a timeline of failures."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The specific investigation question for the log analyst.",
                    }
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_trace_agent",
            "description": (
                "Ask the Trace Analysis Agent to investigate a specific question about distributed traces. "
                "Use when you need to understand which services a slow request passed through, "
                "or want to correlate a trace latency with a log error or metric spike."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The specific investigation question for the trace analyst.",
                    }
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_metrics_agent",
            "description": (
                "Ask the Metrics Analysis Agent to investigate a specific question about Prometheus metrics. "
                "Use when you need to clarify whether a metric series shows NaN vs zero, "
                "confirm a spike ratio, or cross-reference a resource anomaly with a latency signal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The specific investigation question for the metrics analyst.",
                    }
                },
                "required": ["question"],
            },
        },
    },
]

_SYSTEM_PROMPT = """You are the RCA Orchestrator Agent — an expert Site Reliability Engineer performing Root Cause Analysis.

You will receive:
1. RAW OBSERVABILITY DATA — the ground truth: exact log lines, metric values, trace latencies.
2. SPECIALIST REPORTS — higher-level interpretations from Log, Trace, and Metrics agents.
3. TOOLS — to ask specialists targeted follow-up questions when the raw data alone is ambiguous.

WORKFLOW:
- Read the raw data first. It is your source of truth — every claim in your final report must trace back to a specific value in it.
- Read the specialist reports as a starting point for hypotheses, but do NOT copy their conclusions without verifying against the raw data.
- Use the tools to ask specialists for deeper investigation when you spot ambiguity or a cross-signal pattern that needs clarification (e.g. "do the log errors at 18:10 coincide with the catalogue latency spike?").
- When you have enough verified evidence, produce the final RCA.

ACCURACY RULES — violations invalidate the analysis:
- Cite ONLY values that literally appear in the RAW DATA or tool responses. No invented numbers.
- Every timestamp you cite must fall within the anomaly window stated above (UTC).
- NaN/missing series mean "no instrumentation data" — not absent, not healthy. State them as "no data / unreporting".
- A metric series showing all-zero error rate may mean no errors OR broken instrumentation — distinguish based on whether response-time metrics are also NaN.
- A log line marked ×1 is a single event (often startup). Do NOT treat it as a recurring runtime problem.
- `up=1` means the metrics scrape port responded — it does NOT confirm HTTP traffic is flowing.
- Metric 01 (request-rate) counts ALL HTTP responses including 4xx/5xx — use Metric 06 for actual error rates.
- A CPU spike on a service that has zero HTTP traffic does NOT contradict itself — it may be GC or background work.
- HTTP 4xx errors (especially 404) mean "resource not found" — these are routing or configuration problems. Downstream service timeouts produce 5xx (502/503/504), NOT 404. Never cite a 404 spike as evidence of a cascade from an upstream timeout.
- Every service listed in affected_services must have its anomaly accounted for in the causal_chain or explicitly noted as "cause unknown" in contributing_factors.

REASONING RULES:
- Correlate across all three signal types before labeling a root cause "Confirmed".
- A root cause supported by only one signal type must be labeled "Hypothesis".
- If two signals point to the same service at the same UTC time, that is strong confirmation.
- If evidence is genuinely insufficient, say so — do not guess.
- "Confirmed" requires BOTH: (1) at least two independent signal types point to the same failure, AND (2) every step of the causal_chain has direct evidence — a log line, a metric value, or a trace span. A metric correlation alone (e.g., service A CPU spike preceded service B latency spike) is "Hypothesis", not "Confirmed", because the mechanism is inferred.
- Build the causal_chain step by step in chronological order. Each step must name the failure domain (service/network/database/infrastructure/config), what happened, the UTC time, and a verbatim evidence citation. Do NOT skip steps — every arrow in the causal chain must be evidenced.
- FAILURE DOMAIN CLARITY: The failure_domain field captures WHERE the failure originated, NOT the most visible victim. 
  Examples: 
    - If frontend has 5xx errors because orders service is down → failure_domain: "orders" (the service that failed)
    - If requests are timing out due to network latency spike → failure_domain: "network"
    - If database queries are slow due to connection pool exhaustion → failure_domain: "database"
    - If CPU spike is from Kubernetes resource limits → failure_domain: "infrastructure"
    - If service crashes due to misconfigured environment variable → failure_domain: "configuration"
  Do NOT default to service names — consider infrastructure, network, database, and configuration as root causes.
- CROSS-SIGNAL CONTRADICTION: If metrics show 0 req/s for a service but traces contain spans for it within the anomaly window, the metric has an instrumentation gap — the service WAS handling traffic. State the discrepancy explicitly. Never call a service "idle" when traces contradict this.
- Before concluding any service received no traffic, check trace data for spans. If found, investigate with ask_trace_agent or ask_metrics_agent before proceeding.

CHAIN-OF-THOUGHT REASONING:
Before proposing a root cause, work through these phases explicitly:

PHASE 1 - SYMPTOM EXTRACTION:
  List all anomalies you observe in logs/metrics/traces. Do not interpret yet.
  For each: WHAT (exact anomaly), WHERE (service), WHEN (UTC time), MAGNITUDE.

PHASE 2 - SIGNAL MAPPING:
  For each symptom, identify which RAW DATA signals support it (logs/metrics/traces).
  Count independent signals per symptom.

PHASE 3 - HYPOTHESIS GENERATION:
  Generate ≥3 different root cause hypotheses that could explain the signals.
  For each, state: root cause, mechanism, expected signals, actual signals match?

PHASE 4 - HYPOTHESIS REFINEMENT:
  For each remaining hypothesis, answer:
    (a) Is this localized or distributed?
    (b) Does evidence show CAUSE or EFFECT?
    (c) What would disconfirm this hypothesis?
    (d) Is there temporal coherence with minimal gaps?

PHASE 5 - CROSS-SIGNAL VALIDATION:
  For your TOP hypothesis, check LOGS, METRICS, TRACES for confirmation.
  Identify any contradictions (e.g., metrics show 0 req/s but traces show spans = instrumentation gap).
  Count how many independent signals confirm this hypothesis.

PHASE 6 - CAUSAL CHAIN CONSTRUCTION:
  Build step-by-step chain from root cause to final symptom.
  Each step must have: time (UTC), service, event (factual), mechanism, evidence (verbatim).
  Verify the chain explains ALL anomalies from Phase 1.

PHASE 7 - CONFIDENCE CALIBRATION:
  Confirmed ← ≥2 independent signals + every causal step evidenced + alternatives ruled out
  Hypothesis ← Only 1 signal OR some steps inferred OR evidence ambiguous
  Be honest: if evidence is insufficient, use Hypothesis.

SPECIAL ANALYSIS PROTOCOLS:
When anomaly involves latency:
  (1) Check distribution (p50 vs p99)
  (2) Check cross-service uniformity
  (3) Check resource correlation (CPU/memory)
  (4) Map dependency chain in traces

When anomaly involves CPU/memory spike:
  (1) Is spike at 100% (hard limit) or transient?
  (2) Correlate with error rates at same time?
  (3) Is threshold context clear?
  (4) Do traces show failures or successful requests?

OUTPUT FORMAT — respond with a single valid JSON object. No text outside the JSON. No markdown fences. In string values use \\n for newlines — never embed literal newline or tab characters inside a JSON string value.

{
  "anomaly_id": "<string>",
  "window_utc": { "start": "<ISO datetime>", "end": "<ISO datetime>" },
  "affected_services": [
    {
      "service": "<name>",
      "anomaly_type": "<error_rate|latency|no_data|cpu_spike|memory_spike|unavailable|other>",
      "details": "<exact metric values and UTC peak times copied verbatim from the raw data>",
      "peak_time_utc": "<HH:MM:SS UTC or null>"
    }
  ],
  "root_cause": {
    "confidence": "<Confirmed|Hypothesis>",
    "failure_domain": "<Name of the system component where the failure originated. Can be: service name (orders, frontend), infrastructure (kubernetes, container, network), database (query timeout, connection pool), configuration issue, or other. NOT necessarily the most visible victim — identify the true source>",
    "failure_mode": "<exact failure type: crash|oom|connection_refused|dns_failure|db_timeout|db_connection_pool_exhausted|config_error|dependency_unavailable|instrumentation_gap|network_latency|network_partition|resource_exhaustion|infrastructure_throttling|other>",
    "summary": "<one sentence — must name the failure domain and failure mode, clearly stating what actually failed at the source>",
    "causal_chain": [
      {
        "step": 1,
        "time_utc": "<HH:MM:SS UTC or null if unknown>",
        "service": "<service name or 'infrastructure'/'network'/'database' if not a service>",
        "event": "<factual description of what happened at this step>",
        "evidence": "<verbatim log line with ×count, exact metric value with timestamp, or trace ID — no paraphrasing>"
      }
    ],
    "contributing_factors": ["<secondary conditions that amplified the failure or made it harder to detect — or 'none'>"]
  },
  "evidence": [
    {
      "signal": "<logs|metrics|traces>",
      "description": "<exact value, UTC timestamp, log line with occurrence count, or trace ID — no paraphrasing>"
    }
  ],
  "impact": "<description of affected end-user functionality, referencing specific services and observed error types>"
}
"""


def _sanitize_json_strings(text: str) -> str:
    """Escape literal control characters inside JSON string values."""
    result = []
    in_string = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and in_string:
            result.append(c)
            i += 1
            if i < len(text):
                result.append(text[i])
                i += 1
            continue
        if c == '"':
            in_string = not in_string
            result.append(c)
        elif in_string and c == "\n":
            result.append("\\n")
        elif in_string and c == "\r":
            result.append("\\r")
        elif in_string and c == "\t":
            result.append("\\t")
        else:
            result.append(c)
        i += 1
    return "".join(result)


def _parse_json_response(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return json.loads(_sanitize_json_strings(text))
        except json.JSONDecodeError:
            return {"raw_analysis": raw, "parse_error": "Model did not return valid JSON"}


def _validate_rca_against_schema(rca: dict) -> tuple[bool, list[str]]:
    """
    Validate RCA output against schema.
    Returns: (is_valid: bool, errors: list of error messages)
    """
    if _RCA_SCHEMA is None:
        return True, []  # Skip validation if schema not loaded
    
    errors = []
    try:
        jsonschema.validate(instance=rca, schema=_RCA_SCHEMA)
        return True, []
    except jsonschema.ValidationError as e:
        errors.append(f"Schema validation failed: {e.message}")
        errors.append(f"  Path: {'.'.join(str(p) for p in e.path)}")
        return False, errors
    except jsonschema.SchemaError as e:
        errors.append(f"Schema definition error: {e.message}")
        return False, errors


def _enrich_rca_with_metadata(rca: dict, reasoning_checkpoints: dict) -> dict:
    """
    Enrich RCA with reasoning metadata and telemetry.
    Adds timestamps and reasoning phase tracking.
    """
    if "metadata" not in rca:
        rca["metadata"] = {}
    
    rca["metadata"]["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
    rca["metadata"]["reasoning_phases_completed"] = reasoning_checkpoints.get("phases_completed", [])
    rca["metadata"]["confidence_rationale"] = reasoning_checkpoints.get("confidence_rationale", "")
    rca["metadata"]["alternatives_considered"] = reasoning_checkpoints.get("alternatives_considered", [])
    
    return rca


def _chat_orchestrator(client: Mistral, messages: list, tools: list) -> object:
    """Call the orchestrator model with retry on 429."""
    for attempt in range(5):
        try:
            return client.chat.complete(
                model=_MODEL,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as exc:
            if any(c in str(exc) for c in ("429", "503")) and attempt < 4:
                wait = 2 ** attempt * 10
                print(f"  [Orchestrator] rate-limited, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise


class OrchestratorAgent:
    """Agent 4: reads raw data summaries + specialist reports, then drives a
    tool-calling investigation loop before producing the final RCA JSON.
    """

    def __init__(self):
        self.client = Mistral(api_key=os.getenv("MISTRAL_API_KEY"), timeout_ms=120_000)
        self.log_agent = LogAgent(self.client)
        self.trace_agent = TraceAgent(self.client)
        self.metrics_agent = MetricsAgent(self.client)

    def _dispatch(self, tool_name: str, arguments: str) -> str:
        question = json.loads(arguments).get("question", "")
        if tool_name == "ask_log_agent":
            print(f"  [Log Agent] investigating: {question[:100]}...")
            return self.log_agent.investigate(question)
        if tool_name == "ask_trace_agent":
            print(f"  [Trace Agent] investigating: {question[:100]}...")
            return self.trace_agent.investigate(question)
        if tool_name == "ask_metrics_agent":
            print(f"  [Metrics Agent] investigating: {question[:100]}...")
            return self.metrics_agent.investigate(question)
        return f"Unknown tool: {tool_name}"

    def run_rca(self, dataset_path: str) -> dict:
        base = Path(dataset_path)
        obs_base = base / "observability"

        metadata = json.loads((obs_base / "export-metadata.json").read_text())
        anomaly_id = metadata.get("anomaly_id", "unknown")
        window_start = metadata.get("expanded_window_start", "")
        window_end = metadata.get("expanded_window_end", "")

        print(f"[RCA] Anomaly {anomaly_id} | window {window_start} → {window_end}")

        # --- Step 1: specialist agents produce their reports sequentially ---
        print("[Agent 1] Analyzing logs...")
        log_report = self.log_agent.analyze(obs_base / "logs")

        print("[Agent 2] Analyzing traces...")
        trace_report = self.trace_agent.analyze(obs_base / "traces")

        print("[Agent 3] Analyzing metrics...")
        metrics_report = self.metrics_agent.analyze(obs_base / "metrics")

        # --- Step 2: orchestrator reads the raw data summaries directly ---
        # These are the same formatted texts the specialist agents used,
        # giving the orchestrator a verified source of truth to cite from.
        raw_logs_text = format_logs(load_logs(obs_base / "logs"))
        raw_traces_text = format_traces(load_traces(obs_base / "traces"))
        raw_metrics_text = format_metrics(load_metrics(obs_base / "metrics"))

        print("[Agent 4] Orchestrator reading raw data + specialist reports, starting investigation...")

        system_prompt = (
            _SYSTEM_PROMPT
            + f"\nAnomaly ID: {anomaly_id}\nAnomaly window (UTC): {window_start} → {window_end}\n"
            + "All timestamps in this dataset are UTC. Only cite timestamps that fall within this window.\n"
        )

        user_message = f"""=== RAW OBSERVABILITY DATA (ground truth — cite from here) ===

--- LOGS (errors and warnings only) ---
Occurrence count shown as ×N. "×1 (single occurrence)" = likely startup/init, NOT a recurring problem.
{raw_logs_text}

--- METRICS ---
Format per series: label=value | min/avg/max | peak timestamp (UTC) | *** SPIKE (N×avg) *** if max > 3× avg
"NO DATA — all NaN/missing" = service is not exporting this metric (broken instrumentation, not healthy).
Zero error-rate + NaN response-time on the same service = instrumentation gap, not absence of errors.
{raw_metrics_text}

--- TRACES ---
{raw_traces_text}


=== SPECIALIST AGENT REPORTS (interpretation layer — verify claims against raw data above) ===

[Log Agent Report]
{log_report}

[Trace Agent Report]
{trace_report}

[Metrics Agent Report]
{metrics_report}


Cross-check the specialist reports against the raw data. Use the investigation tools to resolve any ambiguity or to deepen analysis of cross-signal patterns before writing your final RCA.
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # --- Step 3: Inject CoT reasoning checkpoint BEFORE tool calls ---
        print("[RCA] Injecting Chain-of-Thought reasoning guidance...")
        messages.append({
            "role": "user",
            "content": """Before proceeding to investigate with tools, work through the initial reasoning phases:

PHASE 1 - SYMPTOM EXTRACTION:
List all anomalies you observe in the raw data. For each: WHAT, WHERE, WHEN, MAGNITUDE.

PHASE 2 - SIGNAL MAPPING:
For each symptom, identify which signals support it (logs/metrics/traces).

PHASE 3 - HYPOTHESIS GENERATION:
Generate ≥3 different possible root causes that fit the observed signals.

PHASE 4 - HYPOTHESIS REFINEMENT:
For each hypothesis, answer: (a) localized or distributed? (b) cause or effect? (c) what would disconfirm it?

Briefly share your analysis from these phases, then ask follow-up questions via tools to resolve ambiguities."""
        })

        # Get initial reasoning
        reasoning_response = _chat_orchestrator(self.client, messages, [])
        initial_reasoning = reasoning_response.choices[0].message.content
        print("[RCA] Initial reasoning checkpoint complete")
        
        messages.append({
            "role": "assistant",
            "content": initial_reasoning
        })

        # --- Step 4: tool-calling investigation loop ---
        reasoning_checkpoints = {
            "phases_completed": [1, 2, 3, 4],  # CoT phases
            "confidence_rationale": "",
            "alternatives_considered": []
        }
        
        analysis = ""
        for round_num in range(_MAX_TOOL_ROUNDS + 1):
            response = _chat_orchestrator(self.client, messages, _TOOLS)
            choice = response.choices[0]

            if choice.finish_reason != "tool_calls":
                analysis = choice.message.content
                break

            tool_calls = choice.message.tool_calls or []
            messages.append({
                "role": "assistant",
                "content": choice.message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                result = self._dispatch(tc.function.name, tc.function.arguments)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

            if round_num == _MAX_TOOL_ROUNDS:
                print("[RCA] Max investigation rounds reached. Generating final report...")
                messages.append({
                    "role": "user",
                    "content": """Produce your final RCA report now. Before finalizing, complete these final reasoning phases:

PHASE 5 - CROSS-SIGNAL VALIDATION:
Verify your top hypothesis against all signals (logs, metrics, traces).

PHASE 6 - CAUSAL CHAIN CONSTRUCTION:
Build the evidence-backed causal chain from root cause to symptom.
Each step must have: time, service, event, and verbatim evidence.

PHASE 7 - CONFIDENCE CALIBRATION:
Determine Confirmed (≥2 signals + all steps evidenced) or Hypothesis (1 signal or inferred steps).

Then respond with the final JSON RCA."""
                })
                reasoning_checkpoints["phases_completed"] = [1, 2, 3, 4, 5, 6, 7]
                final = _chat_orchestrator(self.client, messages, [])
                analysis = final.choices[0].message.content
                break

        # --- Step 5: Parse and validate result ---
        rca = _parse_json_response(analysis)
        
        # Validate against schema
        if _RCA_SCHEMA:
            is_valid, errors = _validate_rca_against_schema(rca)
            if is_valid:
                print("[RCA] ✓ Output validated against schema")
            else:
                print("[RCA] ⚠ Schema validation warnings:")
                for error in errors:
                    print(f"  {error}")
                # Don't fail, but record issues
                if "metadata" not in rca:
                    rca["metadata"] = {}
                rca["metadata"]["validation_warnings"] = errors
        
        # Enrich with reasoning metadata
        rca = _enrich_rca_with_metadata(rca, reasoning_checkpoints)
        
        # Save result
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_file = base / f"rca-analysis-{timestamp}.json"
        output_file.write_text(json.dumps(rca, indent=2))
        print(f"[RCA] Analysis saved to: {output_file}")
        print(f"[RCA] Confidence: {rca.get('root_cause', {}).get('confidence', 'unknown')}")
        
        return rca


def analyze_root_cause(dataset_path: str) -> dict:
    """Run multi-agent RCA on a dataset directory. Returns the parsed RCA dict and saves rca-analysis.json."""
    return OrchestratorAgent().run_rca(dataset_path)
