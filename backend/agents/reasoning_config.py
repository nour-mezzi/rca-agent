"""
Reasoning configuration for Chain-of-Thought (CoT) / Least-to-Most prompting.
Implements structured reasoning phases for improved RCA accuracy.

Based on research:
- Wei et al. (2022): CoT prompting improves complex reasoning
- Kojima et al. (2023): Zero-shot CoT effectiveness
- Suzgun et al. (2023): Structured prompts outperform unstructured
- Yao et al. (2023): Tree-of-Thought multi-path exploration
"""

# Seven-phase reasoning pipeline for RCA
REASONING_PHASES = [
    {
        "phase": "symptom_extraction",
        "phase_number": 1,
        "description": "Extract and list all symptoms",
        "prompt": """
PHASE 1: SYMPTOM EXTRACTION

Examine all three signals (logs, metrics, traces) and list EVERY anomaly you observe.
For each symptom, record:
- WHAT: Exact anomaly (e.g., "HTTP 5xx spike", "latency increase", "service unavailable")
- WHERE: Which service(s) affected?
- WHEN: UTC timestamp range (must be within anomaly window)
- MAGNITUDE: How severe? (e.g., "0.804 errors/s", "2400ms latency", "0 req/s")

Do NOT interpret or connect symptoms yet. Just list them.
Then, answer: "How many independent services show anomalies?"
""",
        "validates": "Ensures complete symptom identification without premature causality assumptions"
    },
    
    {
        "phase": "signal_mapping",
        "phase_number": 2,
        "description": "Map symptoms to evidence signals",
        "prompt": """
PHASE 2: SIGNAL MAPPING

For each symptom you listed in Phase 1, identify which RAW DATA supports it.

For symptom: [list from phase 1]
  - LOGS evidence: [exact log line or "none found"]
  - METRICS evidence: [exact metric value with timestamp or "no data"]
  - TRACES evidence: [trace ID or "no spans found" or "latency stat"]

Answer: "For each symptom, how many independent signal types provide evidence?"
If a symptom is only supported by ONE signal type, mark it as "single-signal anomaly".
If supported by TWO+, mark as "multi-signal confirmed".
""",
        "validates": "Prevents citing unsupported claims; enforces evidence grounding"
    },
    
    {
        "phase": "hypothesis_generation",
        "phase_number": 3,
        "description": "Generate all possible root cause hypotheses",
        "prompt": """
PHASE 3: HYPOTHESIS GENERATION

Based on the symptoms and signals, generate AT LEAST 3 different possible root cause hypotheses.

For each hypothesis, state:
1. ROOT CAUSE: What fundamentally failed?
2. MECHANISM: How did this failure propagate to visible symptoms?
3. EXPECTED SIGNALS: If this were true, what would we see in logs/metrics/traces?
4. OBSERVED SIGNALS: Do the expected signals match what we actually see?

Example format:
  Hypothesis A: "orders service crashed"
    Mechanism: crash → no request handling → frontend retries fail → 5xx errors
    Expected signals: orders metrics drop to 0, logs show connection refused, zero traces
    Match: YES / PARTIAL / NO

Answer: "Which hypotheses fully or mostly match the observed signals?"
""",
        "validates": "Prevents anchoring bias; explores alternative explanations"
    },
    
    {
        "phase": "hypothesis_refinement",
        "phase_number": 4,
        "description": "Test hypotheses with causal analysis",
        "prompt": """
PHASE 4: HYPOTHESIS REFINEMENT

For each remaining hypothesis, answer:

(a) IS THIS LOCALIZED OR DISTRIBUTED?
    Localized: Single service, single metric type affected
    Distributed: Multiple services or cascade effect
    
(b) DOES THE EVIDENCE SHOW CAUSE OR EFFECT?
    Root cause: The fundamental failure (e.g., service crash, resource exhaustion, network latency)
    Effect: Secondary symptom (e.g., 5xx errors from retry failures)
    
    Tip: Causes usually happen FIRST in time. Effects follow. Check timestamps.
    
(c) WHAT WOULD DISCONFIRM THIS HYPOTHESIS?
    List observable data patterns that would prove this wrong.
    Example: "If we found traces with orders service handling requests, 
             then 'orders crashed' would be false."
    
(d) TEMPORAL COHERENCE:
    Order the hypothesis chain chronologically. 
    Is there a clear cause → effect → effect sequence with minimal time gaps?
    If gaps > 5 minutes, explain why (queuing, retry backoff, etc.)

(e) FAILURE DOMAIN CLASSIFICATION:
    Where is the true failure origin? NOT the most visible victim.
    Categories:
      • SERVICE: Specific service crashed, had code bug, resource exhaustion, etc.
      • DATABASE: DB timeout, connection pool exhausted, slow queries, connection failure
      • NETWORK: Latency spike, packet loss, DNS failure, network partition, route misconfiguration
      • INFRASTRUCTURE: Kubernetes scheduler issue, container resource limits, host CPU throttling, storage I/O
      • CONFIGURATION: Misconfigured env var, limits, replicas, feature flags, deprecated API
      • OTHER: Identify what type
    
    IMPORTANT: Don't default to "service" just because a service shows errors.
    Errors in a service are usually EFFECTS of something else failing.
    Ask: "What made this service fail?" Then identify that root cause's domain.

Answer: "After refinement, rank the top 2 hypotheses by evidence strength. For each, specify the failure domain."
""",
        "validates": "Distinguishes true causes from effects; checks temporal logic; enforces domain classification"
    },
    
    {
        "phase": "cross_signal_validation",
        "phase_number": 5,
        "description": "Validate with multi-signal evidence",
        "prompt": """
PHASE 5: CROSS-SIGNAL VALIDATION

For your TOP hypothesis, perform multi-signal validation:

SIGNAL 1 - LOGS:
  Question: Do logs confirm or contradict this hypothesis?
  Check: Error messages, timestamps, service names, correlation with peak times
  
SIGNAL 2 - METRICS:
  Question: Do metrics confirm or contradict this hypothesis?
  Check: Service availability (0 req/s?), latency spikes, resource usage (CPU/memory)
  TRAP: If metrics show NaN on a service, distinguish "no data" from "healthy"
  
SIGNAL 3 - TRACES:
  Question: Do traces confirm or contradict this hypothesis?
  Check: Service spans present/absent, latency distribution (p50 vs p99)
  TRAP: If metrics show 0 req/s but traces show spans, there's an instrumentation gap
  
CONTRADICTION DETECTION:
  - If metrics show 0 req/s but traces show spans for this service → metrics are broken
  - If logs show errors but no corresponding metric/trace data → consider instrumentation gap
  - If all three signals show consistent pattern → HIGH CONFIDENCE

Answer: 
  "How many independent signal types CONFIRM this hypothesis?"
  "Are there any CONTRADICTIONS between signals?"
  "If contradictions exist, explain them (e.g., instrumentation gap, missing data)"
""",
        "validates": "Enforces multi-signal evidence requirement for 'Confirmed' confidence"
    },
    
    {
        "phase": "causal_chain_construction",
        "phase_number": 6,
        "description": "Build evidence-based causal chain",
        "prompt": """
PHASE 6: CAUSAL CHAIN CONSTRUCTION

Build the step-by-step causal chain from root cause to final visible symptom.

Format each step:
  Step N:
    Time: HH:MM:SS UTC
    Service: [name]
    Event: [what happened — keep factual, no interpretation]
    Mechanism: [HOW did this cause the NEXT step?]
    Evidence: [VERBATIM from raw data — log line, metric value, trace ID]

EXAMPLE (DO NOT COPY):
  Step 1:
    Time: 18:10:00 UTC
    Service: orders
    Event: HTTP request rate dropped from 2.3 to 0.0 req/s (sudden)
    Mechanism: Service became unreachable, so clients stopped sending traffic
    Evidence: "[01-http_requests_rate] orders metric = 0.0 req/s"
    
  Step 2:
    Time: 18:10:00 UTC
    Service: frontend
    Event: Began retrying orders requests after connection timeouts
    Mechanism: Frontend retry logic detected timeouts, started retry backoff
    Evidence: "[logs] Zero spans for orders service, connection refused errors pending"
    
  Step 3:
    Time: 18:15:28 UTC
    Service: frontend
    Event: Connection pool exhausted, returned 5xx errors to clients
    Mechanism: Retry exhaustion + connection pool overflow
    Evidence: "[06-http_errors] frontend 5xx spike to 0.804 errors/s"

VALIDATION:
  "Does this chain explain ALL anomalies listed in Phase 1?"
  "Does each step have direct evidence?"
  "Are time gaps > 5 min explained (e.g., retry backoff, queuing)?"
""",
        "validates": "Ensures every causal step is evidence-backed and temporally coherent"
    },
    
    {
        "phase": "confidence_calibration",
        "phase_number": 7,
        "description": "Calibrate confidence level",
        "prompt": """
PHASE 7: CONFIDENCE CALIBRATION

Determine whether your conclusion is "Confirmed" or "Hypothesis" based on evidence quality.

RULE 1: Signal Count
  "Confirmed" requires: ≥2 independent signal types (logs, metrics, traces) point to the SAME root cause at the SAME UTC time
  "Hypothesis" if: Only 1 signal type supports the conclusion
  
  Example CONFIRMED:
    - Metrics: orders req/s dropped to 0
    - Traces: zero orders spans found
    - Logs: frontend connection refused errors
    → 3 signals align → CONFIRMED
    
  Example HYPOTHESIS:
    - Only metrics show catalogue latency spike
    - Cannot correlate with logs or traces
    → 1 signal only → HYPOTHESIS

RULE 2: Mechanism Evidence
  "Confirmed" requires: Every step in causal_chain has direct evidence
  "Hypothesis" if: Some steps are inferred (e.g., "CPU spike PROBABLY caused OOM" but no logs confirming OOM)

RULE 3: Alternative Explanations
  "Confirmed": You ruled out alternative hypotheses with contradicting evidence
  "Hypothesis": Multiple explanations still plausible given available data

FINAL QUESTIONS:
  1. "How many independent signals support this root cause?" (≥2 for Confirmed)
  2. "Is every causal step evidenced, or are some inferred?" (Evidenced for Confirmed)
  3. "Did you rule out alternative hypotheses?" (Yes for Confirmed)
  4. "If evidence is insufficient, acknowledge it" (Use Hypothesis)

DO NOT OVER-CLAIM. If evidence is weak or ambiguous, use "Hypothesis".
""",
        "validates": "Prevents over-confidence and false positives"
    }
]

# Prompts for specific anomaly types (optional specialization)
LATENCY_ANALYSIS_PROTOCOL = """
LATENCY ANALYSIS SPECIALIZATION (when anomaly_type = "latency"):

(1) DISTRIBUTION SHAPE:
    Check: p50 vs p99 latency values
    - Only p99 spiked, p50 stable? → Tail issue (GC pause, individual slow request)
    - Both spiked uniformly? → Systemic bottleneck (DB saturation, resource limit)
    
(2) CROSS-SERVICE UNIFORMITY:
    Check: Did all services latency spike together?
    - Yes → Shared resource problem (DB, network, load balancer)
    - No → Service-specific issue (memory leak, config, code change)
    
(3) RESOURCE CORRELATION:
    Check: CPU/memory spike at same time as latency spike?
    - CPU spike + latency spike together → Compute-bound (increase threads, optimize code)
    - No resource spike + latency spike → I/O-bound (DB slow, network latency)
    
(4) DEPENDENCY CHAIN:
    Check: Trace spans — which services are on critical path?
    Do downstream services all see latency equally, or only those calling the slow service?
"""

CPU_MEMORY_ANALYSIS_PROTOCOL = """
CPU/MEMORY SPIKE ANALYSIS (when anomaly_type = "cpu_spike" or "memory_spike"):

(1) RESOURCE AVAILABILITY:
    Is the spike at 100% (hard limit) or just a spike above baseline?
    - At 100% → Resource exhausted, service likely throttled or killed
    - Below 100% → Transient spike (GC, burst traffic, network I/O wait)
    
(2) CORRELATION WITH ERRORS:
    Did error rate spike at same time as resource spike?
    - Yes → Resource exhaustion caused failures
    - No → Resource spike is unrelated (background maintenance, periodic job)
    
(3) THRESHOLD CONTEXT:
    For memory: Is usage at OOM kill threshold?
    For CPU: Is service single-threaded (spike = unavailability) or multi-threaded?
    
(4) TRACE BEHAVIOR:
    Do traces show request failures/timeouts at time of spike?
    Or do traces show successful requests despite high resource usage?
"""

def get_reasoning_prompt_for_phase(phase_number: int) -> str:
    """Retrieve the full prompt for a specific reasoning phase."""
    for phase in REASONING_PHASES:
        if phase["phase_number"] == phase_number:
            return phase["prompt"]
    return ""

def get_all_reasoning_prompts() -> str:
    """Generate the complete CoT reasoning guidance."""
    prompts = [
        "=== CHAIN-OF-THOUGHT (CoT) REASONING PROTOCOL ===",
        "You will now reason through this RCA using a 7-phase methodology.",
        "Each phase is a checkpoint that builds on the previous one.",
        ""
    ]
    
    for phase in REASONING_PHASES:
        prompts.append(f"\n{phase['prompt']}\n")
    
    return "\n".join(prompts)
