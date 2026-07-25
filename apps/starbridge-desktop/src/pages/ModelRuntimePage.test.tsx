import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { KORYAOClient } from "../services/client";
import { ModelRuntimePage } from "./ModelRuntimePage";

function healthyRuntime(overrides: Record<string, unknown> = {}) {
  return {
    schema: "koryao-model-contract/v1",
    serviceId: "koryao-model-runtime",
    serviceVersion: "0.1.0",
    status: "healthy",
    runtimeMode: "local",
    supportedContracts: ["koryao-model-contract/v1"],
    network: { bindAddress: "127.0.0.1", externalNetworkAccess: false },
    privacy: {
      acceptsRawAssets: false,
      logsAbsolutePaths: false,
      logsFullInstructions: false,
    },
    models: [
      {
        modelId: "koryao-c1-planner",
        version: "0.1.0",
        providerId: "rule-based",
        status: "experimental",
        capabilities: ["plan", "evaluate", "repair"],
      },
      {
        modelId: "koryao-c1-evaluator",
        version: "0.1.0",
        providerId: "rule-based",
        status: "ready",
        capabilities: ["evaluate"],
      },
    ],
    ...overrides,
  };
}

describe("ModelRuntimePage", () => {
  it("shows model, provider, capability, and privacy details", async () => {
    const client = {
      getModelRuntimeStatus: vi.fn().mockResolvedValue(healthyRuntime()),
    } as unknown as KORYAOClient;

    render(<ModelRuntimePage client={client} runtimeReady />);

    expect(await screen.findByText("koryao-c1-planner")).toBeInTheDocument();
    expect(screen.getByText("2 个模型 · 1 个 Provider")).toBeInTheDocument();
    expect(screen.getByText("127.0.0.1 / LOOPBACK")).toBeInTheDocument();
    expect(screen.getByText("任务规划")).toBeInTheDocument();
    expect(screen.getByText("安全边界已启用")).toBeInTheDocument();
    expect(screen.getByText("外网访问：关闭")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重新检测" }));
    await waitFor(() => expect(client.getModelRuntimeStatus).toHaveBeenCalledTimes(2));
  });

  it("does not probe while the KORYAO backend is offline", async () => {
    const client = {
      getModelRuntimeStatus: vi.fn(),
    } as unknown as KORYAOClient;

    render(<ModelRuntimePage client={client} runtimeReady={false} />);

    expect(await screen.findByText("本地模型运行端未连接")).toBeInTheDocument();
    expect(screen.getByText("请先启动 KORYAO 本地服务。")).toBeInTheDocument();
    expect(client.getModelRuntimeStatus).not.toHaveBeenCalled();
  });

  it("does not claim the safety boundary is enabled when the runtime reports unsafe settings", async () => {
    const client = {
      getModelRuntimeStatus: vi.fn().mockResolvedValue(healthyRuntime({
        network: { bindAddress: "127.0.0.1", externalNetworkAccess: true },
        privacy: {
          acceptsRawAssets: true,
          logsAbsolutePaths: true,
          logsFullInstructions: true,
        },
      })),
    } as unknown as KORYAOClient;

    render(<ModelRuntimePage client={client} runtimeReady />);

    expect(await screen.findByText("检测到需要处理的安全配置")).toBeInTheDocument();
    expect(screen.getByText("外网访问：开启")).toBeInTheDocument();
    expect(screen.getByText("原始素材接收：开启")).toBeInTheDocument();
    expect(screen.queryByText("安全边界已启用")).not.toBeInTheDocument();
  });
});
