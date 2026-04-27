"""
conftest.py — makes aws/lambda/ importable from tests/.

Adds the parent directory (aws/lambda/) to sys.path so pytest can find
smart_recovery_policy, rollback_manager, and recovery_handler.
"""
import sys
from pathlib import Path

# aws/lambda/ is one level up from aws/lambda/tests/
sys.path.insert(0, str(Path(__file__).parent.parent))
