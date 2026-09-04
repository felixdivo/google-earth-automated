Stay on the `main` branch unless told otherwise (when automatically creating worktrees, merge them back in the end).

Always commit changes. Split into sensible chunks. NEVER push.

This is application code, not a library. Do not maintian backward compatibility.

Regularly simplify code to keep it clean and readable. Clean up stale code and dependencies. Remove unused imports, functions, classes, commented-out code, etc. Keep README and `pyproject.toml` updated. Lint and format with ruff (`uv run ruff check` and `uv run ruff format`) before committing; it is the only tool in the toolchain and nothing runs it automatically.
