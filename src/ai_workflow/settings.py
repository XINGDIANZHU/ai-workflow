"""Locate this checkout without depending on the creator's home directory."""
import os
from pathlib import Path


def project_root():
    return Path(os.environ.get("AI_WORKFLOW_HOME") or Path(__file__).resolve().parents[2]).resolve()
