"""
Command-line interface for the C2000 debugger.

@date: 24.08.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import json
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click
from rich.console import Console
from rich.table import Table

from .ccxml import CCXMLConfig, DebugProbe, dump_ccxml_config
from .interface import (
    BacktraceCommand,
    Command,
    CommandParser,
    FrameCommand,
    LocationCommand,
    MemCommand,
    PrintCommand,
    check_dss_available,
    generate_ccxml_from_device_config,
    run_repl,
    run_reset,
)
from .render import (
    render_backtrace,
    render_current_line,
    render_frame_source,
    render_mem,
    render_print,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from contextlib import AbstractContextManager

DEFAULT_DEVICE_PATH = Path("c2000_device.json")

REPL_COMMAND_PARSERS: Mapping[type[Command], CommandParser] = {
    BacktraceCommand: render_backtrace,
    FrameCommand: render_frame_source,
    MemCommand: render_mem,
    PrintCommand: render_print,
    LocationCommand: render_current_line,
}


def err_exit(msg: str, code: int = 1) -> NoReturn:
    """
    Tiny convenience function for CLI commands displaying an error message
    and exiting"""
    click.echo(msg)
    sys.exit(code)


def _resolve_device_path(
    ctx: click.Context,
    param: click.Parameter,
    value: str | None,
) -> Path | None:
    if value is None and DEFAULT_DEVICE_PATH.exists():
        return DEFAULT_DEVICE_PATH
    if value is not None:
        path_type = click.Path(exists=True, path_type=Path)
        path = path_type.convert(value, param, ctx)
        assert isinstance(path, Path), "resolution should only be used on Path-like objects"
        return path
    return None


@click.group()
def c2000_debugger() -> None:
    """
    Main entrypoint for all C2000 debugger commands
    """


def _use_ccxml(
    config_path: Path | None = None,
    ccxml_path: Path | None = None,
) -> AbstractContextManager[Path]:
    """
    Syntaxic sugar for the CCXML logic:
    if a CCXML file is explicitly passed, we should use that one directly;
    otherwise it should be generated from device config and cleared on exit.
    Wraps the first case with a nullcontext so that this logic always
    maps to a context manager.
    """
    if config_path is None and ccxml_path is None:
        err_exit("You must pass a CCXML if not passing a device config")
    if ccxml_path is not None:
        return nullcontext(ccxml_path)
    assert config_path is not None, "Checked above"
    return generate_ccxml_from_device_config(config_path)


@c2000_debugger.command()
def info() -> None:
    """
    Shows the info for your DSS install
    """
    dss_info = check_dss_available()

    table = Table(title="DSS Information")
    table.add_column("Key", style="cyan")
    table.add_column("Value", style="magenta")

    for key, value in dss_info.items():
        table.add_row(key, value)

    console = Console()
    console.print(table)


@c2000_debugger.command()
@click.option(
    "--ccxml",
    default=None,
    type=click.Path(),
    help="Path to CCXML configuration file (overrides DSS_CCXML env var)",
)
@click.option(
    "-d",
    "--device",
    default=None,
    callback=_resolve_device_path,
    help="Path to JSON device configuration (generates CCXML)",
)
@click.option(
    "-x",
    "--script",
    "commands_path",
    default=None,
    type=click.Path(exists=True, path_type=Path),
    help="Path to a script of debugger commands to run before dropping into the REPL",
)
def repl(ccxml: Path | None, device: Path | None, commands_path: Path | None) -> None:
    with _use_ccxml(device, ccxml) as generated_ccxml:
        sys.exit(
            run_repl(
                ccxml_path=generated_ccxml,
                commands_path=commands_path,
                command_parsers=REPL_COMMAND_PARSERS,
            ),
        )


@c2000_debugger.command()
@click.option(
    "--ccxml",
    default=None,
    type=click.Path(),
    help="Path to CCXML configuration file (overrides DSS_CCXML env var)",
)
@click.option(
    "-d",
    "--device",
    default=None,
    callback=_resolve_device_path,
    help="Path to JSON device configuration (generates CCXML)",
)
def reset(ccxml: Path | None, device: Path | None) -> None:
    with _use_ccxml(device, ccxml) as ccxml_path:
        sys.exit(run_reset(ccxml_path=ccxml_path))


def _interactive_ccxml_config() -> CCXMLConfig:
    """Interactively prompt user to create a CCXML configuration.

    Returns:
        CCXMLConfig instance created from user input.
    """
    device = input("Enter target device (e.g., F280041): ").strip()
    if not device:
        raise ValueError("Device cannot be empty")

    click.echo("\nAvailable probes:")
    probes = list(DebugProbe)
    for i, probe in enumerate(probes, 1):
        click.echo(f"  {i}. {probe.value}")

    while True:
        choice = input(f"Select probe (1-{len(probes)}): ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(probes):
                selected_probe = probes[idx]
                break
            click.echo(f"Please enter a number between 1 and {len(probes)}")
        except ValueError:
            click.echo("Invalid input. Please enter a number.")

    return CCXMLConfig(device=device, probe=selected_probe)


@c2000_debugger.command()
@click.option(
    "-o",
    "--output",
    type=click.Path(),
    default=None,
    help="Path to the output JSON file",
)
def interactive_ccxml_config(output: Path | None) -> None:
    """
    Interactive helper to set your the device JSON config.
    """
    ccxml = _interactive_ccxml_config()
    target = output or DEFAULT_DEVICE_PATH
    target.write_text(
        json.dumps(
            dump_ccxml_config(ccxml),
            default=str,
            indent=4,
        ),
    )
    click.echo(f"✓ JSON config written to {target} !")
