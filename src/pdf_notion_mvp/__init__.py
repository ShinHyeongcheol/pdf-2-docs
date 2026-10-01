"""An offline-first document workflow. No network adapters are enabled."""
import os

# The offline demo must not inherit tracing settings that upload document content.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
