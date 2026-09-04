"""`python -m google_earth_automated` -- the same CLI as the `earth` script.

`earth serve` spawns its workers this way rather than through the console
script, so a fleet starts the same whether the parent was run as `uv run earth`,
`python -m google_earth_automated`, or from an installed entry point.
"""

from .cli import main

main()
