// Hard-reset whatever is already flashed and let it run free, over JTAG - no .out file needed.

importPackage(Packages.com.ti.debug.engine.scripting);
importPackage(Packages.com.ti.ccstudio.scripting.environment);
importPackage(Packages.java.lang);

var CCXML = java.lang.System.getenv("DSS_CCXML");
if (!CCXML) {
    print("error: DSS_CCXML env var not set");
    java.lang.System.exit(1);
}
var CODE_START_ENV = java.lang.System.getenv("DSS_START_ADDRESS");
var CODE_START = CODE_START_ENV ? java.lang.Long.decode(CODE_START_ENV).longValue() : 0x080000;

var env = ScriptingEnvironment.instance();
env.traceSetConsoleLevel(TraceLevel.SEVERE);
env.setScriptTimeout(60000);

var server = env.getServer("DebugServer.1");
server.setConfig(CCXML);
var session = server.openSession(".*C28xx_CPU1");

session.target.connect();
session.breakpoint.removeAll();
session.target.reset();

// Numeric literal rather than the code_start symbol: no symbols are loaded here on purpose.
session.expression.evaluate("PC = 0x" + CODE_START.toString(16));
session.target.runAsynch();

print("Reset; running from 0x" + CODE_START.toString(16) + ". Disconnecting with the CPU running.");
try {
    session.target.disconnect();
} catch (e) {
    // already disconnected / target gone - fine on the way out
}
session.terminate();
server.stop();
