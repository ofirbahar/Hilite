from __future__ import annotations

import click

from .config import ConfigError, load_config
from .pipeline import rescore as rescore_checks
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
@click.argument("run_id", type=int)
@click.option("--db", default="bvm.sqlite3", show_default=True, help="Notebook database file.")
def delete(run_id: int, db: str) -> None:
    """Delete one check and everything collected for it (the number in /runs/NUMBER).

    For when someone writes in and asks for their check to be removed.
    """
    store = Store(db)
    try:
        removed = store.delete_run(run_id)
    finally:
        store.close()
    if not removed:
        raise click.ClickException(f"There is no check number {run_id}.")
    click.echo(f"Check {run_id} was deleted.")


@main.command()
@click.argument("run_id", type=int)
@click.option("--db", default="bvm.sqlite3", show_default=True, help="Notebook database file.")
def link(run_id: int, db: str) -> None:
    """Print the personal link of a check: the one address that can delete it.

    A check made before personal links existed gets one now. Add the site's own address
    in front (https://your-site/...) before sending it to the owner.
    """
    store = Store(db)
    try:
        key = store.ensure_key(run_id)
    finally:
        store.close()
    if key is None:
        raise click.ClickException(f"There is no check number {run_id}.")
    click.echo(f"Personal link of check {run_id}: /runs/{run_id}?key={key}")


@main.command()
@click.option("--db", default="bvm.sqlite3", show_default=True, help="Notebook database file.")
def rescore(db: str) -> None:
    """Score older checks by the current formula, from what they already collected.

    Nothing is fetched again. Run it once after an upgrade that changed the formula, so a
    new check can be compared with the ones before it.
    """
    store = Store(db)
    try:
        done = rescore_checks(store)
    finally:
        store.close()
    click.echo(f"Rescored {done} checks.")


@main.command()
@click.option("--db", default="bvm.sqlite3", show_default=True, help="Notebook database file.")
@click.option("--port", default=8000, show_default=True)
@click.option("--allow-private", is_flag=True,
              help="Allow scanning addresses on this machine or network (development only).")
@click.option("--contact", default=None,
              help="Address shown on the privacy page for questions and deletion requests "
                   "(default: the HILITE_CONTACT environment variable).")
def web(db: str, port: int, allow_private: bool, contact: str | None) -> None:
    """Start the web interface on this machine."""
    from .web import create_app

    create_app(db, allow_private=allow_private, contact=contact).run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
