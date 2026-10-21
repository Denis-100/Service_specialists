"""An isolated disposable database for browser tests."""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app

with tempfile.TemporaryDirectory(prefix='svobodno-e2e-') as folder:
    create_app({'DATABASE': str(Path(folder) / 'test.sqlite')}).run(host='127.0.0.1', port=5051)
