from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "uxp" / "photoshop-bridge"
RUNNER = ROOT / "uxp" / "photoshop-bridge" / "src" / "batchplay-runner.js"
SCHEMA = ROOT / "examples" / "photoshop_bridge" / "protocols" / "modal_execution.v1.schema.json"


class PhotoshopUxpModalEnvelopeTests(unittest.TestCase):
    def test_plugin_uses_commonjs_and_allows_both_loopback_hosts(self) -> None:
        manifest = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
        domains = set(manifest["requiredPermissions"]["network"]["domains"])
        self.assertEqual(
            {
                "ws://localhost:8971",
                "http://localhost:8971",
                "ws://127.0.0.1:8971",
                "http://127.0.0.1:8971",
            },
            domains,
        )

        html = (PLUGIN / "index.html").read_text(encoding="utf-8")
        self.assertIn('<script src="index.js"></script>', html)
        self.assertNotIn('type="module"', html)

        sources = {"index.js": (PLUGIN / "index.js").read_text(encoding="utf-8")}
        sources.update(
            {
                path.name: path.read_text(encoding="utf-8")
                for path in sorted((PLUGIN / "src").glob("*.js"))
            }
        )
        for name, source in sources.items():
            self.assertNotRegex(
                source,
                r"(?m)^\s*(?:import|export)\b",
                msg=f"{name} must use the UXP CommonJS module contract",
            )
        self.assertIn('require("./src/bridge-client.js")', sources["index.js"])
        self.assertIn('require("./src/batchplay-runner.js")', sources["index.js"])
        for name in ("bridge-client.js", "batchplay-runner.js", "batchplay-schema.js"):
            self.assertIn("module.exports", sources[name])

    def test_bridge_client_prefers_localhost_then_falls_back_to_ipv4(self) -> None:
        client_source = PLUGIN / "src" / "bridge-client.js"
        script = f"""
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync({json.dumps(str(client_source))}, "utf8");
const sockets = [];
const timers = [];
class FakeWebSocket {{
  constructor(url) {{
    this.url = url;
    this.listeners = {{}};
    this.readyState = 0;
    this.sent = [];
    sockets.push(this);
  }}
  addEventListener(name, handler) {{
    this.listeners[name] = this.listeners[name] || [];
    this.listeners[name].push(handler);
  }}
  emit(name, event = {{}}) {{
    for (const handler of this.listeners[name] || []) handler(event);
  }}
  close() {{
    this.readyState = 3;
  }}
  send(payload) {{
    this.sent.push(JSON.parse(payload));
  }}
}}
const moduleRecord = {{ exports: {{}} }};
const context = {{
  module: moduleRecord,
  exports: moduleRecord.exports,
  require: (name) => {{
    if (name !== "photoshop") throw new Error(`unexpected_module:${{name}}`);
    return {{ app: {{ version: "test" }} }};
  }},
  WebSocket: FakeWebSocket,
  setTimeout: (handler) => {{
    timers.push(handler);
    return timers.length;
  }},
  clearTimeout: () => {{}},
}};
vm.createContext(context);
vm.runInContext(source, context, {{ filename: "bridge-client.js" }});
const {{ BridgeClient, DEFAULT_PROXY_URLS }} = moduleRecord.exports;
const statuses = [];
const client = new BridgeClient({{ onStatus: (status) => statuses.push(status) }});
client.connect();
sockets[0].emit("error");
sockets[0].emit("close");
sockets[1].readyState = 1;
sockets[1].emit("open");
const configured = new BridgeClient({{ proxyUrl: "ws://localhost:9999/custom" }});
process.stdout.write(JSON.stringify({{
  defaultUrls: DEFAULT_PROXY_URLS,
  socketUrls: sockets.map((socket) => socket.url),
  statuses,
  registered: sockets[1].sent[0],
  configuredProxyUrl: configured.proxyUrl,
  timerCount: timers.length,
}}));
"""
        process = subprocess.run(
            ["node", "--input-type=commonjs", "-e", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(process.stdout)

        self.assertEqual(
            ["ws://localhost:8971/uxp", "ws://127.0.0.1:8971/uxp"],
            payload["defaultUrls"],
        )
        self.assertEqual(payload["defaultUrls"], payload["socketUrls"])
        self.assertEqual("connected", payload["statuses"][-1])
        self.assertEqual("register", payload["registered"]["type"])
        self.assertEqual("ws://localhost:9999/custom", payload["configuredProxyUrl"])
        self.assertEqual(0, payload["timerCount"])

    def test_schema_is_closed_and_has_bounded_terminal_states(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            {"completed", "cancelled", "failed"},
            set(schema["properties"]["status"]["enum"]),
        )
        timeout = schema["properties"]["timeout_seconds"]
        self.assertEqual(1, timeout["minimum"])
        self.assertEqual(30, timeout["maximum"])
        self.assertFalse(schema["properties"]["history"]["additionalProperties"])
        self.assertIn("required_for_write", schema["properties"]["history"]["required"])

    def test_runner_uses_timeout_cancellation_and_explicit_history_outcome(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")

        for required in (
            "timeOut: timeoutSeconds",
            "executionContext?.isCancelled",
            "hostControl.suspendHistory",
            "hostControl.resumeHistory",
            "resumeHistory(suspension, true)",
            "resumeHistory(suspension, false)",
            '"cancelled"',
            '"failed"',
            'status: "completed"',
            'MODAL_SCHEMA_VERSION = "starbridge.photoshop-modal.v1"',
            "schema_version: MODAL_SCHEMA_VERSION",
        ):
            self.assertIn(required, source)

    def test_runner_reports_commit_rollback_cancel_and_redacts_paths(self) -> None:
        script = f"""
const fs = require("node:fs");
const calls = {{ options: [], resumes: [] }};
const executionContext = {{
  isCancelled: false,
  hostControl: {{
    suspendHistory: async (options) => ({{ id: "history-1", ...options }}),
    resumeHistory: async (suspension, commit) => calls.resumes.push(commit),
  }},
}};
const photoshopStub = {{
  action: {{ batchPlay: async () => [] }},
  app: {{ activeDocument: {{ id: 42 }} }},
  core: {{
    executeAsModal: async (handler, options) => {{
      calls.options.push(options);
      return handler(executionContext);
    }},
  }},
}};
const moduleRecord = {{ exports: {{}} }};
const moduleRequire = (name) => {{
  if (name === "./batchplay-schema.js") return {{ validateDescriptor: () => ({{ allowed: true }}) }};
  if (name !== "photoshop") throw new Error("unexpected_module");
  return photoshopStub;
}};
new Function("require", "module", "exports", fs.readFileSync({json.dumps(str(RUNNER))}, "utf8"))(
  moduleRequire,
  moduleRecord,
  moduleRecord.exports,
);
const runner = moduleRecord.exports;
(async () => {{
  const success = await runner.runModalJob(
    "ps.test.write",
    {{ commandName: "Test Commit", timeoutSeconds: 7 }},
    async () => ({{ warnings: ["C:/Users/<USER_HOME>/source.psd"] }}),
  );
  const failed = await runner.runModalJob(
    "ps.test.write",
    {{ commandName: "Test Rollback" }},
    async () => {{ throw new Error("C:/Users/<USER_HOME>/source.psd failed"); }},
  );
  const cancelled = await runner.runModalJob(
    "ps.test.write",
    {{ commandName: "Test Cancel" }},
    async (context) => {{ context.isCancelled = true; throw new Error("user_cancelled"); }},
  );
  process.stdout.write(JSON.stringify({{ success, failed, cancelled, calls }}));
}})().catch((error) => {{
  console.error(error);
  process.exitCode = 1;
}});
"""
        process = subprocess.run(
            ["node", "--input-type=commonjs", "-e", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(process.stdout)

        self.assertEqual("completed", payload["success"]["modal"]["status"])
        self.assertTrue(payload["success"]["modal"]["history"]["committed"])
        self.assertTrue(payload["success"]["modal"]["history"]["required_for_write"])
        self.assertEqual(7, payload["success"]["modal"]["timeout_seconds"])
        self.assertEqual(7, payload["calls"]["options"][0]["timeOut"])
        self.assertIn("<redacted-path>", payload["success"]["warnings"][0])
        self.assertEqual("failed", payload["failed"]["modal"]["status"])
        self.assertTrue(payload["failed"]["modal"]["history"]["rolled_back"])
        self.assertIn("<redacted-path>", payload["failed"]["errors"][0]["message"])
        self.assertEqual("cancelled", payload["cancelled"]["modal"]["status"])
        self.assertTrue(payload["cancelled"]["modal"]["cancelled"])

    def test_required_history_fails_closed_before_handler_or_completion(self) -> None:
        script = f"""
const fs = require("node:fs");
let handlerCalledWithoutControl = false;
const contexts = [
  {{ isCancelled: false, hostControl: {{}} }},
  {{
    isCancelled: false,
    hostControl: {{
      suspendHistory: async (options) => ({{ id: "history-1", ...options }}),
      resumeHistory: async () => {{}},
    }},
  }},
];
const photoshopStub = {{
  action: {{ batchPlay: async () => [] }},
  app: {{ activeDocument: {{ id: 42 }} }},
  core: {{ executeAsModal: async (handler) => handler(contexts.shift()) }},
}};
const moduleRecord = {{ exports: {{}} }};
const moduleRequire = (name) => {{
  if (name === "./batchplay-schema.js") return {{ validateDescriptor: () => ({{ allowed: true }}) }};
  if (name === "photoshop") return photoshopStub;
  throw new Error(`unexpected_module:${{name}}`);
}};
new Function("require", "module", "exports", fs.readFileSync({json.dumps(str(RUNNER))}, "utf8"))(
  moduleRequire,
  moduleRecord,
  moduleRecord.exports,
);
const runner = moduleRecord.exports;
(async () => {{
  const unsupported = await runner.runModalJob(
    "ps.test.write",
    {{ historyTarget: "active_document" }},
    async () => {{ handlerCalledWithoutControl = true; return {{}}; }},
  );
  const omitted = await runner.runModalJob(
    "ps.test.write",
    {{ historyTarget: "handler_document" }},
    async () => ({{}}),
  );
  process.stdout.write(JSON.stringify({{ unsupported, omitted, handlerCalledWithoutControl }}));
}})().catch((error) => {{
  console.error(error);
  process.exitCode = 1;
}});
"""
        process = subprocess.run(
            ["node", "--input-type=commonjs", "-e", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        payload = json.loads(process.stdout)

        self.assertFalse(payload["handlerCalledWithoutControl"])
        for key in ("unsupported", "omitted"):
            result = payload[key]
            self.assertFalse(result["success"])
            self.assertEqual("failed", result["modal"]["status"])
            self.assertTrue(result["modal"]["history"]["required_for_write"])
            self.assertEqual("history_control_failed", result["errors"][0]["code"])

    def test_runner_redacts_error_paths_and_keeps_batchplay_on_sandbox_copy(self) -> None:
        source = RUNNER.read_text(encoding="utf-8")

        self.assertIn("redacted-path", source)
        self.assertIn("document.duplicate", source)
        self.assertIn("registerAutoCloseDocument", source)
        self.assertIn("unregisterAutoCloseDocument", source)
        self.assertIn('historyTarget: "handler_document"', source)
        self.assertNotIn("synchronousExecution: true", source)


if __name__ == "__main__":
    unittest.main()
