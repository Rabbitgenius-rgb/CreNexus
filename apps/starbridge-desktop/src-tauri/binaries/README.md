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

脚本会动态解析当前 host triple。`x86_64-apple-darwin` 有参数与路径测试，
但没有在 arm64 构建中冒充 universal binary。Darwin one-folder 产物包含固定版本的
Vector60 Python runtime；当前没有打包 Node/SVGO，因此 SVGO 路径明确保持未包含。
每次构建都在对应 triple 的唯一 generation 中运行，并把 PyInstaller cache 固定到该
triple 的 build root 后执行 `--clean`。同一 triple 的构建/staging 使用 POSIX 锁串行；
可执行文件与 support 目录先作为完整候选验证，再成对替换，最终验证失败会恢复旧 pair。
binaries、build 或 build venv 旁出现同步/冲突编号副本时 fail closed，不复用陈旧
Analysis 或混入另一平台产物。

本阶段已用 Tauri v2 schema 与已安装的 Tauri CLI 解析 macOS overlay/resource glob；
这只证明配置入口有效，不代表真实 `.app` 内 `Contents/MacOS`、`Contents/Resources`
布局或应用内 sidecar 启动已经验证。后两项属于 Stage 5，不在本阶段冒充完成。

不要提交生成的可执行文件、`_internal*`、DLL/dylib、Python bytecode 或本机路径。
