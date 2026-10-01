"""``kintsua`` command-line entry point (``kintsua.cli.main:app`` in pyproject)."""

from __future__ import annotations

from typing import Annotated

import typer

from kintsua import __version__
from kintsua.cli.watch import app as watch_app

app = typer.Typer(
    help="Kintsua: self-maintaining integrations for AI agents.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    add_completion=False,
)
app.add_typer(watch_app, name="watch", help="Snapshot and diff MCP servers / OpenAPI specs in CI.")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"kintsua {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", "-V", callback=_version, is_eager=True, help="Print the version."
        ),
    ] = False,
) -> None:
    """Watch every external API and MCP tool your agent depends on."""


if __name__ == "__main__":
    app()
