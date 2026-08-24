"""
Implements the command-line logic to invoke
the DSS script for CCS studio

@date: 21.08.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations
from typing import Generator

import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from contextlib import contextmanager

from .ccxml import load_ccxml_config, build_ccxml

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


@contextmanager
def generate_ccxml_from_device_config(config_path: Path) -> Generator[Path, None, None]:
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


def run_repl(dss_location: Path | None = None, ccxml_path: Path | None = None) -> int:
    """
    Runs the interactive REPL script.
    Returns the exit code from the DSS process.
    """
    dss_location = dss_location or get_dss_path_from_env()
    if not dss_location.exists():
        raise FileNotFoundError(
            f"DSS location configured as {dss_location}, but file does not exist",
        )

    script_path = get_script_by_name("repl")
    command = [str(dss_location), str(script_path)]

    env = os.environ.copy()
    if ccxml_path:
        env["DSS_CCXML"] = str(ccxml_path)

    proc = subprocess.run(command, check=False, env=env)  # noqa: S603
    return proc.returncode
