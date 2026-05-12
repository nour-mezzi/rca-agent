# Backwards-compatibility shim — logic has moved to the 4-agent architecture.
# Import directly from orchestrator for new code.
from .orchestrator import analyze_root_cause  # noqa: F401
