# Failure Domain Improvement: Removing Linguistic Bias

## Problem Identified

The original field name `primary_service` **linguistically biased** the model toward assuming root causes are always services. This caused it to:
- Overlook infrastructure failures (Kubernetes, container limits)
- Miss network issues (latency, packet loss, DNS)
- Ignore database problems (connection pool exhaustion, slow queries)
- Not consider configuration errors
- Default to "it's a service bug" when the real issue was elsewhere

### Example
```
Observed: Frontend getting 5xx errors, catalogue latency spike
Original bias: "It's the frontend service failing"
Better answer: "Network latency is the root cause"
```

---

## Solution: "failure_domain" Instead of "primary_service"

Changed the field to `failure_domain` which explicitly supports these categories:

| Failure Domain | Examples | Indicators |
|---|---|---|
| **service** | orders crashed, frontend bug | App logs show errors, traces fail |
| **database** | connection pool exhausted, slow queries | Latency spike, metric NaN, timeouts |
| **network** | latency spike, packet loss, DNS failure | All services latency up, p99 >> p50 |
| **infrastructure** | Kubernetes throttling, container OOM | CPU at 100%, memory limits hit |
| **configuration** | misconfigured env var, wrong limits | Service behavior changed, logs show config |
| **other** | TLS cert issue, storage I/O, etc. | Specify in evidence |

---

## Changes Made

### 1. Orchestrator System Prompt (`orchestrator.py`)

**Old:**
```json
"primary_service": "<the service where the failure originated — NOT the most visible victim>"
```

**New:**
```json
"failure_domain": "<Name of the system component where the failure originated. 
Can be: service name (orders, frontend), infrastructure (kubernetes, container, 
network), database (query timeout, connection pool), configuration issue, or other. 
NOT necessarily the most visible victim — identify the true source>"
```

**Added explicit guidance:**
```
FAILURE DOMAIN CLARITY: The failure_domain field captures WHERE the failure 
originated, NOT the most visible victim.

Examples:
  - If frontend has 5xx errors because orders service is down → failure_domain: "orders"
  - If requests are timing out due to network latency spike → failure_domain: "network"
  - If database queries are slow due to connection pool exhaustion → failure_domain: "database"
  - If CPU spike is from Kubernetes resource limits → failure_domain: "infrastructure"
  - If service crashes due to misconfigured environment variable → failure_domain: "configuration"

Do NOT default to service names — consider infrastructure, network, database, and 
configuration as root causes.
```

### 2. Failure Mode Enum (Expanded)

**Old:**
```
crash|oom|connection_refused|dns_failure|db_timeout|config_error|
dependency_unavailable|instrumentation_gap|network_latency|resource_exhaustion|other
```

**New:**
```
crash|oom|connection_refused|dns_failure|db_timeout|db_connection_pool_exhausted|
config_error|dependency_unavailable|instrumentation_gap|network_latency|
network_partition|resource_exhaustion|infrastructure_throttling|other
```

### 3. JSON Schema (`rca_schema.json`)

**Changed required field from:**
```json
"required": ["statement", "service", "component", "failure_type", "evidence_citations"]
```

**To:**
```json
"required": ["statement", "failure_domain", "failure_type", "evidence_citations"]
```

**Updated component to be optional**, since not all failures have components (e.g., network failures don't have a "component").

**Expanded failure_type enum** to include:
- `network_latency`
- `network_partition`
- `database_timeout`
- `database_connection_pool_exhausted`
- `infrastructure_throttling`
- `infrastructure_crash`

### 4. Reasoning Config (`reasoning_config.py`)

**Added to Phase 4 (Hypothesis Refinement):**

```
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
```

---

## Before & After Examples

### Example 1: Network Latency
**Before:**
```
Symptom: Frontend 5xx errors
Observed: All services latency spike together
Old RCA: primary_service = "frontend" (WRONG - service is victim, not cause)
```

**After:**
```
Symptom: Frontend 5xx errors
Observed: All services latency spike together, p99 >> p50, uniform across all
New RCA: failure_domain = "network", failure_mode = "network_latency"
Reason: Uniform latency spike across ALL services points to shared network issue
```

### Example 2: Database Connection Pool
**Before:**
```
Symptom: Slow queries, timeouts
Observed: Database metrics show connection count at max, query latency spiking
Old RCA: primary_service = "database_handler" (vague, specific service)
```

**After:**
```
Symptom: Slow queries, timeouts
Observed: Connection count at limit, wait time spiking
New RCA: failure_domain = "database", failure_mode = "database_connection_pool_exhausted"
Reason: Specific infrastructure bottleneck identified
```

### Example 3: Kubernetes Resource Limit
**Before:**
```
Symptom: Service becomes unresponsive
Observed: CPU metric at 100%, memory at limit
Old RCA: primary_service = "orders" (WRONG - service working fine, throttled by infra)
```

**After:**
```
Symptom: Service becomes unresponsive
Observed: CPU at 100%, memory at container limit, no logs from service
New RCA: failure_domain = "infrastructure", failure_mode = "infrastructure_throttling"
Reason: External resource constraint, not service bug
```

---

## Linguistic Bias Removal

This change removes **multiple layers of linguistic bias**:

1. **Field name bias:** "primary_service" primed thinking toward services
2. **Enum bias:** Old failure_mode didn't explicitly include network/infrastructure options
3. **Reasoning bias:** New Phase 4 guidance explicitly warns against defaulting to "service"
4. **Causal bias:** Prompt now emphasizes: effects in services are usually caused by something else

### Research Support
This aligns with cognitive bias research:
- **Anchoring bias:** Removing "service" as default anchor
- **Availability heuristic:** Expanding visible categories (network, database, infrastructure)
- **Confirmation bias:** Explicit instruction to test against alternatives

---

## Impact on RCA Quality

### What Improves
✓ Network issues now correctly identified as "network" failures
✓ Database bottlenecks identified as "database" issues
✓ Infrastructure limits identified as "infrastructure" problems
✓ Better causal chain construction (identifies true origin, not victim)
✓ Better remediation recommendations (fix the right thing, not the symptom)

### Validation
- Schema now accepts and validates all failure domain categories
- CoT Phase 4 explicitly asks "what is the failure domain?"
- Output can be tracked to measure improvement in domain categorization

---

## Backward Compatibility

⚠️ **Breaking Change:** Field renamed from `primary_service` to `failure_domain`

**Migration:**
- Old RCA outputs used `"primary_service": "orders"`
- New RCA outputs use `"failure_domain": "orders"` (for service failures)
- Also accept `"failure_domain": "network"`, `"database"`, `"infrastructure"`, etc.

**For historical data:**
```python
# Can add a compatibility layer if needed
failure_domain = rca.get("failure_domain") or rca.get("primary_service")
```

---

## Testing Recommendations

Run your existing datasets and check:

1. **Service failures** → `failure_domain` should still be service name
2. **Network issues** → `failure_domain` should be "network"
3. **Database issues** → `failure_domain` should be "database"
4. **Infrastructure issues** → `failure_domain` should be "infrastructure"

Example validation:
```python
import json
rca = json.load(open("rca-analysis-*.json"))
print(f"Failure domain: {rca['root_cause']['failure_domain']}")
print(f"Failure mode: {rca['root_cause']['failure_mode']}")

# Should cover variety of domains, not just services
```

---

## Summary

By changing `primary_service` to `failure_domain` and expanding the schema, reasoning, and guidance, we:

✅ Remove linguistic bias toward "service" as default root cause
✅ Enable identification of network, database, and infrastructure failures
✅ Improve causal chain accuracy (identify origin, not victim)
✅ Better align output with actual RCA best practices
✅ Enable more accurate remediation recommendations

---

Date: May 12, 2026
Related commit: "fix: change 'primary_service' to 'failure_domain' to support broader root causes"
