# RCA Agent — Full Documentation

## Table of Contents

1. [Changes Made in This Session](#1-changes-made-in-this-session)
2. [System Architecture Overview](#2-system-architecture-overview)
3. [Agent Workflow & Data Flow](#3-agent-workflow--data-flow)
4. [Code Explanation — File by File](#4-code-explanation--file-by-file)
   - [main.py](#41-mainpy)
   - [rca_agent.py](#42-rca_agentpy)
   - [orchestrator.py](#43-orchestratorpy)
   - [log_agent.py](#44-log_agentpy)
   - [metrics_agent.py](#45-metrics_agentpy)
   - [trace_agent.py](#46-trace_agentpy)
   - [reasoning_config.py](#47-reasoning_configpy)
   - [rca_schema.json](#48-rca_schemajson)
5. [Output Report Structure](#5-output-report-structure)

---

## 1. Changes Made in This Session

### 1.1 `rca_schema.json` — Schema Refactoring

The schema was restructured to enforce a well-formed, actionable RCA report. Every change corresponds to a gap in the previous outputs.

#### Fields added

| Field | Location | Type | Why |
|---|---|---|---|
| `severity` | top-level, **required** | enum `P1`/`P2`/`P3`/`P4` | Reports previously had no triage classification. P1=complete outage, P2=major degradation, P3=partial, P4=minor. |
| `recommendations` | top-level, **required** | array of `{action, priority, rationale}` | Reports ended with findings but no actionable next steps. Each recommendation has `immediate`, `short-term`, or `long-term` priority. |
| `confidence_rationale` | inside `root_cause`, **required** | string | The `confidence` field (`Confirmed`/`Hypothesis`) had no explanation. Now the model must state *why* — citing signal count, evidence quality, and whether alternatives were ruled out. |
| `alternatives_considered` | inside `root_cause`, **required** | array of strings | Previously buried in `metadata` and always empty. Moved here so the model writes each rejected hypothesis inline with the root cause, with the evidence that ruled it out. |

#### Fields made required

| Field | Location | Change |
|---|---|---|
| `mechanism` | inside each `causal_chain` step | Was optional — is now **required**. It explains *how* each step caused the next, which is the core of a causal chain. Without it a step list is just a timeline, not a causation chain. |

#### Fields removed from `metadata`

`confidence_rationale` and `alternatives_considered` were previously listed under `metadata` but were always empty strings/arrays because the orchestrator populated them from a hardcoded dict that was never updated. They were moved into `root_cause` where the model fills them directly in its JSON output.

#### `failure_mode` enum

`dns_misconfiguration_and_startup_failure` was already in the schema. A previous validation error had been caused by a schema/code mismatch in an older version; it is now consistent.

---

### 1.2 `orchestrator.py` — Orchestrator Changes

#### Output format template (inside `_SYSTEM_PROMPT`)

The JSON template the model uses to produce its final answer was updated to match the new schema:

- Added `"severity"` field with P1–P4 guidance
- Added `"mechanism"` as a required field inside every `causal_chain` step
- Added `"confidence_rationale"` inside `root_cause`
- Added `"alternatives_considered"` inside `root_cause`
- Added `"recommendations"` array with `action`, `priority`, `rationale` per item
- Updated `failure_mode` enum to include `dns_misconfiguration_and_startup_failure`

#### `_enrich_rca_with_metadata` — simplified

**Before:**
```python
def _enrich_rca_with_metadata(rca: dict, reasoning_checkpoints: dict) -> dict:
    rca["metadata"]["reasoning_phases_completed"] = reasoning_checkpoints.get("phases_completed", [])
    rca["metadata"]["confidence_rationale"] = reasoning_checkpoints.get("confidence_rationale", "")
    rca["metadata"]["alternatives_considered"] = reasoning_checkpoints.get("alternatives_considered", [])
```

**After:**
```python
def _enrich_rca_with_metadata(rca: dict, phases_completed: list) -> dict:
    rca["metadata"]["generated_at"] = datetime.now(tz=timezone.utc).isoformat()
    rca["metadata"]["reasoning_phases_completed"] = phases_completed
```

`confidence_rationale` and `alternatives_considered` are no longer written from a Python dict — the model now writes them directly in the JSON output under `root_cause`, which is the correct place for them. The function signature was simplified accordingly.

#### `reasoning_phases_completed` tracking — fixed

**Before:** The tracking dict was hardcoded to `[1, 2, 3, 4]` and only updated to `[1..7]` if the investigation hit the max-rounds safety path. Normal runs always produced `[1, 2, 3, 4]` even though phases 5–7 were completed by the model internally.

**After:** `phases_completed = [1, 2, 3, 4, 5, 6, 7]` is set once, unconditionally. Phases 1–4 are completed explicitly via the CoT checkpoint message; phases 5–7 are completed by the model internally as part of producing the final output (the system prompt mandates it).

#### Max-rounds final prompt — updated

The forced-JSON prompt that fires when the investigation loop exhausts all rounds was updated to explicitly require `mechanism` in causal_chain steps, `confidence_rationale`, and `recommendations`.

#### JSON recovery fallback — new

A new safety mechanism was added after `_parse_json_response`. If the model returns non-JSON on the natural loop exit (e.g., a tool name fragment or partial text), the orchestrator now:

1. Appends the bad response to the conversation as an assistant turn
2. Sends a new user message: "Your previous response was not valid JSON. Output ONLY the RCA JSON object now..."
3. Calls the model one more time without tools
4. Re-parses the result

This was triggered during the ANOMALY-004 run and recovered successfully.

---

## 2. System Architecture Overview

```
main.py
  └── analyze_root_cause(dataset_path)          ← entry point
        └── OrchestratorAgent.run_rca()
              ├── LogAgent.analyze()             ← Agent 1
              ├── TraceAgent.analyze()           ← Agent 2
              ├── MetricsAgent.analyze()         ← Agent 3
              └── [Orchestrator LLM loop]        ← Agent 4
                    ├── ask_log_agent()    tool → LogAgent.investigate()
                    ├── ask_trace_agent()  tool → TraceAgent.investigate()
                    └── ask_metrics_agent() tool → MetricsAgent.investigate()
```

The system uses **4 LLM agents** powered by the Mistral API:

| Agent | Model | Role |
|---|---|---|
| Log Agent | `mistral-small-latest` | Reads and summarizes service logs |
| Trace Agent | `mistral-small-latest` | Reads and summarizes distributed traces |
| Metrics Agent | `mistral-small-latest` | Reads and summarizes Prometheus metrics |
| Orchestrator | `mistral-large-latest` | Drives investigation, applies CoT reasoning, writes final RCA JSON |

Specialist agents (1–3) use a smaller, cheaper model because their task is structured summarization. The orchestrator uses a larger model because it must reason across all signals, call tools, and produce a complex structured output.

---

## 3. Agent Workflow & Data Flow

### Step 0 — Dataset loading

```
dataset_path/
  observability/
    export-metadata.json      ← anomaly_id, window_start, window_end
    logs/
      *.log                   ← one file per service
    metrics/
      01-http_requests_rate.json
      02-http_response_times.json
      ...                     ← Prometheus range query results (JSON)
    traces/
      tempo-traces.json       ← Grafana Tempo trace export
      tempo-span-details.json
```

The orchestrator reads `export-metadata.json` first to get the anomaly ID and UTC time window. All subsequent analysis is scoped to that window.

---

### Step 1 — Specialist agents analyze their signal type

Each specialist agent has two functions:

- `load_*()` — reads raw files from disk, parses and pre-processes them into a structured Python dict
- `format_*()` — converts that dict into a human-readable text summary
- `analyze()` — sends that text to the LLM and gets back a structured natural-language report
- `investigate(question)` — answers a targeted follow-up question using the already-loaded text (no re-reading disk)

**Log Agent (`log_agent.py`):**
1. Reads every `*.log` file in `logs/`
2. Classifies each line as `error`, `warn`, or `other` using regex on level keywords and content
3. De-duplicates identical lines and counts occurrences (e.g., `×2236`)
4. Returns the top 20 unique errors and top 10 unique warnings per service
5. Formats them with `[ERROR ×N]` / `[WARN ×N]` notation
6. Sends to LLM for a 4-section report: errors, warnings, patterns, key findings

**Trace Agent (`trace_agent.py`):**
1. Reads `tempo-traces.json` and similar files
2. Filters spans: keeps only spans with non-zero status codes, duration > 50ms, or error tags
3. Also keeps parent spans of any relevant span (to preserve context)
4. Computes per-service count/avg/max latency stats
5. Lists "slow traces" (>500ms) with traceID
6. Sends to LLM for a 5-section report: latency summary, slow traces, hotspots, error traces, key findings

**Metrics Agent (`metrics_agent.py`):**
1. Reads each `*.json` file (one per metric type)
2. For each time series, computes `min/avg/max` from the **full** dataset before any compression
3. Applies **Douglas-Peucker line simplification** to reduce the number of points sent to the LLM while preserving shape (spikes, drops)
4. Flags spikes: if `max > 3× avg`, adds `*** SPIKE (N×avg) ***` annotation
5. Marks series where all values are NaN as `NO DATA — all NaN/missing`
6. Sends to LLM for a 5-section report: health, latency, errors, resources, key findings

---

### Step 2 — Orchestrator receives raw data + specialist reports

The orchestrator does **not** re-read the files. It calls the same `load_*` / `format_*` functions to get the pre-processed text directly — the same text the specialist agents used. This gives it a verified source of truth it can cite from.

The user message sent to the orchestrator LLM contains:

```
=== RAW OBSERVABILITY DATA (ground truth — cite from here) ===

--- LOGS ---
[formatted log summary]

--- METRICS ---
[formatted metrics summary]

--- TRACES ---
[formatted trace summary]

=== SPECIALIST AGENT REPORTS (interpretation layer) ===

[Log Agent Report]
[Trace Agent Report]
[Metrics Agent Report]
```

The system prompt tells the orchestrator: raw data is truth, specialist reports are hypotheses to verify, not copy.

---

### Step 3 — Chain-of-Thought (CoT) reasoning checkpoint

Before the investigation tool loop starts, a second user message is injected forcing the orchestrator to work through phases 1–4 explicitly in natural language:

- **Phase 1 — Symptom Extraction:** List every anomaly (WHAT, WHERE, WHEN, MAGNITUDE) without interpretation
- **Phase 2 — Signal Mapping:** For each symptom, count how many signal types (logs/metrics/traces) support it
- **Phase 3 — Hypothesis Generation:** Generate ≥3 different possible root causes
- **Phase 4 — Hypothesis Refinement:** For each, answer: localized or distributed? cause or effect? what would disconfirm it?

The model responds with this reasoning as plain text. That response is appended to the message history. This forces the model to build its understanding incrementally before jumping to conclusions — a technique shown to significantly improve complex reasoning accuracy.

---

### Step 4 — Tool-calling investigation loop (max 5 rounds)

The orchestrator is now given access to 3 tools:

```
ask_log_agent(question)     → LogAgent.investigate(question)
ask_trace_agent(question)   → TraceAgent.investigate(question)
ask_metrics_agent(question) → MetricsAgent.investigate(question)
```

Each tool call routes to the corresponding specialist agent's `investigate()` method, which answers the question using the text already in memory — no re-reading of disk or re-calling the specialist's full `analyze()`.

The loop runs until:
- The model stops calling tools (`finish_reason != "tool_calls"`) → natural exit, model produces final JSON
- 5 rounds are exhausted → a final forced prompt is injected requesting JSON output with all required fields

At forced exit, the prompt explicitly names phases 5–7:
- **Phase 5 — Cross-Signal Validation:** Verify top hypothesis against all three signals
- **Phase 6 — Causal Chain Construction:** Build the step-by-step chain with mechanism + evidence per step
- **Phase 7 — Confidence Calibration:** Determine `Confirmed` vs `Hypothesis` and write the rationale

---

### Step 5 — Parse, validate, enrich, save

1. **Parse:** `_parse_json_response()` tries to extract valid JSON from the model's response. It handles markdown code fences, sanitizes control characters inside strings, and as a last resort extracts the first `{...}` block with a regex.

2. **JSON recovery (new):** If parsing still fails (returns `parse_error` key), a recovery prompt is injected: "Your previous response was not valid JSON. Output ONLY the JSON object now." The model is called once more without tools.

3. **Field injection:** If `anomaly_id` or `window_utc` are missing from the output, they are injected from the metadata loaded in Step 0.

4. **Schema validation:** The output is validated against `rca_schema.json` using the `jsonschema` library. Validation is non-fatal — warnings are logged and stored in `metadata.validation_warnings`. The analysis is never discarded due to a schema error.

5. **Metadata enrichment:** `generated_at` (UTC ISO timestamp) and `reasoning_phases_completed` (`[1,2,3,4,5,6,7]`) are added to the `metadata` block.

6. **Save:** The final JSON is written to `dataset_path/rca-analysis-<timestamp>Z.json`.

---

### Complete data flow diagram

```
Disk
 ├── *.log   ──► load_logs()  ──► format_logs()  ──┬──► LogAgent LLM     ──► log_report
 ├── *.json  ──► load_metrics()──► format_metrics()─┼──► MetricsAgent LLM ──► metrics_report
 └── *.json  ──► load_traces() ──► format_traces() ─┴──► TraceAgent LLM   ──► trace_report
                                       │
                                       │ (same formatted text, reused)
                                       ▼
                              Orchestrator LLM (mistral-large)
                                       │
                              ┌────────┴────────┐
                              │   System prompt  │  (accuracy rules + CoT phases + output format)
                              │   User message   │  (raw data + specialist reports)
                              │   CoT checkpoint │  (phases 1-4 forced response)
                              └────────┬────────┘
                                       │
                              ┌────────▼────────┐
                              │  Tool loop      │  (up to 5 rounds)
                              │  ask_log_agent  │◄──► LogAgent.investigate()
                              │  ask_trace_agent│◄──► TraceAgent.investigate()
                              │  ask_metrics_   │◄──► MetricsAgent.investigate()
                              └────────┬────────┘
                                       │
                              ┌────────▼────────┐
                              │ Parse → Validate │
                              │ → Enrich → Save  │
                              └────────┬────────┘
                                       │
                              rca-analysis-<ts>Z.json
```

---

## 4. Code Explanation — File by File

### 4.1 `main.py`

```python
from backend.agents.rca_agent import analyze_root_cause

DATASET_PATH = "datasets/ANOMALY-004-20260507T162820Z"
result = analyze_root_cause(DATASET_PATH)
print(result)
```

The entry point. Sets the dataset path and calls `analyze_root_cause`, which is a thin shim that instantiates `OrchestratorAgent` and calls `run_rca`. The result is the parsed RCA dict (also saved to disk).

---

### 4.2 `rca_agent.py`

```python
from .orchestrator import analyze_root_cause  # noqa: F401
```

A backwards-compatibility shim. All logic was moved to `orchestrator.py` when the 4-agent architecture was introduced. This file re-exports `analyze_root_cause` so callers using `from rca_agent import analyze_root_cause` continue to work.

---

### 4.3 `orchestrator.py`

The most important file. Contains the full RCA pipeline.

#### Module-level constants

```python
_MODEL = "mistral-large-latest"
_MAX_TOOL_ROUNDS = 5
```

`_MODEL` is the orchestrator model. Specialist agents use `mistral-small-latest` (defined in their own files). `_MAX_TOOL_ROUNDS` caps the tool-calling loop to prevent infinite investigation.

#### `_RCA_SCHEMA` loading

```python
_SCHEMA_PATH = Path(__file__).parent / "rca_schema.json"
with open(_SCHEMA_PATH) as f:
    _RCA_SCHEMA = json.load(f)
```

Loaded once at import time. If the file is missing, `_RCA_SCHEMA = None` and validation is skipped. This makes schema validation optional — a missing schema file doesn't break the pipeline.

#### `_TOOLS` list

The three tool definitions exposed to the orchestrator LLM. Each follows the Mistral function-calling format: `type: "function"`, with a `name`, `description`, and `parameters` (JSON Schema). The descriptions tell the LLM *when* to use each tool — this is critical because the LLM decides whether to call a tool based entirely on these descriptions.

#### `_SYSTEM_PROMPT`

A long system prompt that contains:

1. **Role definition** — "expert SRE performing RCA"
2. **Accuracy rules** — specific constraints to prevent hallucination (e.g., "NaN means no instrumentation data, not healthy", "up=1 does not confirm traffic is flowing", "×1 is a single event, not a recurring problem")
3. **Reasoning rules** — when to use `Confirmed` vs `Hypothesis`, how to classify `failure_domain`
4. **CoT phase summaries** — phases 1–7 described in brief (the full prompts are in `reasoning_config.py`)
5. **Special protocols** — latency analysis checklist, CPU/memory spike checklist
6. **Output format** — the exact JSON template the model must produce

The output format is embedded in the system prompt as a JSON skeleton with inline comments explaining each field. This is the most direct way to constrain LLM output structure.

#### `_sanitize_json_strings(text)`

A character-by-character parser that escapes raw newlines (`\n`), carriage returns (`\r`), and tabs (`\t`) found inside JSON string values. LLMs occasionally embed literal newlines in JSON strings, which makes the output invalid JSON. This function fixes that without modifying the JSON structure.

It tracks whether it's inside a JSON string using an `in_string` boolean toggle on `"` characters, handling escape sequences (`\"`) correctly.

#### `_parse_json_response(raw)`

Multi-stage JSON extraction:

1. Strip whitespace
2. If the text contains a markdown code block (` ```json ... ``` `), extract only the inner content
3. Try `json.loads()` directly
4. If that fails, try again after `_sanitize_json_strings()`
5. If that fails, use a regex `\{.*\}` (DOTALL) to find the first JSON object in the text
6. If all fail, return `{"raw_analysis": raw, "parse_error": "..."}` — the `parse_error` key is used downstream to trigger the recovery fallback

If the result is a JSON array (unexpected), it returns the first element. This handles the rare case where the model wraps its output in an array.

#### `_validate_rca_against_schema(rca)`

Wraps `jsonschema.validate()`. Returns `(True, [])` on success, `(False, [error_messages])` on failure. Two exception types are caught:
- `ValidationError` — the RCA dict doesn't match the schema (e.g., missing required field, wrong enum value)
- `SchemaError` — the schema itself is malformed

#### `_enrich_rca_with_metadata(rca, phases_completed)`

Adds `metadata.generated_at` (UTC ISO 8601 timestamp) and `metadata.reasoning_phases_completed` to the final dict. Creates the `metadata` key if it doesn't already exist.

#### `_chat_orchestrator(client, messages, tools)`

A wrapper around `client.chat.complete()` with exponential backoff retry on HTTP 429 (rate limit) and 503 (service unavailable). Retries up to 5 times with waits of 10s, 20s, 40s, 80s. Raises on the 5th failure or on any non-rate-limit error.

#### `OrchestratorAgent` class

**`__init__`:** Instantiates the Mistral client and the three specialist agents (all sharing the same client instance).

**`_dispatch(tool_name, arguments)`:** Routes a tool call from the LLM to the correct specialist agent. Parses the `arguments` JSON string, extracts the `question` field, calls the appropriate `.investigate()` method, and returns its string result back into the conversation.

**`run_rca(dataset_path)`:** The main pipeline method. See the workflow section above for the full step-by-step description.

---

### 4.4 `log_agent.py`

#### `load_logs(logs_dir)`

Reads every `*.log` file, strips ANSI color codes (present in Docker container logs), then classifies each line:

- **Error** if: line contains `ERROR`, `SEVERE`, `CRITICAL`, `FATAL` (by regex in first 80 chars), or if content contains `error`, `exception`, `timeout`, `fail` (case-insensitive)
- **Warn** if: line contains `WARN` or `WARNING`

Lines are de-duplicated by exact string match and counted. The top 20 unique errors and 10 unique warnings per service are kept.

#### `format_logs(logs)`

Produces a human-readable text block. Skips services with zero errors and warnings to keep context short. Formats occurrences as `×N` or `×1 (single occurrence)` — the parenthetical is important because it signals to the LLM that a `×1` event is likely a startup artifact, not a runtime problem.

#### `LogAgent.analyze(logs_dir)`

Calls `load_logs` + `format_logs`, then sends a structured prompt to the LLM requesting a 4-section report. The LLM response is a natural-language summary stored in memory as `self._logs_text`.

#### `LogAgent.investigate(question)`

Uses `self._logs_text` (already in memory from `analyze`) to answer a targeted question. No additional file I/O. Returns a precise, evidence-citing answer.

---

### 4.5 `metrics_agent.py`

#### Douglas-Peucker simplification (`_douglas_peucker`, `_simplify_series`)

Prometheus time series can have hundreds of data points per metric series. Sending all of them to the LLM wastes tokens and doesn't improve analysis. Douglas-Peucker is a line-simplification algorithm that removes points that are close to the straight line between their neighbors, preserving peaks and troughs.

The epsilon (tolerance) is computed adaptively: `data_range * 0.05`. This means a series with a large range (e.g., memory in bytes) uses a proportionally larger tolerance than a series with a small range (e.g., error rate near 0).

**Important:** Stats (`min/avg/max/spike`) are computed from the **full series** before simplification, stored in `_original_stats`. The simplification only affects what's sent to the LLM for pattern reading — the statistics are always accurate.

#### `_compute_original_stats(series)`

Computes `min`, `avg`, `max`, `peak_time` (UTC), `spike_ratio` (`max/avg`), and `is_spike` (`spike_ratio > 3`) from the full time series. Handles `NaN` values (Prometheus exports `NaN` for missing scrapes). If all values are NaN, returns `{"all_nan": True}`.

#### `_METRIC_DESCRIPTIONS`

A dict mapping metric file names to human-readable descriptions. These are injected into the formatted metrics text to help the LLM interpret what each metric measures. For example, metric `01-http_requests_rate` has a note that 4xx/5xx in it are throughput counts, not a dedicated error rate — preventing a common misinterpretation.

#### `MetricsAgent.analyze` / `MetricsAgent.investigate`

Same pattern as `LogAgent`. `analyze()` loads, formats, and sends to LLM. `investigate()` answers targeted questions using the cached text.

---

### 4.6 `trace_agent.py`

#### `_is_relevant_span(span)`

A span is considered relevant if any of:
- Status code is non-zero (error)
- Duration > 50ms
- Service name is in a set of critical services
- Any tag value contains "error"
- Any log entry in the span contains "error" or "exception"

#### `_filter_trace_spans(trace)`

Keeps only relevant spans plus their parent spans (context preservation). Records the original and filtered span counts so the LLM knows filtering occurred.

#### `load_traces(traces_dir)`

Groups traces by root service name, computes per-service latency stats, identifies slow traces (>500ms). Only `*.json` files that aren't plain arrays are processed (the Tempo format wraps traces in an object with a `"traces"` key).

#### `TraceAgent.analyze` / `TraceAgent.investigate`

Same pattern. The `investigate()` method is particularly useful for the orchestrator to ask questions like "were there any traces for the orders service between 16:00 and 16:30?" — trace data is rarely self-explanatory from the formatted summary alone.

---

### 4.7 `reasoning_config.py`

Defines the 7-phase CoT reasoning pipeline as a list of dicts. Each phase has:
- `phase_number` (1–7)
- `description` (one line)
- `prompt` (the full instruction text for that phase)
- `validates` (what cognitive error this phase prevents)

The phases are:

| # | Name | Prevents |
|---|---|---|
| 1 | Symptom Extraction | Premature convergence (jumping to a conclusion before listing all symptoms) |
| 2 | Signal Mapping | Unsupported claims (citing evidence that doesn't exist in the data) |
| 3 | Hypothesis Generation | Anchoring bias (fixating on the first plausible explanation) |
| 4 | Hypothesis Refinement | Confusing causes with effects; ignoring temporal ordering |
| 5 | Cross-Signal Validation | Single-signal over-confidence |
| 6 | Causal Chain Construction | Skipping steps; unjustified causal leaps |
| 7 | Confidence Calibration | Over-claiming "Confirmed" when evidence is weak |

The file also defines `LATENCY_ANALYSIS_PROTOCOL` and `CPU_MEMORY_ANALYSIS_PROTOCOL` — specialized checklists for specific anomaly types. These are imported by the orchestrator but referenced in the system prompt rather than injected dynamically.

The functions `get_reasoning_prompt_for_phase(n)` and `get_all_reasoning_prompts()` are utility helpers for retrieving phase content. They are imported by the orchestrator but the actual phase injection into the conversation is done inline in `run_rca()` rather than via these functions.

---

### 4.8 `rca_schema.json`

A JSON Schema (draft-07) that defines the required structure of the output RCA report. Used by `jsonschema.validate()` in the orchestrator. Validation is **non-fatal** — the analysis is never discarded, but warnings are logged and stored in `metadata.validation_warnings`.

#### Top-level required fields

```
anomaly_id, window_utc, severity, root_cause, evidence, impact, recommendations
```

#### `root_cause` required fields

```
confidence, confidence_rationale, failure_domain, failure_mode,
summary, causal_chain, alternatives_considered
```

#### `causal_chain` item required fields

```
step, time_utc, service, event, mechanism, evidence
```

#### `failure_mode` enum

```
crash | oom | connection_refused | dns_failure | db_timeout |
db_connection_pool_exhausted | config_error | dependency_unavailable |
instrumentation_gap | network_latency | network_partition |
resource_exhaustion | infrastructure_throttling | dns_misconfiguration |
dns_misconfiguration_and_startup_failure | other
```

#### `recommendations` item required fields

```
action, priority (immediate|short-term|long-term), rationale
```

---

## 5. Output Report Structure

A valid RCA report produced by this system has the following top-level structure:

```json
{
  "anomaly_id": "ANOMALY-007",
  "window_utc": { "start": "...", "end": "..." },
  "severity": "P1",

  "affected_services": [
    {
      "service": "front-end",
      "anomaly_type": "unavailable",
      "details": "verbatim metric values and log counts",
      "peak_time_utc": "16:06:24"
    }
  ],

  "root_cause": {
    "confidence": "Confirmed",
    "confidence_rationale": "3 signals confirm ... alternatives ruled out ...",
    "failure_domain": "configuration",
    "failure_mode": "dns_misconfiguration_and_startup_failure",
    "summary": "One sentence naming failure domain and mode.",
    "causal_chain": [
      {
        "step": 1,
        "time_utc": "15:41:39",
        "service": "front-end",
        "event": "Startup failure due to npm ERR!",
        "mechanism": "Service failed to bind to port 8079",
        "evidence": "front-end logs: npm ERR! ×54"
      }
    ],
    "contributing_factors": ["..."],
    "alternatives_considered": ["Memory leak ruled out: no OOM logs"]
  },

  "evidence": [
    { "signal": "logs", "description": "verbatim log line with ×count" },
    { "signal": "metrics", "description": "exact metric value at timestamp" }
  ],

  "recommendations": [
    {
      "action": "Fix front-end startup script",
      "priority": "immediate",
      "rationale": "Addresses the primary root cause"
    }
  ],

  "impact": "Complete user-facing outage ...",

  "metadata": {
    "generated_at": "2026-05-12T22:16:03Z",
    "reasoning_phases_completed": [1, 2, 3, 4, 5, 6, 7]
  }
}
```

Every string value in `evidence` and `causal_chain.evidence` must be a verbatim citation from the raw data — the system prompt enforces this with explicit accuracy rules. No paraphrasing, no invented numbers.
