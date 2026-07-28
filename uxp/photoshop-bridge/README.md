# CreNexus Photoshop UXP Bridge

This is the local UXP plugin side of the CreNexus Photoshop bridge.

## Current State

- 根目录 `index.js` 提供 `starbridge.ping`、`ps.document.info`、`ps.layers.list`、`ps.preview.export`、`ps.camera_raw.tune`、`ps.batchplay.validate.local` 和 `ps.batchplay.execute_confirmed` JSON-RPC handler。
- 插件入口和本地模块使用 UXP 可直接加载的 CommonJS（经典 `<script>`、`require()`、`module.exports`），不依赖原生 ESM。
- 入口放在插件根目录，使 `require("./src/...")` 同时符合“相对当前文件”规则和本机 UXP 的根目录解析行为。
- `src/bridge-client.js` 默认连接 `ws://localhost:8971/uxp`；连接建立失败时只回退到 `ws://127.0.0.1:8971/uxp`。
- 构造器仍可显式传入 `proxyUrl`；自定义地址必须同时进入 manifest 网络白名单，默认不会放宽到局域网或公网。
- `src/batchplay-schema.js` and `src/batchplay-runner.js` enforce a typed allowlist and wrap write-like execution in `executeAsModal`.
- `runModalJob` uses a bounded modal queue timeout, cancellation checkpoints, and explicit history commit/rollback metadata defined by `starbridge.photoshop-modal.v1`.
- Confirmed BatchPlay duplicates the active document first and registers the copy for automatic close on cancellation or failure.
- Preview export requires a Node Proxy verified repository sandbox path; UXP repeats the extension and scope check before writing.
- Real writes still require explicit confirmation and must stay on sandbox copies.
- Camera Raw tuning is experimental. V1 supports parameter planning and safe validation. Real Photoshop apply requires a verified local BatchPlay descriptor and explicit confirmation.

Protocol and safety details: [`docs/photoshop-uxp-modal-envelope.md`](../../docs/photoshop-uxp-modal-envelope.md).

## Intended Chain

`Codex -> MCP Server -> Node Proxy -> UXP Plugin -> Photoshop DOM / batchPlay / executeAsModal`

上述双回环顺序用于兼容 macOS 和 Windows 对 `localhost` 的 IPv4/IPv6 解析差异。当前回归测试覆盖模块加载契约、地址顺序和安全回退；Windows Photoshop 实机仍需单独验证。

## What To Add Later

- Host-specific preview bitmap encoding if the local Photoshop build exposes a reliable export path from UXP
- Additional typed descriptors beyond the current allowlist
