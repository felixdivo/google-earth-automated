"""Automate Google Earth Studio project generation and rendering."""

import typer

from . import build, gpu_check, login, render, serve, shutdown

app = typer.Typer(
    name="earth",
    help=__doc__,
    no_args_is_help=True,
    add_completion=False,
)

app.command("build")(build.command)
app.command("login")(login.command)
app.command("render")(render.command)
app.command("gpu-check")(gpu_check.command)
app.command("serve")(serve.command)


def main() -> None:
    """Console-script entry point.

    Typer already turns a KeyboardInterrupt into exit 130, and each command
    closes its browser from its own `finally` as one propagates -- so all this
    adds is the handler that makes an interrupt quiet and bounded (see
    shutdown.py).
    """
    shutdown.install_signal_handlers()
    app()
