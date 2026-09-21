"""Make `app` importable when pytest is invoked from anywhere. The stats
service is deliberately Flask-free at import time, so no app fixture is
needed for the aggregation tests."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
