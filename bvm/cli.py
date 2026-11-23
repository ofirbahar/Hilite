from __future__ import annotations

import click

from .config import ConfigError, load_config
from .pipeline import run as run_pipeline
from .report import write_report
from .store import Store


@click.group()
def main() -> None:
    """Brand Visibility Monitor."""


@main.command()
@click.argument("settings", type=click.Path(exists=True, dir_okay=False))
@click.option("--db", default="bvm.sqlite3", show_default=True, help="Notebook database file.")
@click.option("--out", default="reports", show_default=True, help="Folder for the report.")
def run(settings: str, db: str, out: str) -> None:
    """Scan one client's site and write the report."""
    try:
        cfg = load_config(settings)
    except ConfigError as exc:
        raise click.ClickException(str(exc))
    store = Store(db)
    try:
        result = run_pipeline(cfg, store)
    finally:
        store.close()
    path = write_report(cfg, result, out)
    pages = sum(1 for f in result.facts if f.kind == "page")
    click.echo(f"{cfg.name}: {pages} pages, {len(result.findings)} findings, "
               f"{len(result.failures)} failures")
    click.echo(f"Report: {path}")


@main.command()
@click.option("--db", default="bvm.sqlite3", show_default=True, help="Notebook database file.")
@click.option("--port", default=8000, show_default=True)
@click.option("--allow-private", is_flag=True,
              help="Allow scanning addresses on this machine or network (development only).")
def web(db: str, port: int, allow_private: bool) -> None:
    """Start the web interface on this machine."""
    from .web import create_app

    create_app(db, allow_private=allow_private).run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
