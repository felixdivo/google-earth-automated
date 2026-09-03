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
    """Console-script entry point: the Typer app, plus graceful interrupts.

    The interrupt itself is announced and bounded by the signal handler; each
    command closes its browser from its own `finally` as the KeyboardInterrupt
    propagates. Typer catches that at the top and exits 130 on its own, so all
    that is left here is the same treatment for one raised anywhere it does
    not reach -- and the right code for a SIGTERM, which is 143, not 130.
    """
    shutdown.install_signal_handlers()
    try:
        app()
    except KeyboardInterrupt:
        raise SystemExit(shutdown.exit_code) from None
    except SystemExit as e:
        if e.code == 130 and shutdown.exit_code != 130:
            raise SystemExit(shutdown.exit_code) from None
        raise
