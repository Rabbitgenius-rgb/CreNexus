import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { PageId } from "../app/routes";
import type {
  ConnectionOverview,
  CreativeApplicationConnection,
  LicenseStatus,
  RuntimeStatus,
} from "../types/api";
import { HomePage } from "./HomePage";

const CONNECTED_STATUS: RuntimeStatus = {
  state: "connected",
  message: "本地服务已就绪。",
  recoveryAttempts: 0,
};

const COMMUNITY_LICENSE: LicenseStatus = {
  state: "community",
  edition: "community",
  message: "Community 免费版正在本机运行。",
  deviceLimit: 0,
  features: [],
  commercialVerifierConfigured: false,
};

function application(
  id: string,
  name: string,
  mark: string,
): CreativeApplicationConnection {
  return {
    id,
    name,
    mark,
    state: "bridge_ready",
    installed: true,
    running: true,
    bridge_available: true,
    managed: true,
    message: `${name} 已就绪。`,
    pairing_state: "paired",
    paired: true,
    adapter_kind: "http",
    control_level: "verified_bridge",
    capabilities: ["probe", "plan", "write"],
    next_steps: [],
    version: "1.0.0",
  };
}

function connections(applications: CreativeApplicationConnection[] = []): ConnectionOverview {
  return {
    schema_version: "starbridge.desktop-connections.v2",
    checked_at: "2026-07-25T08:00:00Z",
    drawing_enabled: true,
    codex: {
      state: "paired",
      app_available: true,
      connector_configured: true,
      session_paired: true,
      pairing_code: "ABCD2345",
      message: "Codex 已关联。",
      next_steps: [],
    },
    applications,
    safety: {
      loopback_only: true,
      credentials_read: false,
      external_apps_force_restarted: false,
    },
  };
}

function renderHome(options?: {
  status?: RuntimeStatus;
  connections?: ConnectionOverview | null;
  onNavigate?: (page: PageId) => void;
}) {
  const onNavigate = options?.onNavigate ?? vi.fn();
  render(
    <HomePage
      status={options?.status ?? CONNECTED_STATUS}
      connections={options?.connections === undefined ? connections() : options.connections}
      recentTasks={[]}
      license={COMMUNITY_LICENSE}
      version={{ desktop: "0.1.0" }}
      onNavigate={onNavigate}
    />,
  );
  return { onNavigate };
}

describe("HomePage", () => {
  it("counts every ready application while limiting the visual directory to three cards", () => {
    renderHome({
      connections: connections([
        application("photoshop", "Photoshop", "PS"),
        application("illustrator", "Illustrator", "AI"),
        application("comfyui", "ComfyUI", "CU"),
        application("blender", "Blender", "BL"),
      ]),
    });

    expect(screen.getByText("4 READY")).toBeInTheDocument();
    expect(screen.getByText("Photoshop 桥")).toBeInTheDocument();
    expect(screen.getByText("Illustrator 桥")).toBeInTheDocument();
    expect(screen.getByText("ComfyUI 桥")).toBeInTheDocument();
    expect(screen.queryByText("Blender 桥")).not.toBeInTheDocument();
  });

  it("distinguishes an empty bridge result from a bridge scan that is still loading", () => {
    const { unmount } = render(
      <HomePage
        status={CONNECTED_STATUS}
        connections={connections([])}
        recentTasks={[]}
        license={COMMUNITY_LICENSE}
        version={{ desktop: "0.1.0" }}
        onNavigate={vi.fn()}
      />,
    );

    expect(screen.getByText("尚未检测到可用软件桥")).toBeInTheDocument();
    expect(screen.queryByText("正在检测本机软件桥")).not.toBeInTheDocument();

    unmount();
    renderHome({ connections: null });
    expect(screen.getByText("正在检测本机软件桥")).toBeInTheDocument();
    expect(screen.queryByText("尚未检测到可用软件桥")).not.toBeInTheDocument();
  });

  it("uses truthful privacy wording for explicitly enabled anonymous metrics", () => {
    renderHome();

    expect(screen.getAllByText(/可选匿名指标/)).toHaveLength(2);
    expect(screen.queryByText(/不会上传任何内容/)).not.toBeInTheDocument();
    expect(screen.getByText("KORYAO-LOCAL")).toBeInTheDocument();
  });

  it("shows the actual next step and opens connection management before drawing is enabled", () => {
    const unpaired = connections([]);
    unpaired.drawing_enabled = false;
    unpaired.codex.session_paired = false;
    unpaired.codex.state = "awaiting_pairing";
    const onNavigate = vi.fn();

    renderHome({ connections: unpaired, onNavigate });

    const start = screen.getByRole("button", { name: "连接 Codex 后开始制图" });
    expect(start).toHaveTextContent("前往连接中心完成关联");
    fireEvent.click(start);
    expect(onNavigate).toHaveBeenCalledWith("integrations");
  });
});
