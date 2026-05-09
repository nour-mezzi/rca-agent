from backend.agents.rca_agent import analyze_root_cause

DATASET_PATH = "datasets/001-20260506T180913Z"

result = analyze_root_cause(DATASET_PATH)

print("\n=== RCA RESULT ===\n")
print(result)
