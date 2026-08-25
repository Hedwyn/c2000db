"""
Renders selected REPL command outputs as Rich tables and source views.

@date: 25.08.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

# Matches one frame line as printed by `session.callStack.getStackTrace()`.
# `func` has to tolerate an argument list (with spaces, pointers, etc.) inside the
# parens, not just "()" - greedy `.+\)` grabs up to the *last* ")" on the line, which
# backtracking then pins down correctly since there's exactly one " at <file>:<line>
# PC = ... FP = ..." tail to satisfy.
_BT_FRAME_RE = re.compile(
    r"^(?P<idx>\d+)\s+(?P<func>.+\))\s+at\s+(?P<file>\S+):(?P<line>\d+)"
    r"\s+PC\s*=\s*(?P<pc>0x[0-9A-Fa-f]+)\s+FP\s*=\s*(?P<fp>0x[0-9A-Fa-f]+)\s*$",
)


@dataclass(frozen=True)
class BacktraceFrame:
    index: int
    function: str
    file: str
    line: int
    pc: str
    fp: str


def parse_backtrace_frames(lines: Iterable[str]) -> tuple[list[BacktraceFrame], list[str]]:
    """
    Parses `bt`/`frame` output (as printed by `session.callStack.getStackTrace()`)
    into structured frames. Lines that don't match the expected frame format (e.g.
    an unwind warning) are returned separately instead of being dropped.
    """
    frames: list[BacktraceFrame] = []
    notes: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        match = _BT_FRAME_RE.match(stripped)
        if match is None:
            notes.append(stripped)
            continue
        frames.append(
            BacktraceFrame(
                index=int(match["idx"]),
                function=match["func"],
                file=match["file"],
                line=int(match["line"]),
                pc=match["pc"],
                fp=match["fp"],
            ),
        )
    return frames, notes


def render_backtrace(args: str, lines: Iterable[str], console: Console | None = None) -> None:
    """
    `bt` command parser: renders the call stack as a Rich table (one row per frame).
    Frame 0 - where the target is actually stopped - is marked "▶ current"; jump
    back to it with `frame 0` after exploring frames further up the stack.
    """
    _ = args  # bt takes no arguments - only present to comply with CommandParser
    console = console or Console()
    frames, notes = parse_backtrace_frames(lines)

    table = Table(
        title="Call Stack",
        caption="▶ = current frame - `frame <n>` to jump, `frame 0` to come back",
    )
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Function", style="bold")
    table.add_column("Location")
    table.add_column("PC", style="magenta")
    table.add_column("FP", style="magenta")
    for frame in frames:
        is_current = frame.index == 0
        table.add_row(
            f"▶ {frame.index}" if is_current else str(frame.index),
            frame.function,
            f"{frame.file}:{frame.line}",
            frame.pc,
            frame.fp,
            style="bold" if is_current else None,
        )

    if frames:
        console.print(table)
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")


def _render_frame_panel(frame: BacktraceFrame, console: Console, context: int) -> None:
    """
    Prints a one-line frame summary followed by a Rich `Panel`/`Syntax` view of its
    source file, with the frame's line highlighted (Rich draws the "❱" gutter arrow
    for it automatically) and `context` lines shown on either side.
    """
    console.print(
        f"[bold]#{frame.index}[/bold] {frame.function} at {frame.file}:{frame.line} "
        f"(PC={frame.pc} FP={frame.fp})",
    )

    source_path = Path(frame.file)
    if not source_path.is_file():
        console.print(f"[yellow]source not found: {frame.file}[/yellow]")
        return

    start_line = max(1, frame.line - context)
    end_line = frame.line + context
    syntax = Syntax.from_path(
        str(source_path),
        line_numbers=True,
        line_range=(start_line, end_line),
        highlight_lines={frame.line},
    )
    console.print(
        Panel(
            syntax,
            title=f"{frame.file}:{frame.line}",
            title_align="left",
            border_style="cyan",
        ),
    )


def render_frame_source(
    args: str,
    lines: Iterable[str],
    console: Console | None = None,
    context: int = 15,
) -> None:
    """
    `frame <n>` command parser: locates frame `n` in the call stack (repl.js's
    "frame" command re-emits the same trace format as `bt`) and renders its source
    file around the target line with `rich.syntax.Syntax`.
    """
    console = console or Console()
    frames, notes = parse_backtrace_frames(lines)

    try:
        index = int(args.strip())
    except ValueError:
        console.print(f"[red]frame: expected a frame number, got {args!r}[/red]")
        return

    frame = next((f for f in frames if f.index == index), None)
    if frame is None:
        valid = ", ".join(str(f.index) for f in frames) or "none"
        console.print(f"[red]frame: no frame {index} (valid: {valid})[/red]")
        for note in notes:
            console.print(f"[yellow]{note}[/yellow]")
        return

    _render_frame_panel(frame, console, context)


def render_current_line(
    args: str,
    lines: Iterable[str],
    console: Console | None = None,
    context: int = 15,
) -> None:
    """
    Command parser for stepping/location commands (halt, run/go/continue/c,
    restart, step/next/over, stepi/si/nexti): repl.js's `printLocation()` emits one
    call-stack-frame-0 line (same format `bt` uses) after each of them - render it
    as a source panel with the current line marked, so stepping visibly moves the
    line pointer. Any other line in the block (e.g. "halted after 22ms:") is printed
    as-is before the panel; if frame 0 specifically couldn't be resolved (e.g. no
    debug info at the current PC, such as right at a function's entry before its
    frame is set up), everything is printed as plain text instead of guessing.
    """
    _ = args  # these commands take no arguments - only present to comply with CommandParser
    console = console or Console()
    frames, notes = parse_backtrace_frames(lines)
    for note in notes:
        console.print(note)
    # Select by the frame's actual `.index == 0` rather than list position - a
    # deeper frame can end up first in `frames` if frame 0's line failed to parse
    # (e.g. an unexpected format), and that must never be mistaken for "current".
    current = next((frame for frame in frames if frame.index == 0), None)
    if current is not None:
        _render_frame_panel(current, console, context)
