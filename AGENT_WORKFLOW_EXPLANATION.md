# Agent Interaction & Workflow Explanation

## Overview

Your RCA (Root Cause Analysis) system uses a **4-tier multi-agent architecture** where specialized agents work together through a coordinated investigation process. This document explains how they interact, the workflow sequence, and how data flows through the system.

## System Architecture

The system consists of **4 different types of agents** that work in a specific sequence:

### Agent Tiers

```
TIER 1-3: SPECIALIST AGENTS (Independent, Parallel Processing)
├── LogAgent    - Analyzes application and system logs
├── TraceAgent  - Analyzes distributed trace spans
└── MetricsAgent - Analyzes Prometheus metrics

TIER 4: ORCHESTRATOR AGENT (Coordinator & Decision Maker)
└── Orchestrator - Reads specialist outputs, asks follow-up questions, produces final RCA
```

---

## How Agents Interact - Complete Data Flow

### Stage 1: Raw Data Loading (No Agent Interaction)

The workflow begins with observability data already collected in the dataset directory:

```
datasets/001-20260506T180913Z/
├── observability/
│   ├── logs/
│   │   └── *.log files (application error logs)
│   ├── metrics/
│   │   ├── 01-http_requests_rate.json
│   │   ├── 02-http_response_times.json
│   │   ├── 03-container_memory.json
│   │   ├── 04-container_cpu.json
│   │   ├── 05-service_health.json
│   │   ├── 06-http_errors.json
│   │   ├── 07-java_http_2xx_health.json
│   │   └── 08-java_http_4xx.json
│   ├── traces/
│   │   ├── tempo-traces.json
│   │   └── tempo-span-details.json
│   └── export-metadata.json (anomaly window: start/end times in UTC)
```

**At this stage:** No agents are running yet. Raw files sit in the file system waiting to be processed.

---

### Stage 2: Specialist Agents Run Independently (No Interaction)

When `analyze_root_cause(dataset_path)` is called, an `OrchestratorAgent` is created, which instantiates three specialist agents:

#### 2.1 LogAgent Execution

**What it does:**
- Reads all `.log` files from the `observability/logs/` directory
- Removes ANSI color codes (terminal formatting)
- Classifies each line by severity level (ERROR, WARN, INFO, DEBUG)
- Deduplicates identical log lines and counts occurrences
- Extracts top 20 unique errors and top 10 unique warnings

**Input to LogAgent:**
```
/observability/logs/
├── frontend.log
├── catalogue.log
├── orders.log
├── shipping.log
└── api-gateway.log
```

**Processing:**
```
For each log file:
  1. Read all lines
  2. Strip ANSI escape sequences
  3. Identify log level (ERROR/WARN/INFO)
  4. Group identical lines
  5. Count occurrences
  6. Sort by frequency
  7. Keep top entries
```

**Output from LogAgent (plain text report):**
```
[frontend] — 487 errors, 32 warnings (5000 total lines)
  [ERROR ×487] Connection refused on orders service
  [ERROR ×156] Timeout waiting for backend response
  [WARN  ×32] Request queue size exceeded threshold

[catalogue] — 0 errors, 15 warnings (2500 total lines)
  [WARN  ×15] High latency detected in database queries

[orders] — 0 errors, 0 warnings (1200 total lines)

[shipping] — 0 errors, 0 warnings (1100 total lines)
```

**Key Point:** LogAgent does NOT know about metrics or traces. It only knows about logs.

---

#### 2.2 TraceAgent Execution

**What it does:**
- Reads distributed trace JSON files (from Tempo)
- Filters out irrelevant spans (success=true, fast duration, non-critical services)
- Keeps spans that have errors, slow latencies (>50ms), or are from critical services
- Groups traces by root service name
- Calculates latency statistics: count, average, max
- Identifies slow traces (>500ms duration)

**Input to TraceAgent:**
```
/observability/traces/
├── tempo-traces.json (list of traces with service flow data)
└── tempo-span-details.json (individual span details)
```

**Processing:**
```
For each trace file:
  1. Parse JSON
  2. Filter spans (keep only error/slow/critical)
  3. Group by root service
  4. Calculate statistics
  5. Find slow traces (>500ms)
  6. Correlate with services
```

**Output from TraceAgent (plain text report):**
```
[tempo-traces.json] (filtered from 500 traces to 120 relevant)

Service latency stats:
  frontend:  count=45  avg=120ms  max=2400ms
  catalogue: count=35  avg=80ms   max=1800ms
  orders:    count=0   avg=N/A    max=N/A
  shipping:  count=0   avg=N/A    max=N/A

Slow traces (>500ms):
  - frontend GET /checkout: 2400ms (8 spans, traceID=abc123)
  - catalogue GET /products: 1800ms (5 spans, traceID=xyz789)
```

**Key Point:** TraceAgent does NOT know about logs or metrics. It only knows about traces.

---

#### 2.3 MetricsAgent Execution

**What it does:**
- Reads Prometheus metrics JSON files
- Parses each metric series (label=value pairs)
- Identifies spikes: compares max value to 3× average (3× = significant spike)
- Detects NaN series (missing instrumentation data)
- Simplifies series data using Douglas-Peucker algorithm (reduces noise)
- Formats readable output with spike markers

**Input to MetricsAgent:**
```
/observability/metrics/
├── 01-http_requests_rate.json     (req/s by service)
├── 02-http_response_times.json    (latency in seconds)
├── 03-container_memory.json       (memory bytes)
├── 04-container_cpu.json          (CPU cores)
├── 05-service_health.json         (up=1, down=0)
├── 06-http_errors.json            (5xx error rate)
├── 07-java_http_2xx_health.json   (2xx success rate)
└── 08-java_http_4xx.json          (4xx rate)
```

**Processing per metric file:**
```
For each metric series:
  1. Extract timestamps and values
  2. Calculate min, avg, max
  3. Identify spike points (max > 3×avg)
  4. Check for NaN values
  5. Simplify series (remove noise)
  6. Format human-readable output
```

**Output from MetricsAgent (plain text report):**
```
[01-http_requests_rate]
  frontend: min=1.2 avg=2.5 max=3.8 req/s | peak=18:15:28 UTC | (no spike)
  catalogue: min=0.8 avg=1.2 max=1.9 req/s | peak=18:12:15 UTC | *** SPIKE (1.6×avg) ***
  orders: min=0.0 avg=0.0 max=0.0 req/s | peak=N/A | NO DATA — all NaN/missing
  shipping: min=0.0 avg=0.0 max=0.0 req/s | peak=N/A | NO DATA — all NaN/missing

[02-http_response_times]
  frontend: min=0.005s avg=0.015s max=0.08s | peak=18:15:28 UTC | (no spike)
  catalogue: min=0.02s avg=0.032s max=0.139s | peak=18:10:43 UTC | *** SPIKE (4.4×avg) ***
  orders: all NaN | peak=N/A | NO DATA — no response times recorded
  shipping: all NaN | peak=N/A | NO DATA — no response times recorded

[06-http_errors]
  frontend: min=0.1 avg=0.286 max=0.804 errors/s | peak=18:15:28 UTC | *** SPIKE (2.8×avg) ***

[07-java_http_2xx_health]
  orders: min=0.0 avg=0.0 max=0.0 | NO DATA — zero success rate (no requests)
  shipping: min=0.0 avg=0.0 max=0.0 | NO DATA — zero success rate (no requests)
```

**Key Point:** MetricsAgent does NOT know about logs or traces. It only knows about metrics.

---

### Stage 3: Specialist Reports Are Combined (First Agent Interaction Point)

After all three specialist agents have completed their independent analysis, the **OrchestratorAgent** receives:

1. **Raw data summaries** (formatted text from logs, traces, metrics)
2. **Specialist reports** (their interpretations)
3. **Metadata** (anomaly ID, analysis window start/end times)

**The Orchestrator now has:**
```
System Message:
├── Instructions for RCA production
├── Accuracy rules (timestamps, evidence citation requirements)
├── Reasoning rules (correlation requirements for "Confirmed" vs "Hypothesis")
├── Output JSON schema

User Message:
├── RAW LOGS TEXT
│   └── [verbatim formatted log summary from LogAgent.analyze()]
├── RAW METRICS TEXT
│   └── [verbatim formatted metrics summary from MetricsAgent.analyze()]
├── RAW TRACES TEXT
│   └── [verbatim formatted trace summary from TraceAgent.analyze()]
├── SPECIALIST REPORTS
│   ├── [Log Agent Report - their interpretation]
│   ├── [Trace Agent Report - their interpretation]
│   └── [Metrics Agent Report - their interpretation]
└── Metadata
    ├── Anomaly ID: 001
    ├── Window: 2026-05-06T17:54:13Z → 2026-05-06T18:51:49Z
    └── Request to investigate and produce RCA
```

**Orchestrator's First Task:**
The Orchestrator reads ALL this information and performs initial analysis:
- Compares specialist reports against RAW DATA
- Identifies which claims from specialists are supported by raw data
- Spots contradictions or ambiguities
- Determines what questions need deeper investigation

---

### Stage 4: Tool-Calling Investigation Loop (Active Agent Interaction)

This is where the agents INTERACT with each other through a **tool-calling mechanism**.

#### How Tool Calling Works

The Orchestrator doesn't just accept specialist reports - it can ask them targeted follow-up questions. The mechanism:

**Orchestrator's Available Tools:**
```
1. ask_log_agent(question: string)
   └─ Ask LogAgent to investigate a specific question about logs

2. ask_trace_agent(question: string)
   └─ Ask TraceAgent to investigate a specific question about traces

3. ask_metrics_agent(question: string)
   └─ Ask MetricsAgent to investigate a specific question about metrics
```

#### Investigation Round Example

**Initial Observation by Orchestrator:**
```
From raw data I see:
- Frontend has 487 ERROR logs about "connection refused on orders service"
- Metrics show orders service with 0 req/s (no traffic)
- But are there ANY traces for orders service?

This could mean:
A) Orders service is truly down
B) Orders service is handling traffic but metrics are broken

Need to ask TraceAgent to clarify.
```

**Orchestrator makes a tool call:**
```json
{
  "tool": "ask_trace_agent",
  "arguments": {
    "question": "Do the tempo traces contain any spans for the 'orders' service during the anomaly window (2026-05-06T17:54:13Z to 18:51:49Z)? If yes, how many traces and what latencies?"
  }
}
```

**TraceAgent investigates and responds:**
```
Searched tempo-traces.json for spans with localEndpoint.serviceName = "orders" 
during anomaly window.

Result: ZERO spans found for orders service.

Conclusion: Orders service received NO distributed requests during the anomaly window.
This is consistent with metrics showing 0 req/s.

The "connection refused" errors in frontend logs are the frontend failing to connect 
to orders, not orders service failures.
```

**Orchestrator now understands:**
```
✓ Orders service: truly unavailable (no requests in metrics, no traces)
✓ Frontend errors: are connection failures trying to reach orders
✓ Root cause hypothesis: orders service not accepting connections

Next question: Why?
```

#### Second Investigation Round Example

**Orchestrator needs to understand the cascade:**
```
I see:
- Orders/shipping services: 0 traffic (unavailable)
- Frontend: 5xx errors (trying to reach them)
- Catalogue: latency spike (4.4× normal)

Does catalogue latency CAUSE the frontend errors, or is it a secondary effect?
Need to understand timing and causality.
```

**Orchestrator makes another tool call:**
```json
{
  "tool": "ask_metrics_agent",
  "arguments": {
    "question": "What is the exact UTC timestamp when catalogue latency first exceeded 3× average? Compare this to when frontend 5xx errors started. Which came first?"
  }
}
```

**MetricsAgent responds:**
```
Analysed metric data:

Catalogue latency spike (4.4×avg):
  Start: 2026-05-06T18:10:43Z
  Peak: 2026-05-06T18:10:43Z
  Value: 0.13941s (avg baseline: 0.03172s)

Frontend 5xx error spike:
  Start: 2026-05-06T18:15:28Z
  Peak: 2026-05-06T18:15:28Z
  Value: 0.80431 errors/s

Timeline: Catalogue latency spike happened ~5 minutes BEFORE frontend error spike.

Likely: Catalogue latency is a secondary effect, not the cause of frontend errors.
```

**Orchestrator refines understanding:**
```
Timeline established:
1. 18:10:43 UTC: Catalogue experiences latency (why? needs investigation)
2. 18:15:28 UTC: Frontend starts getting 5xx errors from orders/shipping

Hypothesis: Orders/shipping became unavailable first, causing:
  - Frontend to retry, get errors (18:15:28)
  - Catalogue to experience latency from retries (18:10:43 contradicts this)

Actually, let me re-examine. Maybe catalogue latency IS independent.
```

#### Maximum Tool Calls

The investigation loop runs for **maximum 5 rounds**. Each round:
1. Orchestrator asks 1-3 targeted questions via tool calls
2. Each specialist agent investigates their data and responds
3. Orchestrator integrates responses into understanding
4. Loop continues or ends after 5 rounds

**Loop termination conditions:**
- Orchestrator has sufficient evidence for root cause
- 5 rounds completed
- All ambiguities resolved

---

### Stage 5: Final RCA Production

After investigation loop completes, the Orchestrator produces the **final RCA JSON** with:

```json
{
  "anomaly_id": "001",
  "window_utc": {
    "start": "2026-05-06T17:54:13Z",
    "end": "2026-05-06T18:51:49Z"
  },
  "affected_services": [
    {
      "service": "frontend",
      "anomaly_type": "error_rate",
      "details": "HTTP 5xx spike to 0.804 errors/s at 18:15:28 UTC",
      "peak_time_utc": "18:15:28"
    }
  ],
  "root_cause": {
    "confidence": "Confirmed",
    "primary_service": "orders",
    "failure_mode": "unavailable",
    "summary": "orders and shipping services were unreachable, causing frontend errors",
    "causal_chain": [
      {
        "step": 1,
        "time_utc": "18:10:00",
        "service": "orders",
        "event": "Received zero HTTP traffic during anomaly window",
        "evidence": "[01-http_requests_rate] metric=0.0 req/s for orders service"
      }
    ]
  }
}
```

**Key constraint:** Every evidence citation must come from RAW DATA or specialist responses. No fabricated numbers.

---

## Data Flow Visualization

### Information Flow Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                    OBSERVABILITY DATA FILES                      │
│              (logs, metrics, traces, metadata)                   │
└──────────────────────────────────────────────────────────────────┘
                    ↓    ↓    ↓
        ┌───────────┴────┴────┴───────────┐
        │                                 │
        ↓                ↓                ↓
   ┌─────────┐    ┌─────────┐      ┌──────────┐
   │ LogAgent│    │TraceAgent     │MetricsAgent
   └────┬────┘    └────┬────┘     └──────┬───┘
        │              │                  │
        ↓              ↓                  ↓
   ┌─────────┐    ┌─────────┐      ┌──────────┐
   │ Log Text│    │Trace Text     │Metrics Text
   │ Report  │    │ Report        │ Report
   └────┬────┘    └────┬────┘     └──────┬───┘
        │              │                  │
        └──────────────┬──────────────────┘
                       ↓
            ┌──────────────────────┐
            │ OrchestratorAgent    │
            │ (reads reports)      │
            └──────────┬───────────┘
                       ↓
            ┌──────────────────────┐
            │ Initial Analysis     │
            │ (spot ambiguities)   │
            └──────────┬───────────┘
                       ↓
            ┌──────────────────────┐
            │ Investigation Loop   │
            │ (max 5 rounds)       │
            └──────────┬───────────┘
                 ↙     ↓     ↖
        ┌─────────┐ ┌─────────┐ ┌──────────┐
        │ask_log_ │ │ask_trace│ │ask_metrics
        │agent()  │ │_agent() │ │_agent()
        └────┬────┘ └────┬────┘ └────┬─────┘
             │           │           │
             ├──────────┬┴───────────┤
             │          ↓            │
             │  [Investigation]     │
             │          ↓            │
             └──────────┬─────────────┘
                        ↓
            ┌──────────────────────┐
            │ Final RCA JSON       │
            │ (with evidence)      │
            └──────────────────────┘
```

### Message Flow Between Agents

```
SEQUENTIAL FLOW (No parallelism between agents):

Time 1: Orchestrator → LogAgent
        "Analyze logs from observability/logs/"
        
Time 2: LogAgent → Orchestrator
        "✓ Complete. Report: [487 frontend errors...]"

Time 3: Orchestrator → TraceAgent
        "Analyze traces from observability/traces/"
        
Time 4: TraceAgent → Orchestrator
        "✓ Complete. Report: [Frontend avg 120ms latency...]"

Time 5: Orchestrator → MetricsAgent
        "Analyze metrics from observability/metrics/"
        
Time 6: MetricsAgent → Orchestrator
        "✓ Complete. Report: [Frontend 5xx spike 2.8×...]"

Time 7: Orchestrator (internally)
        "Cross-checking reports against raw data..."

Time 8: Orchestrator → TraceAgent (tool call)
        "Question: Do you see any orders traces during window?"
        
Time 9: TraceAgent → Orchestrator
        "Checked: Zero orders traces found. Consistent with 0 req/s."

Time 10: Orchestrator → MetricsAgent (tool call)
         "Question: What time did catalogue latency spike vs frontend errors?"
         
Time 11: MetricsAgent → Orchestrator
         "Catalogue spike at 18:10:43, frontend errors at 18:15:28. 5 min gap."

Time 12: Orchestrator (internally)
         "Analysis complete. Producing final RCA JSON..."
         
Time 13: Orchestrator outputs
         "rca-analysis-20260509T230044Z.json"
```

---

## How Agents Don't Interact

### Critical Separation of Concerns

Agents **deliberately don't interact directly** in most of the workflow:

1. **LogAgent doesn't call MetricsAgent**
   - LogAgent only knows about logs
   - It cannot determine if a log pattern is significant without comparing to metrics
   - That's the Orchestrator's job

2. **TraceAgent doesn't call LogAgent**
   - TraceAgent only knows about traces
   - It cannot correlate trace latencies with log errors
   - Orchestrator does the correlation

3. **MetricsAgent doesn't call TraceAgent**
   - MetricsAgent only knows about metrics
   - It cannot verify if a metric spike matches a trace pattern
   - Orchestrator coordinates this verification

4. **Specialists don't produce JSON RCA**
   - Only the Orchestrator produces the final JSON
   - Specialists provide raw text reports and investigation answers
   - This ensures quality control and evidence tracking

---

## Workflow Summary

### Complete Execution Sequence

```
STEP 1: INITIALIZATION
├─ User calls: analyze_root_cause("datasets/001-20260506T180913Z")
├─ OrchestratorAgent created
├─ LogAgent, TraceAgent, MetricsAgent instantiated
└─ Metadata loaded (anomaly window, ID)

STEP 2: INDEPENDENT ANALYSIS (No interaction between specialists)
├─ LogAgent.analyze(logs_dir)
│  ├─ Read *.log files
│  ├─ Process lines, deduplicate, count
│  └─ Return text report
├─ TraceAgent.analyze(traces_dir)
│  ├─ Read JSON trace files
│  ├─ Filter, group, calculate stats
│  └─ Return text report
└─ MetricsAgent.analyze(metrics_dir)
   ├─ Read metric JSON files
   ├─ Calculate spikes, detect NaN
   └─ Return text report

STEP 3: ORCHESTRATOR READS RAW DATA & REPORTS
├─ Load raw logs text (same as specialist saw)
├─ Load raw traces text (same as specialist saw)
├─ Load raw metrics text (same as specialist saw)
├─ Load specialist interpretations
└─ Verify claims against raw data

STEP 4: INVESTIGATION LOOP (Max 5 rounds)
├─ Round 1:
│  ├─ Analyze for ambiguities
│  ├─ Ask targeted questions via tool calls
│  └─ Receive specialist responses
├─ Round 2-4:
│  └─ Continue investigating unclear points
└─ Round 5:
   ├─ Finalize hypothesis
   └─ Build causal chain

STEP 5: FINAL RCA PRODUCTION
├─ Synthesize findings into JSON
├─ Cite every claim with evidence
├─ Build causal chain with timestamps
├─ Evaluate confidence (Confirmed vs Hypothesis)
└─ Save rca-analysis-TIMESTAMP.json
```

---

## Key Interaction Patterns

### Pattern 1: Verification

**What happens:**
```
Specialist claim → Orchestrator checks raw data → Accept or question

Example:
  LogAgent says: "Frontend has 487 connection errors"
  Orchestrator checks: raw logs file says: "Connection refused ×487"
  Result: ✓ VERIFIED - claim is evidence-based
```

### Pattern 2: Cross-Signal Correlation

**What happens:**
```
Orchestrator spots signal from one specialist that needs verification from another

Example:
  MetricsAgent: "Orders service has 0 req/s"
  Orchestrator thinks: "But is this truly unavailable or just a metrics gap?"
  Orchestrator asks TraceAgent: "Any traces for orders service?"
  TraceAgent responds: "No traces found"
  Orchestrator concludes: ✓ CONFIRMED - service truly unavailable
```

### Pattern 3: Timeline Resolution

**What happens:**
```
Orchestrator needs to understand causality and timing

Example:
  Multiple services have anomalies
  Orchestrator needs to know: Which failed first?
  Orchestrator asks MetricsAgent: "Exact timestamps of each spike"
  MetricsAgent responds with precise UTC times
  Orchestrator builds: Step 1 → Step 2 → Step 3 causal chain
```

### Pattern 4: Ambiguity Resolution

**What happens:**
```
Specialist report contains possible interpretations

Example:
  MetricsAgent: "Catalogue service latency is 4.4× baseline"
  Unclear: Is this a PRIMARY cause or SECONDARY effect?
  Orchestrator asks: "When did catalogue latency start vs frontend errors?"
  MetricsAgent responds: "Catalogue latency BEFORE frontend errors"
  Orchestrator concludes: "Catalogue latency is secondary/coincidental"
```

---

## Data Transformation Through Pipeline

### Example: Frontend Error Investigation

```
RAW DATA (log files):
┌─────────────────────────────────────────────────┐
│ 2026-05-06T18:15:28.123Z [frontend] ERROR      │
│ Connection refused on orders service            │
│                                                 │
│ 2026-05-06T18:15:28.456Z [frontend] ERROR      │
│ Connection refused on orders service            │
│                                                 │
│ 2026-05-06T18:15:28.789Z [frontend] ERROR      │
│ Connection refused on orders service            │
│ ... (487 times total)                           │
└─────────────────────────────────────────────────┘
                    ↓
            [LogAgent processing]
                    ↓
AGENT 1 OUTPUT (Log Report):
┌─────────────────────────────────────────────────┐
│ [frontend] — 487 errors                         │
│   [ERROR ×487] Connection refused on orders     │
└─────────────────────────────────────────────────┘
                    ↓
            [Orchestrator reads]
                    ↓
RAW METRICS (metric files):
┌─────────────────────────────────────────────────┐
│ [01-http_requests_rate]                         │
│ frontend: 3.8 req/s                             │
│ orders: 0.0 req/s ← ZERO TRAFFIC                │
└─────────────────────────────────────────────────┘
                    ↓
            [MetricsAgent processing]
                    ↓
AGENT 3 OUTPUT (Metrics Report):
┌─────────────────────────────────────────────────┐
│ orders: min=0.0 avg=0.0 max=0.0 req/s          │
│ NO DATA — all NaN/missing                       │
└─────────────────────────────────────────────────┘
                    ↓
            [Orchestrator correlates]
                    ↓
ORCHESTRATOR INVESTIGATION:
┌─────────────────────────────────────────────────┐
│ Observation: Frontend has 487 connection       │
│ errors + Orders has zero traffic               │
│                                                 │
│ Question: Are there orders traces?             │
│ (asks TraceAgent)                               │
│                                                 │
│ Response: No orders traces found                │
│                                                 │
│ Conclusion: Orders service unreachable         │
│ Confidence: CONFIRMED (3 signals align)        │
└─────────────────────────────────────────────────┘
                    ↓
FINAL RCA (Evidence-based):
┌─────────────────────────────────────────────────┐
│ root_cause.primary_service: "orders"            │
│ root_cause.failure_mode: "unavailable"          │
│ evidence:                                       │
│   - logs: "487 connection errors"               │
│   - metrics: "0 req/s"                          │
│   - traces: "no spans found"                    │
│ causal_chain: [step showing orders unavailable]│
│ confidence: "Confirmed"                         │
└─────────────────────────────────────────────────┘
```

---

## Why This Architecture Works

### Advantages of the Multi-Agent System

1. **Specialization**
   - Each agent is expert in its signal type
   - LogAgent optimizes for log analysis
   - TraceAgent optimizes for distributed tracing
   - MetricsAgent optimizes for time-series analysis

2. **Parallel Efficiency**
   - Specialist agents can run in parallel (currently sequential for simplicity)
   - Each agent only processes its data type
   - No blocking between agents

3. **Verification**
   - Orchestrator verifies all claims against raw data
   - Cross-signal correlation prevents false conclusions
   - Requires multi-signal evidence for "Confirmed" confidence

4. **Explainability**
   - Every conclusion cites specific evidence
   - Causal chain shows exact steps
   - UTC timestamps for precision

5. **Iterative Investigation**
   - Tool-calling allows Orchestrator to ask follow-up questions
   - Can resolve ambiguities through targeted investigation
   - Prevents premature conclusions

6. **Quality Control**
   - Orchestrator acts as quality gate
   - Specialist reports are hypotheses, not conclusions
   - Final JSON is highest confidence output

---

## Communication Protocol

### How Agents Communicate

#### 1. Initial Analysis (Sequential)

```
OrchestratorAgent.run_rca(dataset_path)
    ├─ Print: "[Agent 1] Analyzing logs..."
    ├─ Call: log_agent.analyze(logs_dir)
    ├─ Receive: text report
    │
    ├─ Print: "[Agent 2] Analyzing traces..."
    ├─ Call: trace_agent.analyze(traces_dir)
    ├─ Receive: text report
    │
    ├─ Print: "[Agent 3] Analyzing metrics..."
    ├─ Call: metrics_agent.analyze(metrics_dir)
    └─ Receive: text report
```

#### 2. Orchestrator Processing

```
Combine all reports into system + user message
Send to LLM with tools available
```

#### 3. Tool-Calling Investigation

```
FOR up to 5 rounds:
    LLM response contains finish_reason = "tool_calls"
    ├─ FOR each tool_call:
    │   ├─ Extract: tool name, arguments
    │   ├─ Call: _dispatch(tool_name, arguments)
    │   │   ├─ ask_log_agent → log_agent.investigate(question)
    │   │   ├─ ask_trace_agent → trace_agent.investigate(question)
    │   │   └─ ask_metrics_agent → metrics_agent.investigate(question)
    │   └─ Add response to message history
    └─ Send updated messages back to LLM
    
WHEN loop ends:
    LLM response contains finish_reason ≠ "tool_calls"
    ├─ Extract final analysis text
    └─ Parse as JSON RCA
```

---

## Example Investigation Session

### Real-World Tool-Calling Example

**Setup:**
- Anomaly ID: 001
- Window: 2026-05-06 17:54:13Z to 18:51:49Z
- Raw data shows: Frontend errors + Orders unavailable

**LLM Initial Analysis:**
```
"I observe frontend 487 connection errors and orders service with zero 
traffic. This suggests orders is unavailable. However, metrics show zero 
values - could be an instrumentation gap.

Let me ask TraceAgent to clarify if orders is truly unavailable or if 
metrics are broken."
```

**Tool Call #1 (Round 1):**
```
LLM calls: ask_trace_agent({
  "question": "Search tempo-traces.json for ANY spans where 
   localEndpoint.serviceName contains 'orders' during the window 
   2026-05-06T17:54:13Z to 18:51:49Z. Report count and latencies."
})

TraceAgent responds:
"Searched tempo-traces.json for 'orders' service. Result: ZERO spans found 
during the anomaly window. This confirms orders service received NO 
distributed requests. The zero metrics are not an instrumentation gap - 
the service truly had no traffic."
```

**LLM Reasoning:**
```
Cross-signal confirmation:
- Metrics: orders = 0 req/s ✓
- Traces: zero spans ✓
- Logs: frontend tries to connect, gets refused ✓

All three signals align. Orders service was unavailable.
Next: understand why.
```

**Tool Call #2 (Round 2):**
```
LLM calls: ask_metrics_agent({
  "question": "For the 'orders' service, show the exact UTC timestamp 
   when request rate dropped to zero. Was it gradual (0.1 → 0.01 → 0.0) 
   or sudden (drop from 2.5 to 0)?"
})

MetricsAgent responds:
"Analyzed 01-http_requests_rate metric for orders service. Pattern: SUDDEN 
DROP from ~2.3 req/s to 0.0 req/s at 2026-05-06T18:10:00Z. No gradual 
decline - sharp cutoff suggesting service restart, crash, or network 
disconnection."
```

**LLM Refines Hypothesis:**
```
Timing: 18:10:00 UTC - orders drops to zero
Effect: 18:15:28 UTC - frontend starts getting connection errors

5-minute delay between outage start and error manifestation suggests:
- Orders became unreachable at 18:10:00
- Frontend retry logic exhausted at 18:15:28
- Connection pool timeout hit, errors logged

This is the root cause.
```

**Final Output:**
```json
{
  "root_cause": {
    "confidence": "Confirmed",
    "primary_service": "orders",
    "failure_mode": "unavailable",
    "summary": "orders service became unreachable at 18:10:00 UTC, causing 
               frontend connection errors 5 minutes later",
    "causal_chain": [
      {
        "step": 1,
        "time_utc": "18:10:00",
        "service": "orders",
        "event": "HTTP request rate dropped from 2.3 to 0 req/s (sudden)",
        "evidence": "[01-http_requests_rate] orders metric showed sharp drop"
      },
      {
        "step": 2,
        "time_utc": "18:10:00+",
        "service": "frontend",
        "event": "Retry attempts to connect to unreachable orders",
        "evidence": "[trace] zero orders spans during window, [logs] no 
                    orders connection handling"
      },
      {
        "step": 3,
        "time_utc": "18:15:28",
        "service": "frontend",
        "event": "Connection pool exhausted, 487 errors logged",
        "evidence": "[06-http_errors] frontend 5xx spike 0.804 errors/s, 
                    [logs] ×487 connection refused errors"
      }
    ]
  }
}
```

---

## Summary: Agent Interaction Model

### The 4-Agent System in One Paragraph

Three **specialist agents** (Log, Trace, Metrics) independently analyze their respective observability signals in parallel, each producing a formatted text report. These reports are combined with the raw data and fed to the **Orchestrator agent**, which reads both the raw data (as ground truth) and specialist interpretations (as hypotheses). The Orchestrator then enters a tool-calling investigation loop where it asks targeted follow-up questions to specialists through a standardized interface, integrating responses to resolve ambiguities and build cross-signal correlations. After up to 5 investigation rounds, the Orchestrator produces a final, evidence-based RCA JSON where every claim cites specific data points, and the confidence level (Confirmed vs Hypothesis) depends on whether multiple independent signals corroborate the conclusion.

