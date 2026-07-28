const DEFAULT_PROXY_URLS = Object.freeze([
  "ws://localhost:8971/uxp",
  "ws://127.0.0.1:8971/uxp",
]);

function nowIso() {
  return new Date().toISOString();
}

class BridgeClient {
  constructor({ proxyUrl, handlers = {}, onStatus = () => {}, onSession = () => {} } = {}) {
    const configuredProxyUrl = typeof proxyUrl === "string" ? proxyUrl.trim() : "";
    this.proxyUrls = configuredProxyUrl ? [configuredProxyUrl] : [...DEFAULT_PROXY_URLS];
    this.proxyUrlIndex = 0;
    this.proxyUrl = this.proxyUrls[this.proxyUrlIndex];
    this.handlers = handlers;
    this.onStatus = onStatus;
    this.onSession = onSession;
    this.socket = null;
    this.connected = false;
    this.lastPingAt = null;
    this.reconnectTimer = null;
  }

  connect() {
    if (this.socket) {
      return;
    }
    try {
      this.onStatus("connecting");
      const socket = new WebSocket(this.proxyUrl);
      this.socket = socket;
      let opened = false;
      let finished = false;
      const handleDisconnect = (status) => {
        if (finished || this.socket !== socket) {
          return;
        }
        finished = true;
        this.connected = false;
        this.socket = null;
        try {
          socket.close();
        } catch (_error) {
          // The failed socket may already be closed.
        }
        if (!opened && this.useFallbackProxyUrl()) {
          this.onStatus("connecting");
          this.connect();
          return;
        }
        this.onStatus(status);
        if (!opened) {
          this.resetDefaultProxyUrl();
        }
        this.scheduleReconnect();
      };
      socket.addEventListener("open", () => {
        if (finished || this.socket !== socket) {
          return;
        }
        opened = true;
        this.connected = true;
        this.onStatus("connected");
        this.send({
          type: "register",
          host: "photoshop",
          connectedAt: nowIso(),
          photoshop_host: this.hostInfo(),
        });
      });
      socket.addEventListener("close", () => handleDisconnect("disconnected"));
      socket.addEventListener("message", async (event) => {
        const message = JSON.parse(String(event.data || "{}"));
        if (message?.type === "codex_session") {
          this.onSession(message);
          return;
        }
        if (!message?.method) {
          return;
        }
        const handler = this.handlers[message.method];
        const replyBase = { jsonrpc: "2.0", id: message.id };
        if (!handler) {
          this.send({ ...replyBase, error: { code: -32601, message: `unknown method: ${message.method}` } });
          return;
        }
        try {
          const result = await handler(message.params || {});
          if (message.method === "starbridge.ping") {
            this.lastPingAt = nowIso();
          }
          this.send({ ...replyBase, result });
        } catch (error) {
          this.send({ ...replyBase, error: { code: -32000, message: String(error?.message || error) } });
        }
      });
      socket.addEventListener("error", () => handleDisconnect("error"));
    } catch (_error) {
      this.connected = false;
      this.socket = null;
      if (this.useFallbackProxyUrl()) {
        this.onStatus("connecting");
        this.connect();
        return;
      }
      this.resetDefaultProxyUrl();
      this.onStatus("error");
      this.scheduleReconnect();
    }
  }

  reconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket) {
      this.socket.close();
      return;
    }
    this.connect();
  }

  hostInfo() {
    try {
      const photoshop = require("photoshop");
      return {
        app: "Photoshop",
        version: String(photoshop?.app?.version || "unknown"),
      };
    } catch (_error) {
      return { app: "Photoshop", version: "unknown" };
    }
  }

  useFallbackProxyUrl() {
    if (this.proxyUrlIndex + 1 >= this.proxyUrls.length) {
      return false;
    }
    this.proxyUrlIndex += 1;
    this.proxyUrl = this.proxyUrls[this.proxyUrlIndex];
    return true;
  }

  resetDefaultProxyUrl() {
    this.proxyUrlIndex = 0;
    this.proxyUrl = this.proxyUrls[this.proxyUrlIndex];
  }

  scheduleReconnect() {
    if (this.reconnectTimer) {
      return;
    }
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 2000);
  }

  send(payload) {
    if (this.socket && this.connected) {
      this.socket.send(JSON.stringify(payload));
    }
  }
}

module.exports = {
  BridgeClient,
  DEFAULT_PROXY_URLS,
};
