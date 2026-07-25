import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { KORYAOClient } from "../services/client";
import type { ModelAvailability, ModelRuntimeStatus } from "../types/api";

interface ModelRuntimePageProps {
  client: KORYAOClient;
  runtimeReady: boolean;
}

function statusLabel(status: ModelRuntimeStatus["status"]) {
  if (status === "healthy") return "运行正常";
  if (status === "degraded") return "部分可用";
  return "不可用";
}

function modelStatusLabel(status: ModelAvailability) {
  if (status === "ready") return "可用";
  if (status === "experimental") return "实验中";
  if (status === "disabled") return "已停用";
  return "不可用";
}

function capabilityLabel(capability: "plan" | "evaluate" | "repair") {
  if (capability === "plan") return "任务规划";
  if (capability === "evaluate") return "结果评估";
  return "修复建议";
}

function errorMessage(reason: unknown) {
  if (reason instanceof Error && reason.message.trim()) return reason.message;
  return "无法连接本地模型运行端。";
}

export function ModelRuntimePage({ client, runtimeReady }: ModelRuntimePageProps) {
  const [status, setStatus] = useState<ModelRuntimeStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const requestIdRef = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;

    if (!runtimeReady) {
      setStatus(null);
      setLoading(false);
      setError("请先启动 KORYAO 本地服务。");
      return;
    }

    setLoading(true);
    setError("");

    try {
      const nextStatus = await client.getModelRuntimeStatus();
      if (requestId !== requestIdRef.current) return;
      setStatus(nextStatus);
    } catch (reason) {
      if (requestId !== requestIdRef.current) return;
      setStatus(null);
      setError(errorMessage(reason));
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [client, runtimeReady]);

  useEffect(() => {
    void refresh();
    return () => {
      requestIdRef.current += 1;
    };
  }, [refresh]);

  const providerCount = useMemo(
    () => new Set(status?.models.map((model) => model.providerId) ?? []).size,
    [status],
  );

  const boundarySafe = status
    ? !status.network.externalNetworkAccess
      && !status.privacy.acceptsRawAssets
      && !status.privacy.logsAbsolutePaths
      && !status.privacy.logsFullInstructions
    : false;

  return (
    <div className="standard-page model-runtime-page">
      <header className="page-intro">
        <div>
          <span className="page-kicker">本地模型 / LOOPBACK</span>
          <h2>KORYAO-C1 模型运行端</h2>
          <p>
            KORYAO 只发送经过 schema 校验的任务元数据、素材 ID 和 Adapter
            白名单。模型不直接读取磁盘，也不能绕过确认门执行软件写入。
          </p>
        </div>
        <button
          type="button"
          className="secondary"
          disabled={loading}
          aria-busy={loading}
          onClick={() => void refresh()}
        >
          {loading ? "检测中…" : "重新检测"}
        </button>
      </header>

      <div aria-live="polite">
        {error ? (
          <section className="model-runtime-offline" role="alert">
            <strong>本地模型运行端未连接</strong>
            <p>{error}</p>
            <small>请启动 KORYAO-Model-Private，并保持默认 loopback 配置。</small>
          </section>
        ) : null}

        {status ? (
          <>
            <section className="model-runtime-summary" aria-label="模型运行端摘要">
              <div>
                <span>服务状态</span>
                <strong>{statusLabel(status.status)}</strong>
              </div>
              <div>
                <span>协议</span>
                <strong>{status.schema}</strong>
              </div>
              <div>
                <span>网络</span>
                <strong>
                  {status.network.bindAddress}
                  {status.network.externalNetworkAccess ? " / EXTERNAL" : " / LOOPBACK"}
                </strong>
              </div>
              <div>
                <span>原始素材</span>
                <strong>{status.privacy.acceptsRawAssets ? "接收" : "不接收"}</strong>
              </div>
            </section>

            <section className="model-runtime-models">
              <header>
                <div>
                  <span>已注册模型</span>
                  <h3>{status.models.length} 个模型 · {providerCount} 个 Provider</h3>
                </div>
                <small>服务版本 {status.serviceVersion}</small>
              </header>

              {status.models.length > 0 ? status.models.map((model) => (
                <article key={`${model.modelId}-${model.version}`}>
                  <div>
                    <span>{model.providerId}</span>
                    <h3>{model.modelId}</h3>
                    <p>版本 {model.version}</p>
                  </div>
                  <div className="model-capabilities" aria-label={`${model.modelId} 能力`}>
                    {model.capabilities.map((capability) => (
                      <span key={capability} title={capability}>
                        {capabilityLabel(capability)}
                      </span>
                    ))}
                  </div>
                  <strong className={`model-state model-state-${model.status}`}>
                    {modelStatusLabel(model.status)}
                  </strong>
                </article>
              )) : (
                <article>
                  <div>
                    <span>未注册</span>
                    <h3>没有检测到可用模型</h3>
                    <p>请检查本地模型配置和协议版本。</p>
                  </div>
                </article>
              )}
            </section>

            <section
              className="model-runtime-boundary"
              style={{ borderLeftColor: boundarySafe ? "var(--success)" : "var(--danger)" }}
            >
              <strong>
                {boundarySafe ? "安全边界已启用" : "检测到需要处理的安全配置"}
              </strong>
              <ul>
                <li>外网访问：{status.network.externalNetworkAccess ? "开启" : "关闭"}</li>
                <li>原始素材接收：{status.privacy.acceptsRawAssets ? "开启" : "关闭"}</li>
                <li>完整指令日志：{status.privacy.logsFullInstructions ? "开启" : "关闭"}</li>
                <li>绝对路径日志：{status.privacy.logsAbsolutePaths ? "开启" : "关闭"}</li>
                <li>真实写入：仍由 KORYAO Adapter 和确认门控制</li>
              </ul>
            </section>
          </>
        ) : null}
      </div>
    </div>
  );
}
