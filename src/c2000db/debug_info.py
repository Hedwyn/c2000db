"""
Extracts function scopes and their stack-resident local variables from a built
`.out`'s DWARF debug info, via TI's `ofd2000` object-file-display tool - the DSS
scripting API (see interface.py) has no "list locals" call of its own.

@date: 25.08.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

OFD_BASENAME = "ofd2000"
OFD_ENV_VARNAME = "C2000_OFD_PATH"

# Matches JS's own `DEFAULT_OUT` in js/repl.js.
DEFAULT_OUT_PATH = Path("build/fw-boot.out")

# The DWARF register number TI's C28x backend assigns to SP - confirmed by
# cross-referencing a real build's `DW_TAG_TI_assign_register` dies (which name
# every register DWARF uses) against its `DW_OP_bregN` variable locations.
_SP_DWARF_REGISTER = 20

# A stack-resident variable's location, e.g. "DW_OP_breg20 0xffffffea" (SP - 22).
# `ofd2000 --xml` always renders the offset as 8 hex digits (32-bit two's
# complement), for both this and any other `DW_OP_bregN` register.
_STACK_LOCATION_RE = re.compile(rf"^DW_OP_breg{_SP_DWARF_REGISTER} (0x[0-9a-fA-F]+)$")


def expand_ofd_location_from_path() -> Path | None:
    """
    Expands `ofd2000` to its full path if found in PATH, otherwise returns None.
    """
    if found := shutil.which(OFD_BASENAME):
        return Path(found)
    return None


def get_ofd_path_from_env() -> Path:
    """
    Extracts the path to `ofd2000` configured in the environment and raises
    `RuntimeError` if not found.
    """
    if (env_var_value := os.environ.get(OFD_ENV_VARNAME)) is not None:
        return Path(env_var_value)
    if not (ofd_location := expand_ofd_location_from_path()):
        raise RuntimeError(
            f"{OFD_BASENAME} not found in PATH, and {OFD_ENV_VARNAME} env var is not set. "
            "Point it at the C2000 code generation tools' ofd2000 binary.",
        )
    return ofd_location


@dataclass(frozen=True)
class LocalVariable:
    """One stack-resident local/parameter, as `SP + offset` words, `size` words wide."""

    name: str
    offset: int
    size: int = 1


@dataclass(frozen=True)
class FunctionScope:
    """One function's PC range and the stack-resident locals declared anywhere in it."""

    name: str
    low_pc: int
    high_pc: int
    variables: tuple[LocalVariable, ...]

    def contains(self, pc: int) -> bool:
        return self.low_pc <= pc < self.high_pc

    def variable_at(self, sp: int, address: int) -> str | None:
        for variable in self.variables:
            start = sp + variable.offset
            if start <= address < start + variable.size:
                return variable.name
        return None


def _die_value(die: ET.Element, attr_name: str) -> ET.Element | None:
    for attribute in die.findall("attribute"):
        type_el = attribute.find("type")
        if type_el is not None and type_el.text == attr_name:
            return attribute.find("value")
    return None


def _attr_string(die: ET.Element, attr_name: str) -> str | None:
    value = _die_value(die, attr_name)
    string_el = value.find("string") if value is not None else None
    return string_el.text if string_el is not None else None


def _attr_addr(die: ET.Element, attr_name: str) -> int | None:
    value = _die_value(die, attr_name)
    addr_el = value.find("addr") if value is not None else None
    return int(addr_el.text, 16) if addr_el is not None and addr_el.text else None


def _attr_const(die: ET.Element, attr_name: str) -> int | None:
    value = _die_value(die, attr_name)
    const_el = value.find("const") if value is not None else None
    return int(const_el.text, 16) if const_el is not None and const_el.text else None


def _attr_type_idref(die: ET.Element) -> str | None:
    value = _die_value(die, "DW_AT_type")
    ref_el = value.find("ref") if value is not None else None
    idref = ref_el.get("idref") if ref_el is not None else None
    return idref or None


def _stack_offset(die: ET.Element) -> int | None:
    value = _die_value(die, "DW_AT_location")
    block_el = value.find("block") if value is not None else None
    if block_el is None or not block_el.text:
        return None
    match = _STACK_LOCATION_RE.match(block_el.text.strip())
    if match is None:
        return None
    raw = int(match[1], 16)
    return raw - 2**32 if raw >= 2**31 else raw


def _resolve_word_size(die_id: str, dies_by_id: Mapping[str, ET.Element], depth: int = 0) -> int:
    """
    Follows `DW_AT_type` references (typedefs/pointers/qualifiers wrap their
    underlying type rather than repeating `DW_AT_byte_size`) until one is found.
    On this word-addressed target, `DW_AT_byte_size` is already in words (e.g.
    `bool` reports 1, matching `sizeof(bool) == 1` on C2000).
    """
    if depth > 8:
        return 1
    die = dies_by_id.get(die_id)
    if die is None:
        return 1
    byte_size = _attr_const(die, "DW_AT_byte_size")
    if byte_size is not None:
        return max(1, byte_size)
    type_idref = _attr_type_idref(die)
    if type_idref is None:
        return 1
    return _resolve_word_size(type_idref, dies_by_id, depth + 1)


def _collect_variables(
    subprogram_die: ET.Element,
    dies_by_id: Mapping[str, ET.Element],
) -> tuple[LocalVariable, ...]:
    variables: list[LocalVariable] = []
    for die in subprogram_die.iter("die"):
        tag = die.find("tag")
        if tag is None or tag.text not in ("DW_TAG_variable", "DW_TAG_formal_parameter"):
            continue
        name = _attr_string(die, "DW_AT_name")
        offset = _stack_offset(die)
        if name is None or offset is None:
            continue
        type_idref = _attr_type_idref(die)
        size = _resolve_word_size(type_idref, dies_by_id) if type_idref is not None else 1
        variables.append(LocalVariable(name=name, offset=offset, size=size))
    return tuple(variables)


def _build_function_scopes(root: ET.Element) -> tuple[FunctionScope, ...]:
    dies_by_id: dict[str, ET.Element] = {}
    for die in root.iter("die"):
        die_id = die.get("id")
        if die_id:
            dies_by_id[die_id] = die

    scopes: list[FunctionScope] = []
    for die in root.iter("die"):
        tag = die.find("tag")
        if tag is None or tag.text != "DW_TAG_subprogram":
            continue
        name = _attr_string(die, "DW_AT_name")
        low_pc = _attr_addr(die, "DW_AT_low_pc")
        high_pc = _attr_addr(die, "DW_AT_high_pc")
        if name is None or low_pc is None or high_pc is None:
            continue
        scopes.append(
            FunctionScope(
                name=name,
                low_pc=low_pc,
                high_pc=high_pc,
                variables=_collect_variables(die, dies_by_id),
            ),
        )
    return tuple(scopes)


def _dump_dwarf_xml(ofd_path: Path, out_path: Path) -> ET.Element:
    proc = subprocess.run(  # noqa: S603
        [str(ofd_path), "--dwarf", "--xml", str(out_path)],
        check=True,
        capture_output=True,
    )
    return ET.fromstring(proc.stdout)  # noqa: S314 - our own ofd2000, on a locally-built .out


@lru_cache(maxsize=4)
def _load_function_scopes(
    out_path: Path,
    out_mtime: float,
    ofd_path: Path,
) -> tuple[FunctionScope, ...]:
    _ = out_mtime  # part of the cache key only - invalidates the cache on rebuild
    return _build_function_scopes(_dump_dwarf_xml(ofd_path, out_path))


def load_function_scopes(out_path: Path, ofd_path: Path | None = None) -> tuple[FunctionScope, ...]:
    """
    Loads every function's stack-resident locals from `out_path`'s DWARF debug
    info. Cached, and automatically refreshed whenever `out_path`'s mtime changes
    (e.g. after a rebuild/reflash).
    """
    ofd_path = ofd_path or get_ofd_path_from_env()
    return _load_function_scopes(out_path, out_path.stat().st_mtime, ofd_path)


def local_variable_at(
    scopes: Sequence[FunctionScope],
    pc: int,
    sp: int,
    address: int,
) -> str | None:
    """
    Name of the local variable/parameter (if any) covering `address`, given the
    function scope containing `pc` and the current stack pointer `sp` - `address`
    is typically one word of a `mem` dump anchored at `sp`. `None` both when `pc`
    falls in no known scope and when it does but nothing there covers `address`
    (e.g. `address` isn't on the stack at all).
    """
    for scope in scopes:
        if scope.contains(pc):
            return scope.variable_at(sp, address)
    return None
