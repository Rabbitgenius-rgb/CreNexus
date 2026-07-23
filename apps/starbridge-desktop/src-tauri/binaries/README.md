# Generated sidecar staging directory

Windows 使用 `Build-Sidecar.ps1`；Darwin 使用 `Build-Sidecar.sh`。两者都会在这里
staging Tauri `externalBin` 所需的 target-triple 可执行文件。生成物均被 Git 忽略。

Expected Windows development layout:

```text
binaries/
├─ starbridge-sidecar-x86_64-pc-windows-msvc.exe
└─ _internal/
```

Darwin 产物按 triple 隔离，避免覆盖另一架构：

```text
binaries/
├─ starbridge-sidecar-aarch64-apple-darwin
└─ _internal-aarch64-apple-darwin/
```

在 macOS 上运行：

```bash
./scripts/Build-Sidecar.sh
./scripts/Test-Sidecar.sh --skip-build
```

必须像上面这样直接执行 wrapper，让受保护的 shebang 在 shell 读取继承环境前生效；
不要改成 `sh ./scripts/Build-Sidecar.sh` 或 `bash ./scripts/Test-Sidecar.sh`。
wrapper 随后只进入固定的系统 Python launcher，由 launcher 构造明确 allowlist，
再启动仓库 `.venv` 中的实际 builder/tester。仓库 `.venv` 不存在时会 fail closed，
不会回退到受 Xcode toolchain 环境影响的系统 Python shim。

脚本会动态解析当前 host triple。`x86_64-apple-darwin` 有参数与路径测试，
但没有在 arm64 构建中冒充 universal binary。Darwin one-folder 产物包含固定版本的
Vector60 Python runtime；当前没有打包 Node/SVGO，因此 SVGO 路径明确保持未包含。
每次构建都在对应 triple 的唯一 generation 中运行，并把 PyInstaller cache 固定到该
triple 的 build root 后执行 `--clean`。同一 triple 的构建/staging 使用 POSIX 锁串行；
可执行文件与 support 目录先作为完整候选验证，再成对替换，最终验证失败会恢复旧 pair。
binaries、build 或 build venv 旁出现同步/冲突编号副本时 fail closed，不复用陈旧
Analysis 或混入另一平台产物。

Tauri v2 的 `externalBin` 会把可执行文件复制到目标目录根，而 list resource glob
会保留 `binaries/` 前缀，无法把动态 triple 的完整 PyInstaller support 树映射到
可执行文件同级。为避免产生“配置可解析但运行必失败”的假阳性，macOS overlay 在
Stage 4 明确保持 `bundle.active=false`，不声明 `externalBin` 或 `resources`。
本阶段只验证 builder/staging 与直接 sidecar 协议；真实 Tauri dev/no-bundle、
`.app` 布局及应用内启动属于 Stage 5，尚未验证。

不要提交生成的可执行文件、`_internal*`、DLL/dylib、Python bytecode 或本机路径。
