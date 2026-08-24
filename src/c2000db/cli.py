"""
Command-line interface for the C2000 debugger.

@date: 24.08.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table
from .interface import check_dss_available


@click.group()
def c2000_debugger() -> None:
    pass


@c2000_debugger.command()
def info() -> None:
    dss_info = check_dss_available()

    table = Table(title="DSS Information")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="magenta")

    for key, value in dss_info.items():
        table.add_row(key, value)

    console = Console()
    console.print(table)
