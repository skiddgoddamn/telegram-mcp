"""Launcher for Claude Code.

runtime.py calls load_dotenv() with no path, so it only finds .env when the
process starts inside this checkout — and an MCP client launches the server
from whatever directory it happens to be in. Chdir here first, then hand over
to main.py unchanged.
"""

import os
import runpy

os.chdir(os.path.dirname(os.path.abspath(__file__)))
runpy.run_path("main.py", run_name="__main__")
