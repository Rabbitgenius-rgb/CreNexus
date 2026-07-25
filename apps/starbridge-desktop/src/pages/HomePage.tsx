import {
  IconArrowRight,
  IconFileCheck,
  IconFolderPlus,
  IconListDetails,
  IconPlayerPlay,
  IconPlugConnected,
  IconShieldCheck,
} from "@tabler/icons-react";

import type { PageId } from "../app/routes";
import { EmptyState } from "../components/EmptyState/EmptyState";
import type {
  ConnectionOverview,
  CreativeApplicationState,
  CreativeJob,
  LicenseStatus,
  RuntimeStatus,
  VersionInfo,
} from "../types/api";

interface HomePageProps {
  status: RuntimeStatus;
  connections: ConnectionOverview | null;
  connectionsLoading: boolean;
  connectionsError: string;
  recentTasks: CreativeJob[];
  license: LicenseStatus;
  version: VersionInfo | null;
  onNavigate: (page: PageId) => void;
}

const STATUS_LABELS: Record<CreativeJob["status"], string> = {
  queued: "等待开始",
  running: "运行中",
  needs_user: "等待确认",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const APP_STATE: Record<CreativeApplicationState, string> = {
  not_installed: "未找到",
  installed: "已安装",
  running: "运行中",
  bridge_ready: "就绪",
  unavailable: "待重试",
};

function formatUpdatedAt(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function HomePage({
  status,
  connections,
  connectionsLoading,
  connectionsError,
  recentTasks,
  license,
  version,
  onNavigate,
}: HomePageProps) {
  const runtimeReady = status.state === "connected";
  const drawingReady = runtimeReady && connections?.drawing_enabled === true;
  const allApplications = connections?.applications ?? [];
  const applications = allApplications.slice(0, 3);
  const tasks = recentTasks.slice(0, 5);
  const pairedApplications = allApplications.filter(
    (application) => application.paired || application.bridge_available,
  ).length;
  const startLabel = drawingReady ? "新建或打开项目" : "连接 Codex 后开始制图";
  const startHint = drawingReady
    ? "可审计 · 可追溯"
    : runtimeReady
      ? "前往连接中心完成关联"
      : "等待本地服务就绪";
  const bridgeErrorVisible = applications.length === 0 && Boolean(connectionsError);
  const bridgeLoadingVisible = applications.length === 0
    && !bridgeErrorVisible
    && (connectionsLoading || connections === null);
  const bridgeEmptyVisible = applications.length === 0
    && !bridgeErrorVisible
    && !bridgeLoadingVisible
    && connections !== null;

  return (
    <div className="home-page editorial-home">
      <section className="home-command-grid" aria-labelledby="home-mission-title">
        <div className="mission-panel">
          <span className="editorial-kicker">MISSION / LOCAL CREATIVE CONTROL</span>
          <h2 id="home-mission-title">从项目开始<br />一次可审计的创作</h2>
          <p>本地优先的 AI 创意软件协作工作台</p>
          <button
            type="button"
            className="home-primary-cta"
            aria-label={startLabel}
            disabled={!runtimeReady}
            onClick={() => onNavigate(drawingReady ? "projects" : "integrations")}
          >
            <IconPlayerPlay aria-hidden="true" />
            <strong>开始项目</strong>
            <span>{startLabel}<small>{startHint}</small></span>
            <IconArrowRight aria-hidden="true" />
          </button>
          {!runtimeReady ? <p className="inline-guidance">本地服务就绪后即可开始；可前往“设置与诊断”重新启动。</p> : !drawingReady ? <p className="inline-guidance">本地服务已就绪；请在连接中心关联当前 Codex 会话。</p> : null}
        </div>

        <aside className="system-status-panel" aria-label="系统运行状态">
          <header><strong>运行状态</strong><span>/ SYSTEM STATUS</span></header>
          <div className="system-status-body">
            <div className="codex-runtime-block">
              <div className="runtime-heading"><strong>CODEX</strong><span>连接状态</span><em className={connections?.codex.session_paired ? "is-ready" : "is-waiting"}>{connections?.codex.session_paired ? "● 已连接" : "● 待关联"}</em></div>
              <dl>
                <div><dt>连接器</dt><dd>{connections?.codex.connector_configured ? "已配置" : "待配置"}</dd></div>
                <div><dt>会话</dt><dd>{connections?.codex.session_paired ? "ACTIVE" : "STANDBY"}</dd></div>
                <div><dt>软件桥</dt><dd>{pairedApplications} READY</dd></div>
              </dl>
            </div>
            <div className="runtime-mode-block">
              <span>运行模式</span>
              <strong>LOCAL-ONLY</strong>
              <em className={runtimeReady ? "is-ready" : "is-waiting"}>{runtimeReady ? "ONLINE · LOCAL-ONLY" : "本地服务需要处理"}</em>
            </div>
            <div className="data-boundary-block">
              <IconShieldCheck aria-hidden="true" />
              <div>
                <strong>数据边界</strong>
                <span>素材与设计文件仅在本机处理；可选匿名指标必须由用户明确启用，且不包含素材、文件名或本机路径。</span>
              </div>
            </div>
          </div>
          <footer>
            <div><span>COMMUNITY 版本</span><strong>{license.edition === "community" ? "Community" : license.edition.toUpperCase()}</strong></div>
            <div><span>版本号</span><strong>v{version?.desktop ?? "—"}</strong></div>
            <div><span>环境标识</span><strong>KORYAO-LOCAL</strong></div>
          </footer>
        </aside>
      </section>

      <section className="privacy-strip">
        <IconShieldCheck aria-hidden="true" />
        <div>
          <strong>素材和设计文件不上传</strong>
          <span>核心处理仅在本机完成；可选匿名指标需要用户主动启用，并且不包含素材、文件名或本机路径。</span>
        </div>
        <p>LOCAL-FIRST<br />PRIVATE BY DESIGN</p>
      </section>

      <section className="bridge-directory" aria-labelledby="bridge-directory-title">
        <header><strong id="bridge-directory-title">软件桥接状态</strong><span>/ SOFTWARE BRIDGES</span><button type="button" aria-label="管理软件桥" onClick={() => onNavigate("integrations")}>管理软件桥 <IconArrowRight aria-hidden="true" /></button></header>
        <div className="bridge-card-grid">
          {applications.map((application) => (
            <article className="bridge-card" key={application.id}>
              <span className="bridge-mark">{application.mark}</span>
              <div className="bridge-summary">
                <div><strong>{application.name} 桥</strong><em className={`application-${application.state}`}>● {APP_STATE[application.state]}</em></div>
                <dl>
                  <div><dt>版本</dt><dd>{application.version ?? "—"}</dd></div>
                  <div><dt>Bridge</dt><dd>{application.bridge_available ? "ON" : "STANDBY"}</dd></div>
                  <div><dt>通道</dt><dd>{application.adapter_kind === "http" ? "LOOPBACK" : "LOCAL IPC"}</dd></div>
                </dl>
              </div>
              <div className="bridge-capabilities"><span>能力</span>{application.capabilities.slice(0, 3).map((capability) => <small key={capability}>{capability}</small>)}</div>
              <button type="button" onClick={() => onNavigate("integrations")}>打开 {application.mark}<IconArrowRight aria-hidden="true" /></button>
            </article>
          ))}
          {bridgeLoadingVisible ? (
            <article className="bridge-card bridge-loading" aria-live="polite">
              <span className="bridge-mark">…</span>
              <div className="bridge-summary"><div><strong>正在检测本机软件桥</strong><em>● 检测中</em></div><p>只读取固定安装、进程和回环接口线索。</p></div>
            </article>
          ) : null}
          {bridgeErrorVisible ? (
            <article className="bridge-card bridge-loading" role="alert">
              <span className="bridge-mark">!</span>
              <div className="bridge-summary"><div><strong>软件桥状态读取失败</strong><em className="application-unavailable">● 需要重试</em></div><p>{connectionsError}</p></div>
              <button type="button" onClick={() => onNavigate("integrations")}>打开连接中心<IconArrowRight aria-hidden="true" /></button>
            </article>
          ) : null}
          {bridgeEmptyVisible ? (
            <article className="bridge-card bridge-loading">
              <span className="bridge-mark">0</span>
              <div className="bridge-summary"><div><strong>尚未检测到可用软件桥</strong><em>● 待配置</em></div><p>前往连接中心检查安装状态、进程和本机桥接配置。</p></div>
              <button type="button" onClick={() => onNavigate("integrations")}>打开连接中心<IconArrowRight aria-hidden="true" /></button>
            </article>
          ) : null}
        </div>
      </section>

      <section className="home-operation-grid">
        <div className="recent-task-panel">
          <header><div><strong>最近任务</strong><span>/ EVIDENCE MANIFEST</span></div><button type="button" onClick={() => onNavigate("tasks")}>查看全部任务 <IconArrowRight aria-hidden="true" /></button></header>
          {tasks.length ? (
            <div className="task-table-wrap">
              <table className="home-task-table">
                <thead><tr><th>任务</th><th>项目</th><th>状态</th><th>进度</th><th>最后更新</th><th>证据</th></tr></thead>
                <tbody>{tasks.map((task) => (
                  <tr key={task.jobId}>
                    <td><IconFileCheck aria-hidden="true" /><span><strong>{task.currentStep}</strong><small>{task.workflowId}</small></span></td>
                    <td>{task.projectId}</td>
                    <td><span className={`task-state task-${task.status}`}>● {STATUS_LABELS[task.status]}</span></td>
                    <td><span>{task.progress}%</span><progress value={task.progress} max="100" /></td>
                    <td><time dateTime={task.updatedAt}>{formatUpdatedAt(task.updatedAt)}</time></td>
                    <td>{task.evidenceId ?? "—"}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : <EmptyState title="还没有任务记录" description="创建项目并建立工作流计划后，任务与证据会出现在这里。" />}
        </div>

        <aside className="quick-action-panel">
          <header><strong>快捷操作</strong><span>/ QUICK ACTIONS</span></header>
          <button type="button" onClick={() => onNavigate("projects")}><IconFolderPlus aria-hidden="true" /><span><strong>新建项目</strong><small>从模板或空白开始</small></span><IconArrowRight aria-hidden="true" /></button>
          <button type="button" onClick={() => onNavigate("tasks")}><IconListDetails aria-hidden="true" /><span><strong>任务中心</strong><small>查看与管理所有任务</small></span><IconArrowRight aria-hidden="true" /></button>
          <button type="button" onClick={() => onNavigate("delivery")}><IconFileCheck aria-hidden="true" /><span><strong>证据总览</strong><small>查看证据与真实交付物</small></span><IconArrowRight aria-hidden="true" /></button>
          <button type="button" onClick={() => onNavigate("integrations")}><IconPlugConnected aria-hidden="true" /><span><strong>连接检测</strong><small>检查所有桥与依赖</small></span><IconArrowRight aria-hidden="true" /></button>
        </aside>
      </section>
    </div>
  );
}
