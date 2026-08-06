# run_dashboard.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from src.engine.web_dashboard import run_dashboard

run_dashboard()