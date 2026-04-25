from backend.agents.rca_agent import analyze_root_cause

DATASET_PATH = "datasets/ANOMALY-015-20260406T095349Z-observability"

result = analyze_root_cause(DATASET_PATH)

print("\n=== RCA RESULT ===\n")
print(result)
