"""Automate Google Earth Studio project generation and rendering."""

import typer

from . import build, gpu_check, login, render, serve

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
