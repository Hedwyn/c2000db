# Installation

```bash
uv pip install git+ssh://git@github.com/Hedwyn/c2000db.git
```

# Usage

```
$ c2000db --help
Usage: c2000db [OPTIONS] COMMAND [ARGS]...

  Main entrypoint for all C2000 debugger commands

Options:
  --help  Show this message and exit.

Commands:
  info                      Shows the info for your DSS install
  interactive-ccxml-config  Interactive helper to set your the device...
  repl
```

# External tools

`c2000db` drives external tools rather than bundling any debugger/compiler
itself. Neither is looked up automatically from a fixed install path - each is
resolved either from an env var, or by basename on `PATH`.

## DSS (required)

Everything (`repl`, `reset`, `info`, ...) is driven through TI's headless
**Debug Server Scripting** engine, bundled with Code Composer Studio (CCS).

- **Get it**: install CCS from [ti.com/tool/CCSTUDIO](https://www.ti.com/tool/CCSTUDIO). DSS ships at
  `<ccs_install>/ccs/ccs_base/scripting/bin/dss.sh` (`dss.bat` on Windows) - no
  separate download.
- **Configure it**: either add that `bin` folder to `PATH`, or set `DSS_PATH`
  to the full path of `dss.sh`/`dss.bat`.
- Missing it is fatal - every command in this package needs it.

## ofd2000 (optional)

The `mem` command's "Variable" row (naming the local variable/parameter, if
any, covering each dumped address in the current frame) is resolved from your
build's DWARF debug info via `ofd2000`, TI's object-file-display tool - part
of the **C2000 code generation tools** (the `ti-cgt-c2000` compiler toolchain),
not CCS/DSS.

- **Get it**: install the C2000 compiler toolchain from
  [ti.com/tool/C2000-CGT](https://www.ti.com/tool/C2000-CGT) (or via `fwmanager`, which caches it under
  `~/.fwmanager/ti_cgt/<version>/ti-cgt-c2000_<version>.LTS/`). The binary is at
  `<cgt_install>/bin/ofd2000`.
- **Configure it**: either add that `bin` folder to `PATH`, or set
  `C2000_OFD_PATH` to the full path of `ofd2000`.
- Missing it is **not** fatal: `mem` still works, its "Variable" row is just
  left empty (with a one-line warning noting the lookup was unavailable).
