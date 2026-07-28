# CreNexus Photoshop Node Proxy

Starts a local HTTP JSON-RPC bridge between the CreNexus MCP server and the Photoshop UXP plugin.

## Start

```powershell
cd node_proxy\photoshop-bridge
npm install
npm start
```

## Endpoints

- `GET /health`
- `GET /bridge/status`
- `POST /rpc`

详细方法白名单、256 KiB 请求上限、输出路径和 BatchPlay 副本规则见 `docs/photoshop-node-proxy-security.md`。

## Notes

- UXP 插件先连接 `ws://localhost:8971/uxp`；若连接建立失败，再回退 `ws://127.0.0.1:8971/uxp`。manifest 同时白名单这两个 host 的 `ws` / `http` origin。
- Node Proxy 仍只监听 IPv4 回环地址 `127.0.0.1`（默认端口 `8971`），不会监听局域网或公网。该顺序兼容 macOS / Windows 的 `localhost` 解析差异，但 Windows Photoshop 实机尚未在本仓库自动化测试中验证。
- If no UXP client is connected, `/rpc` returns `uxp_client_not_connected`.
- 每个通过校验的 HTTP RPC 只向当前 UXP 客户端分发一次；`running` 在分发前发布，随后只发布一次 `completed` 或 `failed`。
- 写请求必须显式确认；传统预览只允许仓库 `sandbox/`、`output/` 和 `examples/output/photoshop/`，`ps.production.execute_confirmed` 只允许 StarBridge 应用数据目录中的 hash 绑定项目源和任务产物。
- 生产协议使用固定输出文件名和代理推导的 `.part` 临时文件；成功后才原子提升，失败只清理本任务应用拥有的临时文件。
- Typed BatchPlay 只在自动复制的临时文档上执行，不覆盖原活动文档。
