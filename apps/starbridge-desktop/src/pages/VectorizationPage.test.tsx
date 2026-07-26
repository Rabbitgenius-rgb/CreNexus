import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { KORYAOClient } from "../services/client";
import type { VectorJob, VectorSelection } from "../types/api";
import { VectorizationPage } from "./VectorizationPage";

const PREVIEW = "data:image/png;base64,iVBORw0KGgo=";

const SELECTION: VectorSelection = {
  selectionId: "selection-test",
  fileName: "pixel.png",
  width: 1,
  height: 1,
  sourceHash: "abc123def456",
  previewDataUrl: PREVIEW,
};

const COMPLETED_JOB: VectorJob = {
  jobId: "vector-test",
  status: "completed",
  progress: 100,
  stage: "处理完成",
  mode: "exact",
  createdAt: "2026-07-25T00:00:00Z",
  completedAt: "2026-07-25T00:00:01Z",
  result: {
    modeLabel: "像素重建",
    status: "completed",
    sourceHash: "abc123def456",
    sourcePreviewDataUrl: PREVIEW,
    resultPreviewDataUrl: PREVIEW,
    metrics: {
      colors: 1,
      subpaths: 1,
      points: 4,
      svgBytes: 256,
      elapsedSeconds: 0.1,
      pixelMatch: true,
    },
    illustratorSafety: {
      riskLevel: "safe",
      action: "review",
      autoOpenAllowed: false,
      message: "SVG 可人工核对；未调用 Illustrator。",
      thresholdSource: "exact-pixel",
    },
    warnings: [],
    outputAvailable: true,
  },
};

describe("Exact Pixel Reconstruction runtime flow", () => {
  it("selects, runs exact mode, renders both previews, and refreshes persisted history", async () => {
    const getVectorizationHistory = vi
      .fn()
      .mockResolvedValueOnce({ eventCount: 0, events: [] })
      .mockResolvedValue({
        eventCount: 1,
        events: [
          {
            eventId: "event-test",
            createdAt: "2026-07-25T00:00:01Z",
            mode: "exact",
            summary: "像素重建完成且逐像素一致",
            sourceHash: "abc123def456",
            metrics: COMPLETED_JOB.result?.metrics,
            outputAvailable: true,
          },
        ],
      });
    const client = {
      chooseVectorInput: vi.fn().mockResolvedValue(SELECTION),
      startVectorization: vi.fn().mockResolvedValue({
        ...COMPLETED_JOB,
        status: "queued",
        progress: 6,
        stage: "已确认，正在准备",
        result: undefined,
      }),
      getVectorizationJob: vi.fn().mockResolvedValue(COMPLETED_JOB),
      getVectorizationHistory,
      openVectorOutput: vi.fn(),
    } as unknown as KORYAOClient;

    render(
      <VectorizationPage
        client={client}
        runtimeReady
        codexConnected
        onOpenConnections={vi.fn()}
        onTaskSaved={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "选择图片" }));
    expect(await screen.findByText("pixel.png")).toBeInTheDocument();
    expect(
      screen.getByRole("radio", { name: /像素矢量/ }),
    ).toHaveAttribute("aria-checked", "true");

    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "开始本机矢量化" }));

    await waitFor(
      () =>
        expect(client.startVectorization).toHaveBeenCalledWith(
          {
            selectionId: "selection-test",
            mode: "exact",
            parameters: {},
            confirmRun: true,
            confirmWrite: true,
            confirmExport: true,
          },
        ),
      { timeout: 2_000 },
    );
    expect(screen.getByText(/原始尺寸处理；超过安全像素或复杂度上限时会直接停止/)).toBeInTheDocument();
    expect(await screen.findByRole("img", { name: "原图预览" })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "矢量化结果预览" })).toBeInTheDocument();
    expect(screen.getByText("一致")).toBeInTheDocument();
    expect(
      await screen.findByText("像素重建完成且逐像素一致"),
    ).toBeInTheDocument();
    expect(getVectorizationHistory).toHaveBeenCalledTimes(2);
  });
});
