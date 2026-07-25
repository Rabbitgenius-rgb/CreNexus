export const navigation = [
  ["/", "首页"], ["/features", "功能"], ["/editions", "版本"],
  ["/workflows", "工作流"], ["/privacy", "隐私"], ["/docs", "文档"],
  ["/roadmap", "路线图"], ["/support", "支持"]
];

export const pages = {
  "/": {
    eyebrow: "本地 AI 创意工作台",
    title: "把创意任务做成一条可验证的本地工作流。",
    intro: "KORYAO Basic（构曜纪基础版）把 Codex 调度、图片矢量化、质量核对、任务记录和创意软件交付集中到一个 Windows 优先的桌面应用中。素材默认留在本机，真实写入必须确认。",
    actions: [["/features", "查看当前能力"], ["/editions", "了解版本边界"]],
    sections: [
      ["从目标到交付", "选择项目和素材后，可以通过 Codex 对话或页面操作完成执行、核对、预览、导出和证据记录。"],
      ["本机处理是默认边界", "KORYAO 不要求把客户图片、设计文件或授权信息上传到 KORYAO 服务器；可选匿名指标必须由用户明确启用。"],
      ["只承诺有证据的能力", "稳定、实验、规划和未支持状态分别标注，不把按钮、协议或测试样例包装成完整商业交付。"]
    ]
  },
  "/features": {
    eyebrow: "功能与证据",
    title: "围绕真实创作流程组织能力。",
    intro: "当前重点是图片矢量化、本地安全调度和可追溯交付；第三方软件桥接按实际验收状态开放。",
    sections: [
      ["五种矢量化模式", "像素重建、Editable-99、匠心矢量、智能矢量和轻量矢量分别面向忠实复刻、高保真编辑、插画、通用素材与轻量图形。"],
      ["Codex + MCP 本地调度", "Codex 负责理解目标和选择工具，本地运行时负责路径限制、确认、执行、验证和脱敏记录。"],
      ["创意软件交付", "Illustrator、Photoshop、ComfyUI、Blender 与 CAD 已有不同程度的实现、探针或协议；具体状态以产品事实和 CI 证据为准。"],
      ["KORYAO-C1 本地模型运行端", "通过 loopback 接收结构化任务元数据，用于计划、评估和修复建议，不直接读取磁盘或绕过写入确认。"]
    ]
  },
  "/editions": {
    eyebrow: "版本对比",
    title: "基础能力直接使用，专业能力按验收开放。",
    intro: "Community 无需激活。Pro 早鸟永久版建议价为 ¥399，但当前尚未正式开售；Enterprise 需要按项目确认范围和报价。",
    sections: [
      ["Community · ¥0", "本地核心运行、五种公开矢量模式、基础任务记录与安全能力；无需登录或连接授权服务器。"],
      ["Pro · 建议 ¥399", "批量队列、项目历史、任务恢复、私有增强与专业支持仍在规划和验收中，具体价格与条款尚未生效。"],
      ["Enterprise · 按项目报价", "企业部署、定制 Adapter、交付支持和私有代码必须通过单独合同明确范围、安全边界与验收证据。"]
    ]
  },
  "/workflows": {
    eyebrow: "工作流案例",
    title: "从一张图片开始，在本机完成可交付结果。",
    intro: "主流程：选择项目与图片 → 选择模式 → 确认参数 → 本地执行 → 查看质量指标 → 预览结果 → 打开输出目录或导出 AI / PSD。",
    sections: [
      ["像素级忠实复刻", "像素重建把工作分辨率中的 RGBA 像素转为真实 SVG 几何，并回渲染逐像素核对。"],
      ["高保真继续编辑", "Editable-99 只有在五项质量门槛全部通过后，才会从合格候选中选择复杂度更低的结果。"],
      ["插画、Logo 与纹样", "匠心、智能和轻量模式在编辑性、相似度、节点数量和文件体积之间提供不同取向。"],
      ["Adobe 文件交付", "Windows 上可在明确确认后调用 Illustrator 或 Photoshop，验证结果后写入用户选择的新路径，已有文件不会被覆盖。"]
    ]
  },
  "/privacy": {
    eyebrow: "本地处理与安全",
    title: "素材留在电脑里，写入保持可控。",
    intro: "核心服务默认只绑定 loopback，不递归扫描未授权目录，也不会默认发送遥测。只有用户明确启用匿名指标并确认 consent 后，才会发送不含素材、文件名、路径或客户文本的受限统计。",
    sections: [
      ["素材", "图片、设计文件和客户项目不需要上传到 KORYAO 服务器。"],
      ["日志", "证据记录使用哈希、相对引用和状态，不保存 Token、Cookie、OAuth、完整指令或真实绝对路径。"],
      ["匿名指标", "仅在用户明确启用 feedback.github_metrics_upload 并确认 starbridge.github_metrics.v1 consent 后发送；关闭开关不会影响本地交付。"],
      ["本地模型", "KORYAO-C1 只接收经过 schema 校验的任务元数据、素材 ID 和 Adapter 白名单，不直接访问磁盘。"],
      ["软件更新", "正式构建只应请求版本信息；下载、安装和真实写入始终需要用户确认。"]
    ]
  },
  "/docs": {
    eyebrow: "文档与事实源",
    title: "从产品承诺到技术边界都有记录。",
    intro: "仓库文档覆盖产品事实、架构、矢量化质量门槛、Adobe 交付、安全模型、本地模型协议和 Windows 发布门槛。",
    actions: [["https://github.com/jianbaorui07-dot/KORYAO-basic/tree/main/docs", "打开 GitHub 文档"]],
    sections: [
      ["产品事实", "机器可读 Manifest 区分可用、实验、规划和未支持状态。"],
      ["质量与验证", "每种矢量模式拥有明确边界，像素重建和 Editable-99 不会静默降低门槛。"],
      ["发布准备", "代码签名、干净机器、网络请求、升级回滚、Defender 与 SmartScreen 都属于正式发布前置条件。"]
    ]
  },
  "/download": {
    eyebrow: "内部预览",
    title: "Windows 未签名预览版仅用于测试。",
    intro: "当前 GitHub prerelease 提供的是更名前兼容的内部预览构建。它不是正式稳定版，也未完成 Authenticode 代码签名，因此 Windows 可能显示“未知发布者”。",
    actions: [["https://github.com/jianbaorui07-dot/KORYAO-basic/releases/download/starbridge-preview-v0.1.0-unsigned.1/StarBridge-Desktop_0.1.0_x64-setup-UNSIGNED-PREVIEW.exe", "下载兼容预览版"], ["https://github.com/jianbaorui07-dot/KORYAO-basic/releases/tag/starbridge-preview-v0.1.0-unsigned.1", "查看发布记录"]],
    sections: [
      ["使用范围", "仅用于团队测试和兼容性验证，不作为正式商业交付。"],
      ["文件校验", "SHA-256：CB163DCB77CE79CDA2E815B8B8FF16760FAFCC81F0465D342B37AE1E71BB146D。"],
      ["已知边界", "仍需完成受信任代码签名、干净 Windows、SmartScreen、升级回滚和正式条款验收。"]
    ]
  },
  "/roadmap": {
    eyebrow: "路线图",
    title: "先完成验收，再扩大承诺。",
    intro: "路线图以可复现证据为准，优先解决安装、兼容、恢复和交付问题。",
    sections: [
      ["Windows 正式发布", "完成签名安装包、SmartScreen、干净机器、升级回滚与更新签名。"],
      ["Adobe 兼容矩阵", "扩大 Photoshop / Illustrator 多版本、多语言、异常恢复和客户机器验收。"],
      ["创意软件闭环", "将 ComfyUI、Blender、AutoCAD 和剪映从探针、dry-run 或实验实现推进到可复现流程。"],
      ["本地模型运行端", "完善协议版本、失败降级、可观测性与桌面端状态说明。"]
    ]
  },
  "/support": {
    eyebrow: "支持与合作",
    title: "公开问题可复现，商业范围单独确认。",
    intro: "当前购买入口尚未开放。Community 缺陷和文档问题可通过 GitHub Issue 反馈，联合开发与商业合作可通过邮件联系。",
    actions: [["https://github.com/jianbaorui07-dot/KORYAO-basic/issues", "提交或查看问题"]],
    sections: [
      ["Community", "请提供可复现步骤、系统版本和脱敏日志，不要上传客户素材、Token、授权文件或真实路径。"],
      ["联合开发", "欢迎开发者、设计师、测试人员和创意软件专家参与功能、验收和文档建设。"],
      ["商业合作", "企业部署、定制 Adapter、交付支持与授权条款需要单独确认；联系 jianbaorui07@gmail.com。"]
    ]
  }
};
