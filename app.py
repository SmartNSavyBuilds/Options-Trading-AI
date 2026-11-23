import runpy
from pathlib import Path

_dashboard = Path(__file__).resolve().with_name('dashboard.py')
if not _dashboard.exists():
    raise FileNotFoundError(f"Missing dashboard entrypoint: {_dashboard}")

runpy.run_path(str(_dashboard), run_name='__main__')
