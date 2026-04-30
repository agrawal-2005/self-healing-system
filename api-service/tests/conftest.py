"""
conftest.py — makes api-service/app/ importable from tests/.

Inserts the api-service directory into sys.path so all tests can do
  from app.xxx import yyy
without needing to install the package.
"""
import sys
from pathlib import Path

# tests/ is inside api-service/; parent = api-service/
sys.path.insert(0, str(Path(__file__).parent.parent))
