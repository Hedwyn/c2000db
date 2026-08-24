/*
 * noop.js -- extracts base information from the DSS environment. 
 *
 * Touches no target and opens no debug session. Its only job is to prove that
 * `dss.sh|bat` is reachable for the invoking user and to report the CCS install it
 * resolved to, so the Python layer can validate the toolchain up front.
 *
 * Output is one KEY=VALUE per line on stdout, terminated by DSS_NOOP=OK.
 * Anything else (or a non-zero exit) means the environment is unusable.
 *
 * Usage: dss.sh noop.js
 */

importPackage(Packages.com.ti.ccstudio.scripting.environment);

var File = java.io.File;
var System = java.lang.System;

function emit(key, value) {
    print(key + "=" + value);
}

function fail(message) {
    emit("DSS_ERROR", message);
    quit(1);
}

/* --- CCS install discovery -------------------------------------------------
 * There is no version accessor on the scripting API (ScriptingEnvironment only
 * exposes directory/trace/timeout helpers), so the version has to be read off
 * the install. dss.sh exports a CLASSPATH pointing at
 * <ccs>/ccs_base/DebugServer/packages/ti/dss/java/*.jar, which is the most
 * direct anchor we get. java.home (<ccs>/eclipse/jre) is the fallback, and a
 * PATH scan for dss.sh itself is the last resort.
 */

function ccsRootFromClasspath() {
    var cp = System.getenv("CLASSPATH");
    if (cp === null) {
        return null;
    }
    var match = String(cp).match(/(.*?)\/ccs_base\/DebugServer\//);
    return match ? match[1] : null;
}

function ccsRootFromJavaHome() {
    // <ccs>/eclipse/jre, or <ccs>/ccs_base/../jre depending on the layout.
    var dir = new File(System.getProperty("java.home"));
    for (var depth = 0; depth < 4 && dir !== null; depth++) {
        if (new File(dir, "eclipse/ccs.properties").isFile()) {
            return String(dir.getCanonicalPath());
        }
        dir = dir.getParentFile();
    }
    return null;
}

function ccsRootFromPath() {
    var path = System.getenv("PATH");
    if (path === null) {
        return null;
    }
    var entries = String(path).split(File.pathSeparator);
    for (var i = 0; i < entries.length; i++) {
        var dss = new File(entries[i], "dss.sh");
        if (!dss.isFile()) {
            continue;
        }
        // <ccs>/ccs_base/scripting/bin/dss.sh -> up four levels is <ccs>.
        var dir = dss.getCanonicalFile().getParentFile();
        for (var up = 0; up < 3 && dir !== null; up++) {
            dir = dir.getParentFile();
        }
        if (dir !== null && new File(dir, "eclipse/ccs.properties").isFile()) {
            return String(dir.getCanonicalPath());
        }
    }
    return null;
}

function readProperties(file) {
    var props = new java.util.Properties();
    var stream = new java.io.FileInputStream(file);
    try {
        props.load(stream);
    } finally {
        stream.close();
    }
    return props;
}

var env = ScriptingEnvironment.instance();

var ccsRoot = ccsRootFromClasspath() || ccsRootFromJavaHome() || ccsRootFromPath();
if (ccsRoot === null) {
    fail("cannot locate CCS install from CLASSPATH, java.home or PATH");
}

var properties = new File(ccsRoot, "eclipse/ccs.properties");
if (!properties.isFile()) {
    fail("no ccs.properties under " + ccsRoot);
}

var ccs = readProperties(properties);
var buildId = ccs.getProperty("ccs_buildid");
if (buildId === null) {
    fail("ccs.properties carries no ccs_buildid");
}
buildId = String(buildId);

emit("CCS_ROOT", ccsRoot);
emit("CCS_INSTALL_DIR", ccs.getProperty("installdir"));
emit("CCS_BUILDID", buildId);
// Marketing version: 20.5.0.00028 -> 20.5.0
emit("CCS_VERSION", buildId.split(".").slice(0, 3).join("."));
emit("DEBUGSERVER_ROOT", ccsRoot + "/ccs_base/DebugServer");
emit("SCRIPTING_DOCS", ccsRoot + "/ccs_base/scripting/docs/DS_API");

emit("JAVA_HOME", System.getProperty("java.home"));
emit("JAVA_VERSION", System.getProperty("java.version"));
emit("RHINO_VERSION", Packages.org.mozilla.javascript.Context.getCurrentContext().getImplementationVersion());
emit("WITHIN_CCS", env.isWithinCCS());
emit("CWD", env.getCurrentDirectory());
emit("SCRIPT_ARGC", typeof arguments === "undefined" ? 0 : arguments.length);

emit("DSS_NOOP", "OK");
