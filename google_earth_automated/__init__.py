"""Automate Google Earth Studio project generation and rendering."""

from pathlib import Path

# Everything this app writes or reads outside the package lives under the repo:
# .auth/chrome-profile (the login profile) and out/ (generated .esp files and
# rendered frames). Defined once here so the modules that need it agree.
REPO_ROOT = Path(__file__).resolve().parent.parent
