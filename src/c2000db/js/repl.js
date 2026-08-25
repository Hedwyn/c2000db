// Interactive command-line debugger for fw-boot, driven through TI's headless
// Debug Server Scripting (DSS) engine - no CCS IDE required.
//
// Usage (from the repo root, so the relative paths below resolve):
//   <ccs_install>/ccs/ccs_base/scripting/bin/dss.sh debug/repl.js
//
// Set DSS_COMMANDS to the path of a script of debugger commands (one per
// line, blank lines and lines starting with '#' ignored) to run before
// dropping into the interactive prompt - same idea as `gdb -x`. A `quit`
// command in the script skips the interactive prompt entirely.

importPackage(Packages.com.ti.debug.engine.scripting);
importPackage(Packages.com.ti.ccstudio.scripting.environment);
importPackage(Packages.java.lang);
importPackage(Packages.java.io);

var CCXML = java.lang.System.getenv("DSS_CCXML");
if (!CCXML) {
    print("error: DSS_CCXML env var not set");
    java.lang.System.exit(1);
}
var DEFAULT_OUT = "build/fw-boot.out";
var COMMANDS_PATH = java.lang.System.getenv("DSS_COMMANDS");

// Printed as its own line right after every command's output, so a stdout-only
// observer (see interface.py's _pump_and_parse_stdout) can tell exactly where one
// command's output ends, without waiting for the next prompt to show up. Must match
// interface.py's END_OF_COMMAND_MARKER.
var END_OF_COMMAND_MARKER = "##--dbg-end--##";

var env = ScriptingEnvironment.instance();
env.traceSetConsoleLevel(TraceLevel.INFO);
env.setScriptTimeout(60000);

var server = env.getServer("DebugServer.1");
server.setConfig(CCXML);
var session = server.openSession(".*C28xx_CPU1");

print("Connecting to target via " + CCXML + " ...");
session.target.connect();
print("Connected.");

var stdin = new BufferedReader(new InputStreamReader(System["in"]));

function hex(n) {
    return "0x" + Long.toHexString(n);
}

function page(name) {
    if (name === "prog" || name === "program") {
        return Memory.Page.PROGRAM;
    }
    return Memory.Page.DATA;
}

function parseAddress(token) {
    return java.lang.Long.decode(token).longValue();
}

function printLocation() {
    var pc = session.expression.evaluate("PC");
    var sym = session.symbol.lookupSymbol(Memory.Page.PROGRAM, pc);
    print("PC = " + hex(pc) + (sym ? " (" + sym + ")" : ""));
}

function help() {
    print([
        "connect | disconnect",
        "load [out]                 - load symbols only, default " + DEFAULT_OUT,
        "flash [out]                - reprogram device + load symbols (session.memory.loadProgram)",
        "halt | run/go/continue/c | reset | restart",
        "fire                        - like run, but doesn't read PC/location back (for timing tests)",
        "step/si | next/over        - single asm instruction step (into / over a call)",
        "break <symbol|0xADDR>      - set breakpoint, prints its id",
        "delete <id> | clearbp",
        "print <expr>                - evaluate a C expression / register (e.g. PC, ACC, a global)",
        "mem <0xADDR> <count> [prog|data]  - dump <count> 16-bit words (default page: data)",
        "write <0xADDR> <0xVALUE>   - write one 16-bit word (data page)",
        "bt                          - print call stack",
        "help | quit",
    ].join("\n"));
}

// Runs a single command line. Returns false if the REPL should terminate
// (e.g. on `quit`), true otherwise. Used for both the interactive prompt and
// commands read from a DSS_COMMANDS script.
function execCommand(line) {
    line = ("" + line).replace(/^\s+|\s+$/g, "");
    if (line === "") {
        return true;
    }
    var parts = line.split(/\s+/);
    var cmd = parts[0];

    try {
        switch (cmd) {
            case "help":
                help();
                break;
            case "connect":
                session.target.connect();
                print("connected");
                break;
            case "disconnect":
                session.target.disconnect();
                print("disconnected");
                break;
            case "load":
                session.symbol.load(parts[1] || DEFAULT_OUT);
                print("symbols loaded from " + (parts[1] || DEFAULT_OUT));
                break;
            case "flash":
                session.memory.loadProgram(parts[1] || DEFAULT_OUT);
                print("flashed + symbols loaded from " + (parts[1] || DEFAULT_OUT));
                break;
            case "halt":
                session.target.halt();
                printLocation();
                break;
            case "fire":
                session.target.run();
                print("fired, not reading location");
                break;
            case "run":
            case "go":
            case "continue":
            case "c":
                var runStart = java.lang.System.currentTimeMillis();
                session.target.run();
                var runElapsed = java.lang.System.currentTimeMillis() - runStart;
                print("halted after " + runElapsed + "ms:");
                printLocation();
                break;
            case "reset":
                session.target.reset();
                print("reset");
                break;
            case "restart":
                session.target.restart();
                printLocation();
                break;
            case "step":
            case "si":
                session.target.asmStep.into();
                printLocation();
                break;
            case "over":
            case "next":
                session.target.asmStep.over();
                printLocation();
                break;
            case "break":
            case "b":
                var target = parts[1];
                var id = target.indexOf("0x") === 0
                    ? session.breakpoint.add(parseAddress(target))
                    : session.breakpoint.add(target);
                print("breakpoint " + id + " at " + target);
                break;
            case "delete":
                session.breakpoint.remove(parseInt(parts[1], 10));
                print("removed breakpoint " + parts[1]);
                break;
            case "clearbp":
                session.breakpoint.removeAll();
                print("all breakpoints cleared");
                break;
            case "print":
            case "p":
                var expr = parts.slice(1).join(" ");
                var val = session.expression.evaluate(expr);
                print(expr + " = " + val + " (" + hex(val) + ")");
                break;
            case "mem":
                var addr = parseAddress(parts[1]);
                var count = parseInt(parts[2] || "1", 10);
                var pg = page(parts[3]);
                var data = session.memory.readData(pg, addr, 16, count);
                for (var i = 0; i < data.length; i++) {
                    print(hex(addr + i) + ": " + hex(data[i]));
                }
                break;
            case "write":
                var waddr = parseAddress(parts[1]);
                var wval = parseAddress(parts[2]);
                session.memory.writeData(Memory.Page.DATA, waddr, wval, 16);
                print("wrote " + hex(wval) + " to " + hex(waddr));
                break;
            case "bt":
                print(session.callStack.getStackTrace());
                break;
            case "quit":
            case "exit":
            case "q":
                return false;
            default:
                print("unknown command: " + cmd + " (try 'help')");
        }
    } catch (e) {
        print("error: " + e);
    }
    return true;
}

// Runs the commands found in the script at `path`, one per line. Blank lines
// and lines starting with '#' are skipped. Returns false if a `quit`
// command was hit (caller should not enter the interactive prompt).
function runCommandScript(path) {
    print("Running commands from " + path + " ...");
    var reader = new BufferedReader(new FileReader(path));
    try {
        var line;
        while ((line = reader.readLine()) !== null) {
            var trimmed = ("" + line).replace(/^\s+|\s+$/g, "");
            if (trimmed === "" || trimmed.indexOf("#") === 0) {
                continue;
            }
            print("dbg> " + trimmed);
            var ok = execCommand(trimmed);
            print(END_OF_COMMAND_MARKER);
            if (!ok) {
                return false;
            }
        }
    } finally {
        reader.close();
    }
    return true;
}

help();

var keepGoing = true;
if (COMMANDS_PATH) {
    keepGoing = runCommandScript(COMMANDS_PATH);
}

while (keepGoing) {
    java.lang.System.out.print("dbg> ");
    java.lang.System.out.flush();
    var line = stdin.readLine();
    if (line === null) {
        break;
    }
    // Echoed as its own "dbg> <line>" line (like runCommandScript does) so a
    // stdout-only observer can always tell which command produced what output,
    // whether it was typed interactively or came from a DSS_COMMANDS script.
    print("dbg> " + line);
    keepGoing = execCommand(line);
    print(END_OF_COMMAND_MARKER);
}

print("Disconnecting...");
try {
    session.target.disconnect();
} catch (e) {
    // already disconnected / target gone - fine on the way out
}
session.terminate();
server.stop();
print("Bye.");
