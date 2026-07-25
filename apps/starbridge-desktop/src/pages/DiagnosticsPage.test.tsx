import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DiagnosticsPage } from "./DiagnosticsPage";

describe("DiagnosticsPage", () => {
  it("describes anonymous metrics as explicit opt-in instead of claiming no telemetry exists", () => {
    render(
      <DiagnosticsPage
        status={{
          state: "connected",
          message: "本地服务已就绪。",
          recoveryAttempts: 0,
          port: 49152,
          backendPid: 1234,
        }}
        version={{ desktop: "0.1.0-alpha.2", backend: "0.1.0-alpha.2" }}
        onRestart={vi.fn().mockResolvedValue(undefined)}
        onOpenLogs={vi.fn().mockResolvedValue("<LOCAL_APP_DATA>/KORYAO/logs")}
        updateStatus={{
          configured: false,
          source: "GitHub Releases",
          currentVersion: "0.1.0-alpha.2",
          available: false,
          signatureRequired: true,
          automaticChecksSupported: false,
        }}
        automaticUpdateChecks={false}
        checkingForUpdate={false}
        installingUpdate={false}
        updateProgress={null}
        updateMessage=""
        updateError=""
        onAutomaticUpdateChecksChange={vi.fn()}
        onCheckForUpdate={vi.fn().mockResolvedValue(undefined)}
        onInstallUpdate={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(screen.getByText(/默认不发送遥测/)).toBeInTheDocument();
    expect(screen.getByText(/明确启用并确认 consent/)).toBeInTheDocument();
    expect(screen.getByText(/不包含素材、文件名、本机路径、客户文本或账号信息/)).toBeInTheDocument();
    expect(screen.queryByText("不收集遥测，不运行后台公网服务")).not.toBeInTheDocument();
  });
});
