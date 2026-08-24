"""
Implements the command-line logic to invoke
the DSS script for CCS studio

@date: 21.08.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations
from typing import TypedDict

import os
import platform
import shutil
import subprocess
from pathlib import Path

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
            f"DSS location configured as {dss_location}, but file does not exist"
        )
    stdout, _ = invoke_dss(dss_location, str(get_script_by_name("get_info")))
    return parse_info(stdout)
