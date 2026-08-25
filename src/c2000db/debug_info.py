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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

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

# A struct/union member's location, e.g. "DW_OP_plus_uconst 0x2" - confirmed
# against a real build's DWARF dump: always this opcode, offset in words (this
# target is word-addressed), rendered as bare "0" for offset 0 and "0x.." for
# anything else - `int(x, 0)` below auto-detects either form.
_MEMBER_OFFSET_RE = re.compile(r"^DW_OP_plus_uconst (0x[0-9a-fA-F]+|\d+)$")

# A global/file-static variable's location, e.g. "DW_OP_addr 0xc2ee" - a fixed
# absolute address, unlike a stack local's SP-relative `DW_OP_bregN`.
_GLOBAL_ADDRESS_RE = re.compile(r"^DW_OP_addr (0x[0-9a-fA-F]+)$")

# Every pointer DIE observed in a real build carries `DW_AT_address_class =
# DW_ADDR_TI_PTR32` and no `DW_AT_byte_size` of its own - a 32-bit/2-word far
# pointer is this target's only address class, so it's used unconditionally
# rather than dispatched on a class we've never actually seen.
_POINTER_WORD_SIZE = 2

# Qualifier/alias DIEs that wrap another type without affecting its layout -
# transparently unwrapped by `resolve_type` rather than modelled as types of
# their own.
_TRANSPARENT_WRAPPER_TAGS = frozenset(
    {
        "DW_TAG_typedef",
        "DW_TAG_const_type",
        "DW_TAG_volatile_type",
        "DW_TAG_TI_far_type",
    },
)


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


@dataclass(frozen=True)
class BaseType:
    """
    A DWARF base type (int, float, bool, ...) - also the fallback `resolve_type`
    returns for any DIE tag it doesn't model further (e.g. `DW_TAG_subroutine_type`,
    a function pointer's target: its address is meaningful, its "value" isn't).
    """

    name: str
    size: int
    encoding: int | None = None


@dataclass(frozen=True)
class PointerType:
    """
    A pointer - `pointee_id` is the DWARF id of the pointee's type DIE, *not*
    eagerly resolved into a `Type`: a self-referential struct (e.g. a linked-list
    node pointing at its own type) would otherwise recurse forever. Resolve it
    on demand with `resolve_type(pointee_id, ...)` when actually dereferencing.
    """

    pointee_id: str | None
    size: int


@dataclass(frozen=True)
class ArrayType:
    """
    An array - `counts` holds every dimension in declaration order (a DWARF
    array DIE carries one `DW_TAG_subrange_type` child per dimension, e.g.
    `int a[2][3]` is one array DIE with two subrange children), so a
    multi-dimensional array is one flat `ArrayType`, not nested ones.
    """

    element: Type
    counts: tuple[int, ...]
    size: int


@dataclass(frozen=True)
class EnumType:
    name: str | None
    size: int
    values: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class Member:
    name: str
    offset: int
    type: Type


@dataclass(frozen=True)
class CompositeType:
    """
    A struct or union. `kind` only matters for display - a union's members
    already encode the difference by all sitting at offset 0.
    """

    name: str | None
    kind: Literal["struct", "union"]
    size: int
    members: tuple[Member, ...]


type Type = BaseType | PointerType | ArrayType | EnumType | CompositeType


def _direct_children(die: ET.Element, tag_name: str) -> Iterator[ET.Element]:
    """
    Yields `die`'s immediate `<die>` children whose tag is `tag_name` - unlike
    `die.iter("die")`, this doesn't also descend into a nested composite type's
    own children (e.g. a struct member that is itself a struct).
    """
    for child in die.findall("die"):
        child_tag = child.find("tag")
        if child_tag is not None and child_tag.text == tag_name:
            yield child


def _member_offset(die: ET.Element) -> int | None:
    value = _die_value(die, "DW_AT_data_member_location")
    block_el = value.find("block") if value is not None else None
    if block_el is None or not block_el.text:
        return None
    match = _MEMBER_OFFSET_RE.match(block_el.text.strip())
    return int(match[1], 0) if match is not None else None


def _resolve_referenced_type(
    die: ET.Element,
    dies_by_id: Mapping[str, ET.Element],
    cache: dict[str, Type],
) -> Type:
    """Resolves `die`'s `DW_AT_type`, defaulting to `void` if it has none (e.g. a bare `void *`)."""
    type_idref = _attr_type_idref(die)
    if type_idref is None:
        return BaseType(name="void", size=0)
    return resolve_type(type_idref, dies_by_id, cache)


def resolve_type(die_id: str, dies_by_id: Mapping[str, ET.Element], cache: dict[str, Type]) -> Type:
    """
    Resolves the DWARF type DIE `die_id` into a `Type` tree, memoizing into
    `cache` (share one `cache` across a whole compile unit - types are reused
    across many variables). See `PointerType` for why pointees aren't eagerly
    resolved.
    """
    if die_id in cache:
        return cache[die_id]
    die = dies_by_id.get(die_id)
    if die is None:
        return BaseType(name="<unknown>", size=1)
    tag = die.find("tag")
    tag_name = tag.text if tag is not None else None

    resolved: Type
    if tag_name in _TRANSPARENT_WRAPPER_TAGS:
        resolved = _resolve_referenced_type(die, dies_by_id, cache)
    elif tag_name == "DW_TAG_pointer_type":
        resolved = PointerType(pointee_id=_attr_type_idref(die), size=_POINTER_WORD_SIZE)
    elif tag_name == "DW_TAG_array_type":
        counts = tuple(
            (_attr_const(subrange, "DW_AT_upper_bound") or 0) + 1
            for subrange in _direct_children(die, "DW_TAG_subrange_type")
        )
        resolved = ArrayType(
            element=_resolve_referenced_type(die, dies_by_id, cache),
            counts=counts,
            size=_attr_const(die, "DW_AT_byte_size") or 0,
        )
    elif tag_name in ("DW_TAG_structure_type", "DW_TAG_union_type"):
        members = tuple(
            Member(
                name=_attr_string(member_die, "DW_AT_name") or "",
                offset=_member_offset(member_die) or 0,
                type=_resolve_referenced_type(member_die, dies_by_id, cache),
            )
            for member_die in _direct_children(die, "DW_TAG_member")
        )
        resolved = CompositeType(
            name=_attr_string(die, "DW_AT_name"),
            kind="union" if tag_name == "DW_TAG_union_type" else "struct",
            size=_attr_const(die, "DW_AT_byte_size") or 0,
            members=members,
        )
    elif tag_name == "DW_TAG_enumeration_type":
        resolved = EnumType(
            name=_attr_string(die, "DW_AT_name"),
            size=_attr_const(die, "DW_AT_byte_size") or 0,
            values=tuple(
                (
                    _attr_const(enumerator, "DW_AT_const_value") or 0,
                    _attr_string(enumerator, "DW_AT_name") or "",
                )
                for enumerator in _direct_children(die, "DW_TAG_enumerator")
            ),
        )
    elif tag_name == "DW_TAG_base_type":
        resolved = BaseType(
            name=_attr_string(die, "DW_AT_name") or "<base>",
            size=_attr_const(die, "DW_AT_byte_size") or 1,
            encoding=_attr_const(die, "DW_AT_encoding"),
        )
    elif tag_name == "DW_TAG_unspecified_type":
        resolved = BaseType(name=_attr_string(die, "DW_AT_name") or "void", size=0)
    else:
        # DW_TAG_subroutine_type, DW_TAG_TI_reserved_, or anything else not
        # modelled above - see BaseType's docstring.
        resolved = BaseType(name=tag_name or "<unknown>", size=1)

    cache[die_id] = resolved
    return resolved


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
    type: Type
    size: int


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


@dataclass(frozen=True)
class GlobalVariable:
    """
    A global/file-static variable, at a fixed `address` (no SP involved). Also
    covers a `static` local declared inside a function - DWARF gives it the same
    `DW_OP_addr` location as a true global, and this module doesn't track C's
    file/function name-scoping, so it's found the same way, by bare name, from
    anywhere; two same-named statics in different functions would collide here.
    """

    name: str
    address: int
    type: Type
    size: int


@dataclass(frozen=True)
class DebugInfo:
    scopes: tuple[FunctionScope, ...]
    globals: tuple[GlobalVariable, ...]
    # Kept around (not just discarded after building `scopes`/`globals`)
    # specifically so `resolve_pointee` can resolve a `PointerType.pointee_id`
    # on demand - see `PointerType`'s docstring for why that's deferred at all.
    _dies_by_id: Mapping[str, ET.Element]
    _type_cache: dict[str, Type]

    def resolve_pointee(self, pointer: PointerType) -> Type | None:
        """The pointee's `Type`, or `None` for a `pointer` with no pointee DIE (e.g. `void *`)."""
        if pointer.pointee_id is None:
            return None
        return resolve_type(pointer.pointee_id, self._dies_by_id, self._type_cache)


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


def _global_address(die: ET.Element) -> int | None:
    value = _die_value(die, "DW_AT_location")
    block_el = value.find("block") if value is not None else None
    if block_el is None or not block_el.text:
        return None
    match = _GLOBAL_ADDRESS_RE.match(block_el.text.strip())
    return int(match[1], 16) if match is not None else None


def _collect_variables(
    subprogram_die: ET.Element,
    dies_by_id: Mapping[str, ET.Element],
    cache: dict[str, Type],
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
        var_type = _resolve_referenced_type(die, dies_by_id, cache)
        variables.append(LocalVariable(name=name, offset=offset, type=var_type, size=var_type.size))
    return tuple(variables)


def _collect_globals(
    root: ET.Element,
    dies_by_id: Mapping[str, ET.Element],
    cache: dict[str, Type],
) -> tuple[GlobalVariable, ...]:
    """
    Walks *every* `DW_TAG_variable` DIE in the compile unit (not just
    subprogram-nested ones, unlike `_collect_variables`) and keeps the ones with
    a `DW_OP_addr` location - mutually exclusive with `_stack_offset`'s
    `DW_OP_bregN`, so this never double-counts a variable `_collect_variables`
    already picked up as a local.
    """
    global_variables: list[GlobalVariable] = []
    for die in root.iter("die"):
        tag = die.find("tag")
        if tag is None or tag.text != "DW_TAG_variable":
            continue
        name = _attr_string(die, "DW_AT_name")
        address = _global_address(die)
        if name is None or address is None:
            continue
        var_type = _resolve_referenced_type(die, dies_by_id, cache)
        global_variables.append(
            GlobalVariable(name=name, address=address, type=var_type, size=var_type.size)
        )
    return tuple(global_variables)


def _build_debug_info(root: ET.Element) -> DebugInfo:
    dies_by_id: dict[str, ET.Element] = {}
    for die in root.iter("die"):
        die_id = die.get("id")
        if die_id:
            dies_by_id[die_id] = die

    # Shared across the whole compile unit - types (e.g. a common typedef) are
    # declared once in the DWARF and reused by many variables, local or global.
    type_cache: dict[str, Type] = {}

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
                variables=_collect_variables(die, dies_by_id, type_cache),
            ),
        )

    return DebugInfo(
        scopes=tuple(scopes),
        globals=_collect_globals(root, dies_by_id, type_cache),
        _dies_by_id=dies_by_id,
        _type_cache=type_cache,
    )


def _dump_dwarf_xml(ofd_path: Path, out_path: Path) -> ET.Element:
    proc = subprocess.run(  # noqa: S603
        [str(ofd_path), "--dwarf", "--xml", str(out_path)],
        check=True,
        capture_output=True,
    )
    return ET.fromstring(proc.stdout)  # noqa: S314 - our own ofd2000, on a locally-built .out


@lru_cache(maxsize=4)
def _load_debug_info(
    out_path: Path,
    out_mtime: float,
    ofd_path: Path,
) -> DebugInfo:
    _ = out_mtime  # part of the cache key only - invalidates the cache on rebuild
    return _build_debug_info(_dump_dwarf_xml(ofd_path, out_path))


def load_debug_info(out_path: Path, ofd_path: Path | None = None) -> DebugInfo:
    """
    Loads every function's stack-resident locals and every global/file-static
    variable from `out_path`'s DWARF debug info. Cached (one `ofd2000` dump and
    DWARF parse per cache hit, shared between scopes and globals), and
    automatically refreshed whenever `out_path`'s mtime changes (e.g. after a
    rebuild/reflash).
    """
    ofd_path = ofd_path or get_ofd_path_from_env()
    return _load_debug_info(out_path, out_path.stat().st_mtime, ofd_path)


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
