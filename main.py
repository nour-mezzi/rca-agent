import sys
from backend.agents.rca_agent import analyze_root_cause

# Get dataset path from command line or use default
if len(sys.argv) > 1:
    if sys.argv[1] == "--analyze" and len(sys.argv) > 2:
        DATASET_PATH = sys.argv[2]
    else:
        DATASET_PATH = sys.argv[1]
else:
    DATASET_PATH = "datasets/001-20260506T180913Z"

result = analyze_root_cause(DATASET_PATH)

print("\n=== RCA RESULT ===\n")
print(result)
