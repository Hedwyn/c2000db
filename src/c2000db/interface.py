"""
Implements the command-line logic to invoke
the DSS script for CCS studio

@date: 21.08.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import codecs
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import Field, dataclass, fields
from io import BufferedReader
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, ClassVar, Protocol, Self, get_type_hints

from .ccxml import build_ccxml, load_ccxml_config

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence

# Called with one output line (no trailing newline) at a time, in the order the
# script printed them. A parser raising is caught and reported, not propagated.
type LineParser = Callable[[str], None]

# Writes one command line to the REPL child's stdin (as if a human had typed it)
# and returns an iterator over *that* command's own output lines - same shape as
# what a `CommandParser`/`InteractiveCommandParser` itself receives. Safe to call
# more than once. See `InteractiveCommandParser`.
type SendCommand = Callable[[str], Iterator[str]]


class CommandParser(Protocol):
    """
    Called with (a) the `Command` parsed from whatever followed the command name on
    its "dbg> <cmd> ..." line and (b) an iterator over that command's output lines
    (no trailing newlines, the "dbg> <cmd>" line itself excluded). A parser raising
    is caught and reported, not propagated.
    """

    def __call__(self, command: Command, lines: Iterable[str]) -> None: ...


class InteractiveCommandParser(Protocol):
    """
    Like `CommandParser`, but also gets a `send` callback for issuing one or more
    *follow-up* commands to the same REPL child before finishing its render - e.g.
    a struct-aware `print` that resolves an expression's address and DWARF type,
    then uses `send` to fetch the raw words at that address the same way `mem`
    would, and renders a nested tree instead of `mem`'s flat dump. Checked before
    `command_parsers` in `run_repl`, so a command can only be handled by one of
    the two registries.
    """

    def __call__(self, command: Command, lines: Iterable[str], send: SendCommand) -> None: ...


# The REPL echoes every command it runs as its own "dbg> <cmd>" line - must match
# repl.js's prompt string.
_PROMPT_PREFIX = "dbg> "

# Must match repl.js's END_OF_COMMAND_MARKER.
_END_OF_COMMAND_MARKER = "##--dbg-end--##"


class Command(Protocol):
    """
    Structural base for the per-REPL-command dataclasses below. Each concrete
    subclass is a frozen dataclass that lists every name the debugger accepts for
    it in `aliases`; `parse` (defined once here, generically, for every subclass)
    turns whatever followed the command name on its "dbg> <cmd> ..." line into a
    typed instance. `COMMAND_ALIASES` - built from every subclass's `aliases` - is
    the single source of truth for resolving a REPL command name, and is what gets
    hooked into `command_parsers` (see `_pump_and_parse_stdout`) instead of
    duplicating alias lists there.
    """

    # Mirrors typeshed's `_typeshed.DataclassInstance` protocol (down to the `Any`,
    # which is `dataclasses.Field`'s own type parameter) - declaring the attribute
    # it structurally requires is what lets `fields(cls)` below typecheck without a
    # `# type: ignore`, since every concrete `Command` subclass is a dataclass.
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]

    aliases: ClassVar[tuple[str, ...]] = ()
    # Whether this command's output should trigger automatic source-line
    # rendering (repl.js commands that call printLocation() after moving the
    # target, e.g. `step`/`continue` - see `LocationCommand`).
    show_source: ClassVar[bool] = False

    @classmethod
    def parse(cls, args: str) -> Self:
        """
        Fills this dataclass's fields, in declaration order, from whitespace-
        separated tokens in `args`, casting each to its field's declared type - the
        last field consumes the rest of the line instead of just one token if it's
        a `str` (so e.g. `UnknownCommand`'s one field gets the full raw text; a
        leading `str` field ahead of other fields, e.g. `MemCommand.address`, still
        only consumes one token). A field with nothing left to consume keeps its
        declared default, which is what lets any/every trailing argument be omitted.
        """
        command_fields = fields(cls)
        field_types = get_type_hints(cls)
        last_index = len(command_fields) - 1
        remaining = args.strip()
        kwargs: dict[str, object] = {}
        for index, field in enumerate(command_fields):
            if not field.init or not remaining:
                break
            field_type = field_types[field.name]
            if field_type is str and index == last_index:
                kwargs[field.name] = remaining
                remaining = ""
                continue
            token, _, remaining = remaining.partition(" ")
            remaining = remaining.strip()
            if field_type is str:
                kwargs[field.name] = token
            elif field_type is int:
                kwargs[field.name] = int(token)
            else:
                raise TypeError(
                    f"{cls.__name__}.{field.name}: Command.parse has no generic support "
                    f"for {field_type!r}",
                )
        return cls(**kwargs)


@dataclass(frozen=True)
class UnknownCommand(Command):
    """Fallback for a REPL command with no registered `Command` subclass."""

    raw_args: str = ""


@dataclass(frozen=True)
class BacktraceCommand(Command):
    aliases: ClassVar[tuple[str, ...]] = ("bt",)


@dataclass(frozen=True)
class FrameCommand(Command):
    aliases: ClassVar[tuple[str, ...]] = ("frame", "f")

    frame_index: int = 0


@dataclass(frozen=True)
class MemCommand(Command):
    aliases: ClassVar[tuple[str, ...]] = ("mem",)

    # Mirrors repl.js's own defaults for a bare "mem": anchored at the stack
    # pointer (repl.js actually starts `count` words *below* SP, since locals sit
    # at negative offsets from it - see js/repl.js) and 16 words - enough to
    # cover a typical frame without flooding the output.
    address: str = "SP"
    count: int = 16
    target: str = "data"


@dataclass(frozen=True)
class PrintCommand(Command):
    aliases: ClassVar[tuple[str, ...]] = ("print", "p")

    expression: str = ""


@dataclass(frozen=True)
class LocationCommand(Command):
    """
    repl.js commands that call printLocation() after moving the target - i.e. ones
    whose output should trigger automatic source-line rendering.
    """

    aliases: ClassVar[tuple[str, ...]] = (
        "halt",
        "run",
        "go",
        "continue",
        "c",
        "restart",
        "step",
        "s",
        "next",
        "n",
        "over",
        "stepi",
        "si",
        "nexti",
    )
    show_source: ClassVar[bool] = True


COMMANDS: tuple[type[Command], ...] = (
    BacktraceCommand,
    FrameCommand,
    MemCommand,
    PrintCommand,
    LocationCommand,
)


def _build_alias_map(commands: Iterable[type[Command]]) -> dict[str, type[Command]]:
    alias_map: dict[str, type[Command]] = {}
    for command_cls in commands:
        for alias in command_cls.aliases:
            if alias in alias_map:
                other = alias_map[alias]
                raise ValueError(
                    f"alias {alias!r} claimed by both {other.__name__} and {command_cls.__name__}",
                )
            alias_map[alias] = command_cls
    return alias_map


# Every alias of every registered `Command`, mapping to its dedicated class - the
# single source of truth `_pump_and_parse_stdout` uses to resolve a REPL command
# name into the class it should be parsed as (and then looked up in `command_parsers`).
COMMAND_ALIASES: Mapping[str, type[Command]] = _build_alias_map(COMMANDS)


DSS_BASENAME = "dss"
JS_FOLDER_PATH = Path(__file__).parent / "js"

DSS_ENV_VARNAME = "DSS_PATH"


def expand_dss_location_from_path(system: str | None = None) -> Path | None:
    """
    Expands DSS script to its full path if found in PATH,
    otherwise returns None.
    """
    if found := shutil.which(get_dss_script_name(system)):
        return Path(found)
    return None


def get_script_by_name(name: str) -> Path:
    """
    Given the `name` of a script pre-bundled in this project,
    returns the full path to the script.
    Note: .js extension should be omitted
    """
    path = JS_FOLDER_PATH / (name + ".js")
    if not path.exists():
        raise FileNotFoundError(f"No script `{name}` bundled in this package")
    return path


def get_dss_script_name(system: str | None = None) -> str:
    """
    Returns the expected DSS script name for the target `system`.
    Detects system automatically if not passed explicitly.
    """
    system = system or platform.system()
    extension = ".bat" if system == "Windows" else ".sh"
    return DSS_BASENAME + extension


def get_dss_path_from_env() -> Path:
    """
    Extracts the path to DSS script configured in the environment
    and raises `RuntimeError` if not found.
    """
    # first check if the dedicated env var was set explicitly
    if (env_var_value := os.environ.get(DSS_ENV_VARNAME)) is not None:
        return Path(env_var_value)
    # if not, assuming the folder is added to PATH
    if not (dss_location := expand_dss_location_from_path()):
        raise RuntimeError(
            "DSS not found in path, and DSS_PATH env var is not set"
            "You have the following options:\n"
            " * add CCS Studio install folder to your PATH",
            f" * set {DSS_ENV_VARNAME} environement variable ",
            " * or pass it explicitly to the CLI",
        )
    return dss_location


def invoke_dss(dss_location: Path | None = None, *args: str) -> tuple[str, str]:
    """
    Invokes DSS script with `args`
    and returns (stdout, stderr)
    """
    dss_location = dss_location or get_dss_path_from_env()
    command = [str(dss_location), *args]

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.communicate()


# Keeping the definition loose for now
type DSSInfo = dict[str, str]


def parse_info(stdout: str) -> DSSInfo:
    lines = stdout.strip().split("\n")
    raw_parsed_dict: DSSInfo = {}
    for line in lines:
        if not line:
            continue
        key, val = line.split("=")
        raw_parsed_dict[key.strip()] = val.strip()
    return raw_parsed_dict


def check_dss_available(dss_location: Path | None = None) -> DSSInfo:
    dss_location = dss_location or get_dss_path_from_env()
    if not dss_location.exists():
        raise FileNotFoundError(
            f"DSS location configured as {dss_location}, but file does not exist",
        )
    stdout, _ = invoke_dss(dss_location, str(get_script_by_name("get_info")))
    return parse_info(stdout)


@contextmanager
def generate_ccxml_from_device_config(config_path: Path) -> Generator[Path]:
    """
    Context manager that generates a CCXML file in a temp directory.
    Loads device configuration from JSON and generates the CCXML file.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        with config_path.open() as f:
            config_data = json.load(f)

        ccxml_config = load_ccxml_config(config_data)
        ccxml_content = build_ccxml(ccxml_config)

        ccxml_path = Path(temp_dir) / "generated.ccxml"
        ccxml_path.write_text(ccxml_content)

        yield ccxml_path


def _build_dss_invocation(
    script: str,
    dss_location: Path | None,
    ccxml_path: Path | None,
    commands_path: Path | None,
) -> tuple[list[str], dict[str, str]]:
    dss_location = dss_location or get_dss_path_from_env()
    if not dss_location.exists():
        raise FileNotFoundError(
            f"DSS location configured as {dss_location}, but file does not exist",
        )
    script_path = get_script_by_name(script)
    command = [str(dss_location), str(script_path)]
    env = os.environ.copy()
    if ccxml_path:
        env["DSS_CCXML"] = str(ccxml_path)
    if commands_path:
        env["DSS_COMMANDS"] = str(commands_path)
    return command, env


def run_script(
    script: str,
    dss_location: Path | None,
    ccxml_path: Path | None = None,
    commands_path: Path | None = None,
) -> int:
    """
    Main helper to run a script with dss
    """
    command, env = _build_dss_invocation(script, dss_location, ccxml_path, commands_path)
    proc = subprocess.run(command, check=False, env=env)  # noqa: S603
    return proc.returncode


def _iter_output_lines(
    stdout: BufferedReader,
    line_parsers: Sequence[LineParser],
) -> Generator[str]:
    """
    Decodes `stdout` and yields each complete line the child printed, in order,
    calling every parser in `line_parsers` on it first (the end-of-command marker is
    exempt - it's plumbing for `gather_command_output`, not real REPL output).

    Also echoes the unterminated "dbg> " prompt the instant it shows up (it never
    gets a trailing newline, since it's printed right before `stdin.readLine()`, so a
    human needs to see it before typing) - this always falls between one command's
    end-of-command marker and the next "dbg> <cmd>" line, i.e. never while a
    `gather_command_output` call is mid-block, so it's safe to always echo live.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    while chunk := stdout.read1(2**16):
        pending += decoder.decode(chunk)
        while (newline_idx := pending.find("\n")) != -1:
            line, pending = pending[:newline_idx], pending[newline_idx + 1 :]
            line = line.rstrip("\r")
            if line != _END_OF_COMMAND_MARKER:
                for parser in line_parsers:
                    try:
                        parser(line)
                    except Exception as exc:  # noqa: BLE001 - a broken parser must not kill the REPL
                        print(f"[c2000db] line parser {parser!r} raised: {exc!r}", file=sys.stderr)  # noqa: T201
            yield line
        if pending == _PROMPT_PREFIX:
            sys.stdout.write(pending)
            sys.stdout.flush()
            pending = ""
    if pending:
        yield pending.rstrip("\r")


def gather_command_output(lines: Iterator[str]) -> Generator[str]:
    """
    Pulls lines from `lines` (as produced by `_iter_output_lines`) and yields them
    one by one, stopping - without yielding it - at the end-of-command marker that
    closes the current command's block. Meant to be called once per command, right
    after its "dbg> <cmd>" line has been consumed from `lines`.
    """
    for line in lines:
        if line == _END_OF_COMMAND_MARKER:
            return
        yield line


def _echo_command_output(command: Command, lines: Iterable[str]) -> None:
    """Default `CommandParser`: just prints each line, for commands with no registered parser."""
    _ = command
    for line in lines:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _run_command_parser(
    parser: CommandParser,
    command_cls: type[Command],
    args: str,
    lines: Iterator[str],
) -> None:
    try:
        command = command_cls.parse(args)
        parser(command, lines)
    except Exception as exc:  # noqa: BLE001 - a broken parser must not kill the REPL
        print(f"[c2000db] command parser {parser!r} raised: {exc!r}", file=sys.stderr)  # noqa: T201
    finally:
        # Drain whatever the parser didn't consume, so `lines` (shared with the
        # caller's line iterator) ends up positioned right after this command's
        # end-of-command marker regardless of how much the parser actually read.
        for _ in lines:
            pass


def _run_interactive_command_parser(
    parser: InteractiveCommandParser,
    command_cls: type[Command],
    args: str,
    lines: Iterator[str],
    send: SendCommand,
) -> None:
    try:
        command = command_cls.parse(args)
        parser(command, lines, send)
    except Exception as exc:  # noqa: BLE001 - a broken parser must not kill the REPL
        print(f"[c2000db] interactive command parser {parser!r} raised: {exc!r}", file=sys.stderr)  # noqa: T201
    finally:
        for _ in lines:
            pass


def _pump_and_parse_stdout(
    stdout: BufferedReader,
    child_stdin: IO[bytes],
    stdin_lock: threading.Lock,
    line_parsers: Sequence[LineParser],
    command_parsers: Mapping[type[Command], CommandParser],
    interactive_command_parsers: Mapping[type[Command], InteractiveCommandParser],
    script_lines: Iterable[str] = (),
) -> None:
    """
    Echoes `stdout` to this process's stdout as it arrives, while additionally:
      * calling every parser in `line_parsers` on each complete line the child printed.
      * for a command whose class (resolved via `COMMAND_ALIASES`) is a key in
        `command_parsers`, handing that command's output lines to the matching
        parser instead of printing them raw - the "dbg> <cmd>" line itself is still
        echoed live, only the result lines are replaced by whatever the parser
        renders. The raw text that followed the command name is parsed into that
        class via `Command.parse` before the parser sees it, so a parser only ever
        deals with typed, defaulted fields - never a raw argument string.
      * same, but for `interactive_command_parsers` (checked first): the parser
        additionally gets a `send` callback (built here, closing over this same
        `lines` generator and `child_stdin`) it can use to issue its own follow-up
        commands - see `InteractiveCommandParser`.
      * feeding `script_lines` into `child_stdin` itself, one at a time, only
        once each prior command's *entire* processing (including any of its own
        `send` follow-ups) has finished - see `run_repl` for why this can't be
        left to repl.js's own DSS_COMMANDS file-reading whenever
        `interactive_command_parsers` is in play.

    A human can still type interactively while this runs (once `script_lines` is
    exhausted): `child_stdin` is also fed by a background thread relaying this
    process's own stdin (see `_forward_stdin` in `run_repl`) - `stdin_lock`
    serializes writes between that thread, `send`, and the script feeder.
    """
    lines = _iter_output_lines(stdout, line_parsers)
    pending_script = list(script_lines)

    def write_line(command_line: str) -> None:
        with stdin_lock:
            child_stdin.write((command_line + "\n").encode())
            child_stdin.flush()

    def send(command_line: str) -> Iterator[str]:
        write_line(command_line)
        # Consume the child's own "dbg> <cmd>" echo of the line we just sent,
        # same as a human-typed command gets - everything after it, up to its
        # end-of-command marker, is this follow-up command's actual output.
        for echoed in lines:
            if echoed.startswith(_PROMPT_PREFIX):
                break
        return gather_command_output(lines)

    if pending_script:
        write_line(pending_script.pop(0))

    for line in lines:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        if line.startswith(_PROMPT_PREFIX):
            command_name, _, args = line.removeprefix(_PROMPT_PREFIX).partition(" ")
            command_cls = COMMAND_ALIASES.get(command_name, UnknownCommand)
            if interactive_parser := interactive_command_parsers.get(command_cls):
                _run_interactive_command_parser(
                    interactive_parser,
                    command_cls,
                    args,
                    gather_command_output(lines),
                    send,
                )
            else:
                parser = command_parsers.get(command_cls, _echo_command_output)
                _run_command_parser(parser, command_cls, args, gather_command_output(lines))
            if pending_script:
                write_line(pending_script.pop(0))


def run_reset(dss_location: Path | None = None, ccxml_path: Path | None = None) -> int:
    """
    Resets the target device
    """
    return run_script("reset", dss_location=dss_location, ccxml_path=ccxml_path)


def _forward_stdin(child_stdin: IO[bytes], stdin_lock: threading.Lock) -> None:
    """
    Relays this process's own stdin to the REPL child's stdin, one line at a time,
    so a human can still type interactively even though `run_repl` now pipes
    stdin itself (needed for `send` - see `_pump_and_parse_stdout`). Runs until
    our stdin hits EOF or the child's stdin pipe is gone (child exited while a
    write was in flight - fine on the way out).
    """
    try:
        while chunk := sys.stdin.buffer.readline():
            with stdin_lock:
                child_stdin.write(chunk)
                child_stdin.flush()
    except (BrokenPipeError, ValueError):
        pass


def _read_command_script(commands_path: Path) -> list[str]:
    """
    Reads a debugger command script the same way repl.js's own `runCommandScript`
    does (blank lines and lines starting with '#' skipped) - used by `run_repl`
    to feed it through `child_stdin` itself, instead of via `DSS_COMMANDS`,
    whenever `interactive_command_parsers` is in play (see `run_repl`).
    """
    lines: list[str] = []
    for raw_line in commands_path.read_text().splitlines():
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    return lines


def run_repl(
    dss_location: Path | None = None,
    ccxml_path: Path | None = None,
    commands_path: Path | None = None,
    line_parsers: Iterable[LineParser] = (),
    command_parsers: Mapping[type[Command], CommandParser] | None = None,
    interactive_command_parsers: Mapping[type[Command], InteractiveCommandParser] | None = None,
) -> int:
    """
    Runs the interactive REPL script.
    If `commands_path` is given, its lines are run as debugger commands
    before dropping into the interactive prompt (like `gdb -x`) - ordinarily via
    repl.js's own `DSS_COMMANDS`-driven file reading, *unless*
    `interactive_command_parsers` is given (see below), in which case this
    process feeds them through `child_stdin` itself instead (see
    `_read_command_script`, `_pump_and_parse_stdout`).
    If `line_parsers` is given, each is called with every line the REPL prints to
    stdout (stderr is untouched). Output is still echoed to this process's stdout
    as it arrives.
    If `command_parsers` is given, running a command whose resolved `Command`
    subclass (see `COMMAND_ALIASES`) is a key in it (e.g. `BacktraceCommand` for
    "bt") hands that command's output lines to the matching parser instead of
    printing them raw - typically used to render a command's output as a Rich table.
    If `interactive_command_parsers` is given, same idea but the parser can also
    issue its own follow-up commands to the child - see `InteractiveCommandParser`.
    That capability is why `commands_path` can't be left to repl.js's own
    DSS_COMMANDS file-reading here: that loop never checks stdin between script
    lines, so a follow-up written to it would sit unread while repl.js raced
    ahead to the *next* script line - whose own output `send` would then
    mistake for the follow-up's.
    Registering either kind of parser means this process now mediates stdin too
    (needed so a parser's follow-up commands, this process's own script-line
    feeding, and a human's typing don't race each other) - see `_forward_stdin`
    - rather than the child inheriting it directly.
    Returns the exit code from the DSS process.
    """
    parsers = list(line_parsers)
    interactive_parsers = interactive_command_parsers or {}
    if not parsers and not command_parsers and not interactive_parsers:
        command, env = _build_dss_invocation("repl", dss_location, ccxml_path, commands_path)
        proc = subprocess.run(command, check=False, env=env)  # noqa: S603
        return proc.returncode

    # DSS_COMMANDS is only safe to leave to repl.js when nothing here will ever
    # write a follow-up command mid-script - see the docstring above.
    script_lines: list[str] = []
    if commands_path is not None and interactive_parsers:
        script_lines = _read_command_script(commands_path)
        command, env = _build_dss_invocation("repl", dss_location, ccxml_path, None)
    else:
        command, env = _build_dss_invocation("repl", dss_location, ccxml_path, commands_path)

    with subprocess.Popen(  # noqa: S603
        command,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as repl_proc:
        assert isinstance(repl_proc.stdout, BufferedReader), "stdout explicitly piped above"
        assert repl_proc.stdin is not None, "stdin explicitly piped above"
        stdin_lock = threading.Lock()
        threading.Thread(
            target=_forward_stdin,
            args=(repl_proc.stdin, stdin_lock),
            daemon=True,
        ).start()
        _pump_and_parse_stdout(
            repl_proc.stdout,
            repl_proc.stdin,
            stdin_lock,
            parsers,
            command_parsers or {},
            interactive_parsers,
            script_lines,
        )
        return repl_proc.wait()
