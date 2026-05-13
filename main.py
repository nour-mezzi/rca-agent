from backend.agents.rca_agent import analyze_root_cause

DATASET_PATH = "datasets/ANOMALY-004-20260507T162820Z"

result = analyze_root_cause(DATASET_PATH)

print("\n=== RCA RESULT ===\n")
print(result)
