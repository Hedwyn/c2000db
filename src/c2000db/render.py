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
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree

from . import debug_info
from .interface import Command, FrameCommand, MemCommand, PrintCommand

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .interface import SendCommand

# Matches one frame line as printed by `session.callStack.getStackTrace()`.
# `func` has to tolerate an argument list (with spaces, pointers, etc.) inside the
# parens, not just "()" - greedy `.+\)` grabs up to the *last* ")" on the line, which
# backtracking then pins down correctly since there's exactly one " at <file>:<line>
# PC = ... FP = ..." tail to satisfy.
_BT_FRAME_RE = re.compile(
    r"^(?P<idx>\d+)\s+(?P<func>.+\))\s+at\s+(?P<file>\S+):(?P<line>\d+)"
    r"\s+PC\s*=\s*(?P<pc>0x[0-9A-Fa-f]+)\s+FP\s*=\s*(?P<fp>0x[0-9A-Fa-f]+)\s*$",
)

# Matches one `mem` line, e.g.: 0x80007: 0xaa55
_MEM_LINE_RE = re.compile(r"^(?P<addr>0x[0-9A-Fa-f]+):\s*(?P<value>0x[0-9A-Fa-f]+)\s*$")

# Matches the "sp = 0x...", "pc = 0x..." lines repl.js prints ahead of a `mem`
# dump, and the "addr = 0x...", "pc = 0x..." lines it prints after a `print`
# whose expression has an address (see js/repl.js).
_LABELED_HEX_RE = re.compile(r"^(?P<name>sp|pc|addr) = (?P<value>0x[0-9A-Fa-f]+)\s*$")

# Matches `print`/`p` output, e.g.: afpu = 3145778 (0x300032)
_PRINT_RE = re.compile(r"^(?P<expr>.+) = (?P<dec>-?\d+) \((?P<hex>0x[0-9A-Fa-f]+)\)\s*$")


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


def render_backtrace(
    command: Command,
    lines: Iterable[str],
    console: Console | None = None,
) -> None:
    """
    `bt` command parser: renders the call stack as a Rich table (one row per frame).
    Frame 0 - where the target is actually stopped - is marked "▶ current"; jump
    back to it with `frame 0` after exploring frames further up the stack.
    """
    _ = command  # bt takes no arguments - only present to comply with CommandParser
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
    command: Command,
    lines: Iterable[str],
    console: Console | None = None,
    context: int = 15,
) -> None:
    """
    `frame <n>` command parser: locates frame `n` in the call stack (repl.js's
    "frame" command re-emits the same trace format as `bt`) and renders its source
    file around the target line with `rich.syntax.Syntax`.
    """
    assert isinstance(command, FrameCommand), "only ever registered for FrameCommand"
    console = console or Console()
    frames, notes = parse_backtrace_frames(lines)

    frame = next((f for f in frames if f.index == command.frame_index), None)
    if frame is None:
        valid = ", ".join(str(f.index) for f in frames) or "none"
        console.print(f"[red]frame: no frame {command.frame_index} (valid: {valid})[/red]")
        for note in notes:
            console.print(f"[yellow]{note}[/yellow]")
        return

    _render_frame_panel(frame, console, context)


def render_current_line(
    command: Command,
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
    _ = command  # these commands take no arguments - only present to comply with CommandParser
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


@dataclass(frozen=True)
class _MemWord:
    address: int
    hex_text: str
    value_text: str
    value: int


def _parse_mem_lines(
    lines: Iterable[str],
) -> tuple[list[_MemWord], int | None, int | None, list[str]]:
    """
    Splits a `mem` dump's lines into the address/value words, the `sp`/`pc` repl.js
    prints alongside them (`None` if either is missing - e.g. an older repl.js),
    and any other line (e.g. an error) that matches neither.
    """
    words: list[_MemWord] = []
    sp: int | None = None
    pc: int | None = None
    notes: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if (register_match := _LABELED_HEX_RE.match(stripped)) is not None:
            value = int(register_match["value"], 16)
            if register_match["name"] == "sp":
                sp = value
            elif register_match["name"] == "pc":
                pc = value
            # "addr" doesn't appear in `mem`'s own output (only `print`'s) - see
            # `_parse_print_lines` - so nothing to do with it here.
            continue
        match = _MEM_LINE_RE.match(stripped)
        if match is None:
            notes.append(stripped)
            continue
        address, value = int(match["addr"], 16), int(match["value"], 16)
        words.append(_MemWord(address, match["addr"], match["value"], value))
    return words, sp, pc, notes


def render_mem(command: Command, lines: Iterable[str], console: Console | None = None) -> None:
    """
    `mem` command parser: renders the dumped words in a vertical (transposed)
    table - one column per address, one row per representation - with a
    "Variable" row naming the local variable/parameter (if any, via
    `debug_info.local_variable_at`) covering that address in the current frame.
    Empty wherever nothing there is stack-resident (e.g. a `prog`-page dump, or an
    address outside the current frame).
    """
    assert isinstance(command, MemCommand), "render_mem is only ever registered for MemCommand"
    console = console or Console()
    words, sp, pc, notes = _parse_mem_lines(lines)

    scopes: tuple[debug_info.FunctionScope, ...] = ()
    if sp is not None and pc is not None and command.target == "data":
        try:
            scopes = debug_info.load_debug_info(debug_info.DEFAULT_OUT_PATH).scopes
        except Exception as exc:  # noqa: BLE001 - locals annotation is best-effort, never fatal to `mem`
            console.print(f"[yellow]mem: local variable lookup unavailable: {exc!r}[/yellow]")

    table = Table(title="Memory", caption=f"mem {command.address} {command.count} {command.target}")
    table.add_column("")
    for word in words:
        table.add_column(word.hex_text, justify="right", style="cyan")

    if words:
        table.add_row("Hex", *(word.value_text for word in words), style="magenta")
        table.add_row("Decimal", *(str(word.value) for word in words))
        if sp is not None and pc is not None:
            variables = (
                debug_info.local_variable_at(scopes, pc, sp, word.address) or "" for word in words
            )
            table.add_row("Variable", *variables, style="green")
        console.print(table)
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")


@dataclass(frozen=True)
class _PrintLines:
    scalar_display: str | None  # already-colorized "expr = dec (hex)", or None if it didn't match
    scalar_value: int | None  # that same value, unsigned - e.g. a pointer's own target address
    addr: (
        int | None
    )  # the expression's own address, if repl.js could take it (see js/repl.js's "print")
    pc: int | None  # the current PC, reported alongside `addr`
    notes: list[str]


def _parse_print_lines(lines: Iterable[str]) -> _PrintLines:
    """
    Splits a `print` command's lines into the flat scalar result, the
    expression's own address and the current PC if repl.js reported them (both
    `None` when the expression has no address, e.g. a bare register), and any
    other line (e.g. an evaluation error).
    """
    scalar_display: str | None = None
    scalar_value: int | None = None
    addr: int | None = None
    pc: int | None = None
    notes: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if (register_match := _LABELED_HEX_RE.match(stripped)) is not None:
            value = int(register_match["value"], 16)
            if register_match["name"] == "addr":
                addr = value
            elif register_match["name"] == "pc":
                pc = value
            continue
        match = _PRINT_RE.match(stripped)
        if match is None:
            notes.append(stripped)
            continue
        # `hex`, not `dec`, for the numeric value: JS's hex() (Long.toHexString)
        # always renders unsigned, so this is safe to use as an address (e.g. a
        # pointer's own value) without worrying about `dec`'s sign.
        scalar_value = int(match["hex"], 16)
        scalar_display = (
            f"[bold cyan]{match['expr']}[/bold cyan] [dim]=[/dim] "
            f"[bold green]{match['dec']}[/bold green] "
            f"[dim]([/dim][yellow]{match['hex']}[/yellow][dim])[/dim]"
        )
    return _PrintLines(scalar_display, scalar_value, addr, pc, notes)


def _find_variable(
    info: debug_info.DebugInfo,
    pc: int,
    name: str,
) -> debug_info.LocalVariable | debug_info.GlobalVariable | None:
    """
    A local in the function scope containing `pc` takes priority (matches C's
    own shadowing rules for a same-named local vs. global); falls back to a
    global/file-static otherwise - see `debug_info.GlobalVariable` for why that
    fallback isn't itself scope-aware.
    """
    for scope in info.scopes:
        if scope.contains(pc):
            local = next((variable for variable in scope.variables if variable.name == name), None)
            if local is not None:
                return local
            break
    return next((variable for variable in info.globals if variable.name == name), None)


# Hard caps so a bad DWARF size (or a genuinely huge struct/array) can't hang the
# REPL on one `print` - dropped words/items are called out, never silently cut.
_MAX_DECODE_WORDS = 2048
_MAX_DECODE_ITEMS = 64


def _fetch_words(send: SendCommand, addr: int, count: int, console: Console) -> list[int] | None:
    """Reads `count` words at `addr` via a follow-up `mem`-equivalent `send` call."""
    capped = min(count, _MAX_DECODE_WORDS)
    if capped < count:
        console.print(
            f"[yellow]print: {count} words is over the {_MAX_DECODE_WORDS}-word decode cap - "
            f"showing only the first {capped}[/yellow]",
        )
    if capped <= 0:
        return []
    words, _sp, _pc, notes = _parse_mem_lines(send(f"mem {hex(addr)} {capped} data"))
    for note in notes:
        console.print(f"[yellow]{note}[/yellow]")
    if len(words) != capped:
        console.print(
            f"[yellow]print: expected {capped} words back, got {len(words)} - "
            "falling back to the scalar line[/yellow]",
        )
        return None
    return [word.value for word in words]


def _format_words(words: Sequence[int], offset: int, size: int) -> str:
    """
    Formats `size` consecutive words from `words` starting at `offset`, as raw
    hex words - used for anything wider than one word (a multi-word base type, a
    2-word pointer, ...), since this target's cross-word ordering for such a
    value hasn't been verified against real hardware; showing raw words avoids a
    confidently-wrong assembled number.
    """
    chunk = words[offset : offset + max(size, 0)]
    if not chunk:
        return "?"
    return " ".join(hex(word & 0xFFFF) for word in chunk)


def _format_scalar(base_type: debug_info.BaseType, words: Sequence[int], offset: int) -> str:
    chunk = words[offset : offset + max(base_type.size, 0)]
    if len(chunk) != 1:
        return _format_words(words, offset, base_type.size)
    raw = chunk[0] & 0xFFFF
    if base_type.encoding == 2:  # DW_ATE_boolean
        return "true" if raw else "false"
    if base_type.encoding in (5, 6):  # DW_ATE_signed / DW_ATE_signed_char
        signed = raw - 0x10000 if raw >= 0x8000 else raw
        return f"{signed} ({hex(raw)})"
    return f"{raw} ({hex(raw)})"


def _unflatten_index(flat_index: int, counts: Sequence[int]) -> tuple[int, ...]:
    """Row-major index decomposition, e.g. for `int a[2][3]`, flat index 4 -> (1, 1)."""
    indices: list[int] = []
    remaining = flat_index
    for count in reversed(counts):
        indices.append(remaining % count if count else 0)
        remaining //= count or 1
    return tuple(reversed(indices))


def _decode_into(
    node: Tree, value_type: debug_info.Type, words: Sequence[int], word_offset: int
) -> None:
    """Recursively expands `value_type` (at `word_offset` into `words`) as children of `node`."""
    if isinstance(value_type, debug_info.CompositeType):
        for member in value_type.members[:_MAX_DECODE_ITEMS]:
            child = node.add(f"[bold]{member.name}[/bold]")
            _decode_into(child, member.type, words, word_offset + member.offset)
        if len(value_type.members) > _MAX_DECODE_ITEMS:
            node.add(f"[dim]... {len(value_type.members) - _MAX_DECODE_ITEMS} more members[/dim]")
        return
    if isinstance(value_type, debug_info.ArrayType):
        element_size = value_type.element.size or 1
        total = 1
        for count in value_type.counts:
            total *= count
        shown = min(total, _MAX_DECODE_ITEMS)
        for flat_index in range(shown):
            label = "".join(f"[{i}]" for i in _unflatten_index(flat_index, value_type.counts))
            child = node.add(label)
            _decode_into(child, value_type.element, words, word_offset + flat_index * element_size)
        if total > shown:
            node.add(f"[dim]... {total - shown} more elements[/dim]")
        return
    if isinstance(value_type, debug_info.PointerType):
        node.label = (
            f"{node.label} [magenta]= {_format_words(words, word_offset, value_type.size)}"
            "[/magenta] [dim](pointer, not dereferenced)[/dim]"
        )
        return
    if isinstance(value_type, debug_info.EnumType):
        chunk = words[word_offset : word_offset + max(value_type.size, 0)]
        if len(chunk) == 1:
            raw = chunk[0] & 0xFFFF
            name = next(
                (enum_name for enum_value, enum_name in value_type.values if enum_value == raw),
                None,
            )
            shown_value = f"{name} ({raw})" if name is not None else str(raw)
        else:
            shown_value = _format_words(words, word_offset, value_type.size)
        node.label = f"{node.label} [green]= {shown_value}[/green]"
        return
    # BaseType, or the fallback `debug_info.resolve_type` returns for a tag it
    # doesn't model further (e.g. a function pointer's target) - see BaseType.
    node.label = f"{node.label} [green]= {_format_scalar(value_type, words, word_offset)}[/green]"


def _render_struct_tree(
    label: str,
    value_type: debug_info.Type,
    addr: int,
    send: SendCommand,
    console: Console,
) -> bool:
    """Fetches `value_type`'s raw words at `addr` via `send` and prints a nested tree. Returns whether it succeeded."""
    words = _fetch_words(send, addr, value_type.size, console)
    if words is None:
        return False
    tree = Tree(f"{label} [dim]@ {hex(addr)}[/dim]")
    _decode_into(tree, value_type, words, 0)
    console.print(tree)
    return True


def render_print(
    command: Command, lines: Iterable[str], send: SendCommand, console: Console | None = None
) -> None:
    """
    `print`/`p` command parser: for a bare local/global name whose DWARF type
    (see debug_info.py) is a struct/union/array, fetches its raw words via a
    follow-up `mem`-equivalent `send` call (see `InteractiveCommandParser`) and
    renders a nested Rich tree. A pointer *to* one is dereferenced one level the
    same way (using its own value - not `&expr` - as the address, and its
    pointee's DWARF type via `DebugInfo.resolve_pointee`) - a null pointer, or a
    pointer nested inside an already-decoded struct's own fields, is left as a
    raw address instead (see `_decode_into`'s `PointerType` case), to avoid
    chasing a cycle (e.g. a linked list). Otherwise - a scalar, an arbitrary
    expression `debug_info` can't resolve by name (e.g. `myStruct.field`), or
    DWARF/`ofd2000` unavailable - falls back to a flat, colorized
    "expr = dec (hex)" line, same as before this feature existed.
    """
    assert isinstance(command, PrintCommand), (
        "render_print is only ever registered for PrintCommand"
    )
    console = console or Console()
    parsed = _parse_print_lines(lines)
    expr = command.expression.strip()

    if parsed.addr is not None and parsed.pc is not None:
        try:
            info = debug_info.load_debug_info(debug_info.DEFAULT_OUT_PATH)
        except Exception as exc:  # noqa: BLE001 - struct decoding is best-effort, never fatal to `print`
            console.print(f"[yellow]print: struct decoding unavailable: {exc!r}[/yellow]")
        else:
            variable = _find_variable(info, parsed.pc, expr)
            label = f"[bold cyan]{expr}[/bold cyan]"
            decoded = False
            if variable is not None:
                if isinstance(variable.type, (debug_info.CompositeType, debug_info.ArrayType)):
                    decoded = _render_struct_tree(label, variable.type, parsed.addr, send, console)
                elif isinstance(variable.type, debug_info.PointerType) and parsed.scalar_value:
                    pointee = info.resolve_pointee(variable.type)
                    if isinstance(pointee, (debug_info.CompositeType, debug_info.ArrayType)):
                        decoded = _render_struct_tree(
                            f"{label} [dim]->[/dim]",
                            pointee,
                            parsed.scalar_value,
                            send,
                            console,
                        )
            if decoded:
                for note in parsed.notes:
                    console.print(f"[yellow]{note}[/yellow]")
                return

    if parsed.scalar_display is not None:
        console.print(parsed.scalar_display)
    for note in parsed.notes:
        console.print(f"[yellow]{note}[/yellow]")
