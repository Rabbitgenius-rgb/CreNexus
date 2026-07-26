# Generated sidecar staging directory

Windows 使用 `Build-Sidecar.ps1`；Darwin 使用 `Build-Sidecar.sh`。两者都会在这里
staging target-triple 可执行文件。Windows 通过 Tauri `externalBin` 打包；Stage 5
的 macOS arm64 `.app` 则把 executable 与 support sibling 一起作为 Resources
打包。生成物均被 Git 忽略。

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
wrapper 不接受 symlink 调用，sibling launcher 也必须是非 symlink 普通文件。

脚本会动态解析当前 host triple。`x86_64-apple-darwin` 有参数与路径测试，
但没有在 arm64 构建中冒充 universal binary。Darwin one-folder 产物包含固定版本的
Vector60 Python runtime；当前没有打包 Node/SVGO，因此 SVGO 路径明确保持未包含。
每次构建都在对应 triple 的唯一 generation 中运行，并把 PyInstaller cache 固定到该
triple 的 build root 后执行 `--clean`。同一 triple 的构建/staging 使用 POSIX 锁串行；
可执行文件与 support 目录先作为完整候选验证，再成对替换，最终验证失败会恢复旧 pair。
binaries、build 或 build venv 旁出现同步/冲突编号副本时 fail closed，不复用陈旧
Analysis 或混入另一平台产物。

Stage 5 的 macOS overlay 仅面向当前实机 `aarch64-apple-darwin`：启用本地 `.app`
bundle，不使用会把 executable 移到 `Contents/MacOS` 的 `externalBin`。固定的真实
`starbridge-sidecar-aarch64-apple-darwin` 与对应 `_internal-aarch64-apple-darwin`
support 树都作为 Resources 打包并保持相邻；Rust runtime 只从编译期固定的
`resource_dir` 路径启动它。验收时必须核对 `.app` 内 executable 与 target-specific
support 目录实际同级，并真实验证应用内启动、认证 bootstrap、一次有限恢复、
手动 restart、正常 Quit、端口释放与零孤儿进程；仅通过配置 schema 或直接
sidecar 测试不能代替这项 runtime smoke。

当前没有 `x86_64` 实机 `.app` 验证，也不提供或声称 universal binary。代码签名、
notarization、公开 release、安装包和 CI 均不属于本阶段，尚未支持或验证；当前
macOS `.app` 只作为 Apple Silicon 本地 runtime smoke 候选。

不要提交生成的可执行文件、`_internal*`、DLL/dylib、Python bytecode 或本机路径。
