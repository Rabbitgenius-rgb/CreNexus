import { BridgeClient } from "./bridge-client.js";
import { executeTypedBatchPlay, runModalJob, validateBatchPlay } from "./batchplay-runner.js";
import { cameraRawFixtureStatus } from "./camera-raw-fixture.js";

const photoshop = require("photoshop");
const { action, app } = photoshop;
const uxp = require("uxp");
const { entrypoints } = uxp;
const storage = uxp.storage;
const localFileSystem = storage.localFileSystem;

const CAMERA_RAW_BLOCKED_REASON = "camera_raw_batchplay_descriptor_not_recorded";
const CAMERA_RAW_NEXT_STEP = "Record a verified Camera Raw Filter descriptor with Alchemist or Photoshop Action listener and add it as a fixture.";
const CAMERA_RAW_PROTOCOL_VERSION = "camera_raw_tune.v1";
const CAMERA_RAW_OUTPUT_DIR = "examples/output/photoshop";
const CAMERA_RAW_RECORDING_LIMIT = 48;
const CAMERA_RAW_RECORDING_MAX_DEPTH = 12;
const CAMERA_RAW_RECORDING_MAX_KEYS = 128;
const CAMERA_RAW_PRIVATE_VALUE_PATTERN =
  /(?:[a-z]:[\\/]|\\\\[^\\]+\\|\/(?:users|home|private|volumes)\/|file:\/\/|https?:\/\/|(?:^|[\\/])[^\\/]+\.(?:psd|psb|jpg|jpeg|png|tif|tiff|dng|cr2|cr3|nef|arw|raf|raw)$)/i;
const CAMERA_RAW_PRIVATE_KEYS = new Set([
  "documentid",
  "layerid",
  "filename",
  "filepath",
  "fullpath",
  "path",
  "targetpath",
  "sourcepath",
  "url",
  "username",
  "userid",
  "sessionid",
  "token",
  "cookie",
  "authorization",
  "machineid",
]);
const CAMERA_RAW_DEFAULTS = {
  temperature: 4800,
  tint: 10,
  exposure: 0.35,
  contrast: 10,
  highlights: -25,
  shadows: 35,
  whites: 12,
  blacks: -12,
  texture: 18,
  clarity: 8,
  dehaze: 3,
  vibrance: 14,
  saturation: -2,
};
const CAMERA_RAW_RANGES = {
  temperature: [2000, 50000],
  tint: [-150, 150],
  exposure: [-5, 5],
  contrast: [-100, 100],
  highlights: [-100, 100],
  shadows: [-100, 100],
  whites: [-100, 100],
  blacks: [-100, 100],
  texture: [-100, 100],
  clarity: [-100, 100],
  dehaze: [-100, 100],
  vibrance: [-100, 100],
  saturation: [-100, 100],
};
const cameraRawRecording = {
  active: false,
  events: [],
  rejected: 0,
};

function isCameraRawPrivateKey(key) {
  const normalized = String(key || "")
    .replace(/[^a-z0-9]/gi, "")
    .toLowerCase();
  return CAMERA_RAW_PRIVATE_KEYS.has(normalized) || normalized.endsWith("filepath");
}

function sanitizeRecordedValue(value, depth = 0) {
  if (depth > CAMERA_RAW_RECORDING_MAX_DEPTH) {
    throw new Error("descriptor_too_deep");
  }
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("descriptor_non_finite_number");
    return value;
  }
  if (typeof value === "string") {
    if (value.length > 512) throw new Error("descriptor_string_too_long");
    if (CAMERA_RAW_PRIVATE_VALUE_PATTERN.test(value)) {
      throw new Error("descriptor_contains_private_value");
    }
    return value;
  }
  if (Array.isArray(value)) {
    if (value.length > CAMERA_RAW_RECORDING_MAX_KEYS) {
      throw new Error("descriptor_array_too_large");
    }
    return value.map((item) => sanitizeRecordedValue(item, depth + 1));
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length > CAMERA_RAW_RECORDING_MAX_KEYS) {
      throw new Error("descriptor_object_too_large");
    }
    const sanitized = {};
    for (const [key, item] of entries) {
      if (isCameraRawPrivateKey(key) || key === "_id") {
        continue;
      }
      if (key === "_options") {
        continue;
      }
      if (key === "_target" && Array.isArray(item)) {
        sanitized[key] = item.map((target) => {
          if (!target || typeof target !== "object") {
            throw new Error("descriptor_target_invalid");
          }
          const safeTarget = {};
          for (const targetKey of ["_ref", "_enum", "_value", "_property"]) {
            if (Object.prototype.hasOwnProperty.call(target, targetKey)) {
              safeTarget[targetKey] = sanitizeRecordedValue(target[targetKey], depth + 2);
            }
          }
          if (!safeTarget._ref) {
            throw new Error("descriptor_target_not_generic");
          }
          return safeTarget;
        });
        continue;
      }
      sanitized[key] = sanitizeRecordedValue(item, depth + 1);
    }
    return sanitized;
  }
  throw new Error("descriptor_value_type_not_supported");
}

function cameraRawRecordingPayload() {
  return {
    fixture_status: "captured_unverified",
    review_required: true,
    protocol_version: CAMERA_RAW_PROTOCOL_VERSION,
    photoshop_version: String(app?.version || "unknown"),
    captured_event_count: cameraRawRecording.events.length,
    rejected_event_count: cameraRawRecording.rejected,
    descriptors: cameraRawRecording.events,
  };
}

function renderCameraRawRecording(message) {
  const status = document.querySelector("#camera-raw-recorder-status");
  const output = document.querySelector("#camera-raw-recorder-output");
  const start = document.querySelector("#camera-raw-recorder-start");
  const stop = document.querySelector("#camera-raw-recorder-stop");
  setPanelText(status, message);
  if (start) start.disabled = cameraRawRecording.active;
  if (stop) stop.disabled = !cameraRawRecording.active;
  if (output) {
    output.value = cameraRawRecording.events.length
      ? JSON.stringify(cameraRawRecordingPayload(), null, 2)
      : "";
  }
}

function onCameraRawActionEvent(event, descriptor) {
  if (!cameraRawRecording.active) return;
  if (cameraRawRecording.events.length >= CAMERA_RAW_RECORDING_LIMIT) {
    cameraRawRecording.rejected += 1;
    renderCameraRawRecording(
      `已达到 ${CAMERA_RAW_RECORDING_LIMIT} 条上限，请停止录制并审查。`,
    );
    return;
  }
  try {
    const eventName = sanitizeRecordedValue(String(event || "unknown"));
    const safeDescriptor = sanitizeRecordedValue(descriptor || {});
    cameraRawRecording.events.push({
      event: eventName,
      descriptor: safeDescriptor,
    });
    renderCameraRawRecording(
      `正在录制：已捕获 ${cameraRawRecording.events.length} 条安全事件。`,
    );
  } catch (_error) {
    cameraRawRecording.rejected += 1;
    renderCameraRawRecording(
      `正在录制：已捕获 ${cameraRawRecording.events.length} 条，拒绝 ${cameraRawRecording.rejected} 条含敏感或异常数据的事件。`,
    );
  }
}

async function startCameraRawRecording() {
  if (cameraRawRecording.active) return;
  cameraRawRecording.events = [];
  cameraRawRecording.rejected = 0;
  try {
    await action.addNotificationListener(["all"], onCameraRawActionEvent);
    cameraRawRecording.active = true;
    renderCameraRawRecording(
      "录制中：现在手动执行一次最小 Camera Raw 滤镜操作，然后点击“停止并审查”。",
    );
  } catch (_error) {
    cameraRawRecording.active = false;
    renderCameraRawRecording(
      "无法开始录制。请确认 Photoshop 已开启开发人员模式并重新加载此插件。",
    );
  }
}

async function stopCameraRawRecording() {
  if (!cameraRawRecording.active) return;
  try {
    await action.removeNotificationListener(["all"], onCameraRawActionEvent);
  } finally {
    cameraRawRecording.active = false;
    renderCameraRawRecording(
      cameraRawRecording.events.length
        ? `录制已停止：${cameraRawRecording.events.length} 条已脱敏事件等待人工审查，尚未获准执行。`
        : "录制已停止，但没有捕获到可审查事件。",
    );
  }
}

function currentHost() {
  return {
    app: "Photoshop",
    version: String(app?.version || "unknown"),
  };
}

function activeDocumentOrNull() {
  return app?.activeDocument || null;
}

function safeLayerItem(layer, groupPath = []) {
  return {
    id: String(layer?._id || layer?.id || ""),
    name: String(layer?.name || ""),
    kind: String(layer?.kind || "layer"),
    type: String(layer?.kind || "layer"),
    visible: Boolean(layer?.visible ?? true),
    locked: Boolean(layer?.locked ?? false),
    opacity: Number(layer?.opacity ?? 100),
    blendMode: String(layer?.blendMode || "normal"),
    bounds: layer?.bounds ? {
      left: Number(layer.bounds.left || 0),
      top: Number(layer.bounds.top || 0),
      right: Number(layer.bounds.right || 0),
      bottom: Number(layer.bounds.bottom || 0),
    } : null,
    group_path: groupPath.join("/"),
  };
}

function flattenLayers(layers, groupPath = []) {
  const rows = [];
  for (const layer of layers || []) {
    rows.push(safeLayerItem(layer, groupPath));
    if (layer?.layers?.length) {
      rows.push(...flattenLayers(layer.layers, [...groupPath, String(layer.name || "")]));
    }
  }
  return rows;
}

function cameraRawPlan(params = {}) {
  const preset = String(params.preset || "blue_artwork_clean");
  const values = { ...CAMERA_RAW_DEFAULTS };
  const errors = [];
  if (preset !== "blue_artwork_clean") {
    errors.push("preset must be blue_artwork_clean");
  }
  const supplied = params.params || {};
  for (const [key, value] of Object.entries(supplied)) {
    if (!Object.prototype.hasOwnProperty.call(CAMERA_RAW_RANGES, key)) {
      errors.push(`params.${key} is not supported`);
      continue;
    }
    if (typeof value !== "number" || !Number.isFinite(value)) {
      errors.push(`params.${key} must be numeric`);
      continue;
    }
    const [minimum, maximum] = CAMERA_RAW_RANGES[key];
    if (value < minimum || value > maximum) {
      errors.push(`params.${key} must be between ${minimum} and ${maximum}`);
      continue;
    }
    values[key] = value;
  }
  const rawSource = params.source || { mode: "active_document" };
  const sourceMode = String(rawSource.mode || "active_document");
  const source = { mode: sourceMode };
  if (!["active_document", "explicit_path"].includes(sourceMode)) {
    errors.push("source.mode must be active_document or explicit_path");
  }
  if (sourceMode === "explicit_path") {
    if (!rawSource.path && rawSource.path_provided !== true) {
      errors.push("source.path is required when source.mode is explicit_path");
    } else {
      source.path_provided = true;
      source.read_policy = "user_explicit_path_only";
    }
  }
  const rawOutput = params.output || {};
  const outputDir = String(rawOutput.dir || CAMERA_RAW_OUTPUT_DIR).replaceAll("\\", "/");
  if (outputDir !== CAMERA_RAW_OUTPUT_DIR) {
    errors.push(`output.dir must stay inside ${CAMERA_RAW_OUTPUT_DIR}`);
  }
  const formats = Array.isArray(rawOutput.formats) && rawOutput.formats.length ? rawOutput.formats.map((item) => String(item).toLowerCase()) : ["jpg"];
  const unsupportedFormats = formats.filter((item) => !["jpg", "png"].includes(item));
  if (unsupportedFormats.length) {
    errors.push(`output.formats contains unsupported values: ${unsupportedFormats.join(", ")}`);
  }
  const basename = String(rawOutput.basename || "camera_raw_tune_preview");
  if (!basename || basename.includes("/") || basename.includes("\\") || basename.includes(":") || basename.includes("..")) {
    errors.push("output.basename must be a simple file stem");
  }
  return {
    errors,
    plan: {
      protocol_version: CAMERA_RAW_PROTOCOL_VERSION,
      method: "ps.camera_raw.tune",
      preset,
      params: values,
      source,
      output: {
        dir: outputDir,
        basename,
        formats,
        export_after_apply: Boolean(rawOutput.export_after_apply),
      },
      descriptor_status: cameraRawFixtureStatus().verified ? "reviewed" : "missing",
      execution_path: ["Codex", "KORYAO MCP", "Node Proxy", "UXP Plugin", "Photoshop"],
    },
  };
}

async function ping() {
  return {
    plugin_status: "ok",
    photoshop_host: currentHost(),
    plugin_version: "1.0.0",
    now: new Date().toISOString(),
  };
}

async function cameraRawTune(params) {
  if (
    Object.prototype.hasOwnProperty.call(params || {}, "descriptor") ||
    Object.prototype.hasOwnProperty.call(params || {}, "descriptors") ||
    Object.prototype.hasOwnProperty.call(params || {}, "descriptor_fixture_path") ||
    Object.prototype.hasOwnProperty.call(params || {}, "descriptor_fixture_verified")
  ) {
    return {
      ok: false,
      executed: false,
      blocked_reason: "caller_supplied_camera_raw_descriptor_forbidden",
      photoshop_host: currentHost(),
    };
  }
  const dryRun = params?.dry_run !== false;
  const confirmApply = Boolean(params?.confirm_apply);
  const confirmExport = Boolean(params?.confirm_export);
  const { errors, plan } = cameraRawPlan(params || {});
  if (errors.length) {
    return { ok: false, errors, plan, photoshop_host: currentHost() };
  }
  if (dryRun) {
    return {
      ok: true,
      dry_run: true,
      confirm_apply: confirmApply,
      confirm_export: confirmExport,
      plan,
      photoshop_host: currentHost(),
      warnings: ["Camera Raw tuning dry-run validated; Photoshop was not modified."],
    };
  }
  if (!confirmApply) {
    return {
      ok: false,
      dry_run: false,
      confirm_apply: false,
      confirm_export: confirmExport,
      plan,
      photoshop_host: currentHost(),
      message: "confirm_apply=true is required when dry_run=false.",
    };
  }
  if (plan.output.export_after_apply && !confirmExport) {
    return {
      ok: false,
      dry_run: false,
      confirm_apply: true,
      confirm_export: false,
      plan,
      photoshop_host: currentHost(),
      message: "confirm_export=true is required when output.export_after_apply=true.",
    };
  }
  const fixtureStatus = cameraRawFixtureStatus();
  if (
    !fixtureStatus.verified ||
    fixtureStatus.fixture_id !== String(params?.camera_raw_fixture_id || "")
  ) {
    return {
      ok: false,
      executed: false,
      dry_run: false,
      confirm_apply: true,
      confirm_export: confirmExport,
      blocked_reason: CAMERA_RAW_BLOCKED_REASON,
      next_step: CAMERA_RAW_NEXT_STEP,
      plan,
      photoshop_host: currentHost(),
    };
  }
  return {
    ok: false,
    executed: false,
    dry_run: false,
    confirm_apply: true,
    confirm_export: confirmExport,
    blocked_reason: "camera_raw_verified_export_not_connected",
    next_step: "Connect reviewed Camera Raw apply to managed staging export, native reopen validation, and cleanup before enabling real execution.",
    plan,
    photoshop_host: currentHost(),
  };
}

async function documentInfo() {
  const document = activeDocumentOrNull();
  if (!document) {
    return {
      ok: true,
      installed: true,
      active_document: false,
      message: "No active Photoshop document.",
      photoshop_host: currentHost(),
    };
  }
  const activeLayer = document.activeLayers?.[0] || null;
  return {
    ok: true,
    active_document: true,
    photoshop_host: currentHost(),
    document: {
      document_id: String(document._id || document.id || ""),
      title: String(document.title || document.name || ""),
      name: String(document.title || document.name || ""),
      width: Number(document.width || 0),
      height: Number(document.height || 0),
      resolution: Number(document.resolution || 72),
      color_mode: String(document.mode || ""),
      bit_depth: Number(document.bitsPerChannel || 8),
      active_layer_id: String(activeLayer?._id || activeLayer?.id || ""),
      active_layer_name: String(activeLayer?.name || ""),
      layer_count: Array.isArray(document.layers) ? document.layers.length : 0,
      saved: typeof document.saved === "boolean" ? document.saved : null,
    },
  };
}

async function layersList() {
  const document = activeDocumentOrNull();
  if (!document) {
    return { ok: false, installed: true, message: "No active Photoshop document.", layers: [] };
  }
  return {
    ok: true,
    photoshop_host: currentHost(),
    layers: flattenLayers(document.layers || []),
  };
}

function splitOutputPath(rawPath) {
  const normalized = String(rawPath || "").replace(/\\/g, "/");
  if (!normalized) {
    return { folderUrl: "", filename: "", absolute: "" };
  }
  const lastSlash = normalized.lastIndexOf("/");
  if (lastSlash <= 0) {
    return { folderUrl: "", filename: normalized, absolute: normalized };
  }
  return {
    folderUrl: normalized.slice(0, lastSlash),
    filename: normalized.slice(lastSlash + 1),
    absolute: normalized,
  };
}

function toFileUrl(absoluteFolder) {
  if (!absoluteFolder) {
    return "";
  }
  if (absoluteFolder.startsWith("file:")) {
    return absoluteFolder;
  }
  if (/^[A-Za-z]:/.test(absoluteFolder)) {
    return "file:///" + absoluteFolder;
  }
  return "file://" + absoluteFolder;
}

function assertSandboxOutputPath(params) {
  const normalized = String(params?.output_path || "").replaceAll("\\", "/");
  const absolute = /^[A-Za-z]:\//.test(normalized) || normalized.startsWith("/");
  const allowedMarker = ["/sandbox/", "/output/", "/examples/output/photoshop/"].some(marker => normalized.includes(marker));
  if (params?.sandbox_verified !== true || !absolute || !allowedMarker || !normalized.toLowerCase().endsWith(".png") || normalized.includes("/../")) {
    throw new Error("output_path_outside_sandbox");
  }
  return normalized;
}

async function resolveOutputEntry(absolutePath) {
  const { folderUrl, filename } = splitOutputPath(absolutePath);
  if (!folderUrl || !filename) {
    throw new Error("output_path must be an absolute path with a filename");
  }
  const folderEntry = await localFileSystem.getEntryWithUrl(toFileUrl(folderUrl));
  if (!folderEntry || !folderEntry.isFolder) {
    throw new Error("output folder is not a directory: " + folderUrl);
  }
  const fileEntry = await folderEntry.createFile(filename, { overwrite: true });
  return fileEntry;
}

async function saveActiveDocumentAsPng(document, absolutePath) {
  const fileEntry = await resolveOutputEntry(absolutePath);
  const saveAs = document.saveAs || (document.api && document.api.saveAs);
  if (!saveAs || typeof saveAs.png !== "function") {
    throw new Error("Photoshop UXP DOM does not expose document.saveAs.png on this host");
  }
  await saveAs.png(fileEntry, { compression: 6, interlaced: false }, true);
  return fileEntry;
}

async function activateDocument(document) {
  if (typeof document?.activate === "function") {
    await document.activate();
  }
}

async function closeDocumentWithoutSaving(document) {
  if (typeof document?.closeWithoutSaving === "function") {
    await document.closeWithoutSaving();
    return;
  }
  throw new Error("photoshop_close_without_saving_unavailable");
}

async function saveProductionCopy(document, absolutePath, format) {
  const fileEntry = await resolveOutputEntry(absolutePath);
  const saveAs = document?.saveAs || document?.api?.saveAs;
  if (!saveAs) throw new Error("photoshop_save_as_unavailable");
  if (format === "png" || format === "subject") {
    if (typeof saveAs.png !== "function") throw new Error("photoshop_png_export_unavailable");
    await saveAs.png(fileEntry, { compression: 6, interlaced: false }, true);
  } else if (format === "jpeg") {
    if (typeof saveAs.jpg !== "function") throw new Error("photoshop_jpeg_export_unavailable");
    await saveAs.jpg(fileEntry, { quality: 10 }, true);
  } else if (format === "psd") {
    if (typeof saveAs.psd !== "function") throw new Error("photoshop_psd_export_unavailable");
    await saveAs.psd(fileEntry, {}, true);
  } else {
    throw new Error("unsupported_production_format");
  }
}

async function validateNativePsdReopen(absolutePath, sandboxDocument) {
  const entry = await localFileSystem.getEntryWithUrl(toFileUrl(absolutePath));
  if (!entry || !entry.isFile) throw new Error("photoshop_psd_reopen_file_unavailable");
  const reopened = await app.open(entry);
  try {
    const width = Number(reopened?.width || 0);
    const height = Number(reopened?.height || 0);
    const layerCount = Array.isArray(reopened?.layers) ? reopened.layers.length : 0;
    const expectedWidth = Number(sandboxDocument?.width || 0);
    const expectedHeight = Number(sandboxDocument?.height || 0);
    const expectedLayerCount = Array.isArray(sandboxDocument?.layers)
      ? sandboxDocument.layers.length
      : 0;
    if (
      width <= 0
      || height <= 0
      || width !== expectedWidth
      || height !== expectedHeight
      || layerCount < 1
      || layerCount !== expectedLayerCount
    ) throw new Error("photoshop_psd_reopen_invalid_document");
    return {
      validated: true,
      width,
      height,
      layer_count: layerCount,
    };
  } finally {
    await closeDocumentWithoutSaving(reopened);
    await activateDocument(sandboxDocument);
  }
}

function assertProductionParams(params) {
  if (params?.confirm_write !== true || params?.managed_source_verified !== true || params?.safe_roots_verified !== true) {
    throw new Error("production_proxy_verification_required");
  }
  const sourcePath = String(params?.source_path || "");
  const stagingOutputs = params?.staging_outputs || {};
  if (!sourcePath || !Object.keys(stagingOutputs).length) throw new Error("production_paths_required");
  return { sourcePath, stagingOutputs };
}

async function applyProductionAdjustments(params) {
  const canvas = params?.canvas || {};
  const adjustment = params?.adjustment || {};
  const descriptors = [];
  if (canvas.resize === true) {
    descriptors.push({
      _obj: "canvasSize",
      width: { _unit: "pixelsUnit", _value: Number(canvas.width) },
      height: { _unit: "pixelsUnit", _value: Number(canvas.height) },
      horizontal: { _enum: "horizontalLocation", _value: "horizontalCenter" },
      vertical: { _enum: "verticalLocation", _value: "verticalCenter" },
    });
  }
  const brightness = Number(adjustment.brightness || 0);
  const contrast = Number(adjustment.contrast || 0);
  if (brightness !== 0 || contrast !== 0) {
    descriptors.push({
      _obj: "make",
      _target: [{ _ref: "adjustmentLayer" }],
      using: {
        _obj: "adjustmentLayer",
        name: "StarBridge Brightness Contrast",
        type: { _obj: "brightnessEvent", brightness, contrast, useLegacy: false },
      },
    });
  }
  const saturation = Number(adjustment.saturation || 0);
  if (saturation !== 0) {
    descriptors.push({
      _obj: "make",
      _target: [{ _ref: "adjustmentLayer" }],
      using: {
        _obj: "adjustmentLayer",
        name: "StarBridge Saturation",
        type: {
          _obj: "hueSaturation",
          presetKind: { _enum: "presetKindType", _value: "presetKindCustom" },
          saturation,
        },
      },
    });
  }
  if (descriptors.length) await action.batchPlay(descriptors, { synchronousExecution: true, modalBehavior: "execute" });
  return descriptors.length;
}

async function exportSubjectCopy(document, absolutePath) {
  await action.batchPlay([
    { _obj: "autoCutout", sampleAllLayers: false },
    { _obj: "copyToLayer" },
  ], { synchronousExecution: true, modalBehavior: "execute" });
  const subjectLayer = document?.activeLayers?.[0];
  if (!subjectLayer) throw new Error("photoshop_subject_layer_unavailable");
  subjectLayer.name = "StarBridge Subject";
  const visibility = (document.layers || []).map((layer) => ({ layer, visible: Boolean(layer.visible) }));
  try {
    for (const item of visibility) item.layer.visible = item.layer === subjectLayer;
    await saveProductionCopy(document, absolutePath, "subject");
  } finally {
    for (const item of visibility) item.layer.visible = item.visible;
  }
}

async function productionExecuteConfirmed(params) {
  const { sourcePath, stagingOutputs } = assertProductionParams(params);
  return runModalJob(
    "ps.production.execute_confirmed",
    { commandName: "StarBridge Photoshop Production", historyTarget: "handler_document", timeoutSeconds: 45 },
    async (executionContext, modalControl) => {
      const hostControl = executionContext?.hostControl;
      if (typeof hostControl?.registerAutoCloseDocument !== "function" || typeof hostControl?.unregisterAutoCloseDocument !== "function") {
        throw new Error("photoshop_auto_close_control_required");
      }
      modalControl.checkpoint();
      const sourceEntry = await localFileSystem.getEntryWithUrl(toFileUrl(sourcePath));
      if (!sourceEntry || !sourceEntry.isFile) {
        throw new Error("managed_source_file_unavailable");
      }
      let sourceDocument = null;
      let sandboxDocument = null;
      let sandboxId = null;
      try {
        sourceDocument = await app.open(sourceEntry);
        if (!sourceDocument || typeof sourceDocument.duplicate !== "function") {
          throw new Error("managed_source_document_unavailable");
        }
        sandboxDocument = await sourceDocument.duplicate("StarBridge Sandbox Copy", false);
        sandboxId = sandboxDocument?.id ?? sandboxDocument?._id;
        if (sandboxId === undefined || sandboxId === null) {
          throw new Error("sandbox_document_id_unavailable");
        }
        await hostControl.registerAutoCloseDocument(sandboxId);
      } finally {
        if (sourceDocument) await closeDocumentWithoutSaving(sourceDocument);
      }
      await modalControl.suspendHistory(sandboxId, "StarBridge Photoshop Production");
      await activateDocument(sandboxDocument);
      modalControl.checkpoint();
      const adjustmentCount = await applyProductionAdjustments(params);
      modalControl.checkpoint();
      for (const [format, outputPath] of Object.entries(stagingOutputs)) {
        if (format !== "subject") await saveProductionCopy(sandboxDocument, String(outputPath), format);
      }
      if (params?.export_subject === true) {
        if (!stagingOutputs.subject) throw new Error("subject_output_path_required");
        await exportSubjectCopy(sandboxDocument, String(stagingOutputs.subject));
      }
      const nativeReopen = stagingOutputs.psd
        ? await validateNativePsdReopen(String(stagingOutputs.psd), sandboxDocument)
        : { validated: false, skipped: true };
      modalControl.checkpoint();
      await closeDocumentWithoutSaving(sandboxDocument);
      await hostControl.unregisterAutoCloseDocument(sandboxId);
      return {
        ok: true,
        executed: true,
        sandbox_copy: true,
        source_overwritten: false,
        managed_source_opened_read_only: true,
        adjustment_count: adjustmentCount,
        output_formats: Object.keys(stagingOutputs),
        native_reopen_validated: nativeReopen.validated === true,
        native_reopen_skipped: nativeReopen.skipped === true,
        native_reopen_width: Number(nativeReopen.width || 0),
        native_reopen_height: Number(nativeReopen.height || 0),
        native_reopen_layer_count: Number(nativeReopen.layer_count || 0),
        rollback_supported: true,
        photoshop_host: currentHost(),
        warnings: [],
      };
    },
  );
}

async function previewExport(params) {
  const document = activeDocumentOrNull();
  if (!document) {
    return { ok: false, installed: true, message: "No active Photoshop document." };
  }
  if (params?.dry_run === true) {
    return {
      ok: true,
      dry_run: true,
      preview_path: String(params.output_path || ""),
      document_name: String(document.title || document.name || ""),
      width: Number(document.width || 0),
      height: Number(document.height || 0),
      photoshop_host: currentHost(),
      warnings: ["dry_run=true: no PNG was written."],
    };
  }
  if (!params?.confirm_write) {
    return { ok: false, message: "confirm_write=true is required." };
  }
  const absolutePath = String(params?.output_path || "");
  if (!absolutePath) {
    return { ok: false, message: "output_path is required for real preview export." };
  }
  assertSandboxOutputPath(params);
  return runModalJob("ps.preview.export", { commandName: "KORYAO Preview Export" }, async () => {
    const fileEntry = await saveActiveDocumentAsPng(document, absolutePath);
    return {
      ok: true,
      executed: true,
      preview_path: absolutePath,
      written_path: String(fileEntry?.nativePath || absolutePath),
      document_name: String(document.title || document.name || ""),
      width: Number(document.width || 0),
      height: Number(document.height || 0),
      format: "png",
      layers_snapshot: flattenLayers(document.layers || []),
      photoshop_host: currentHost(),
      warnings: [],
    };
  });
}

async function batchplayValidate(params) {
  const descriptors = params?.descriptors || (params?.descriptor ? [params.descriptor] : []);
  const validations = await validateBatchPlay(descriptors);
  return {
    ok: validations.every((item) => item.allowed),
    validations,
    photoshop_host: currentHost(),
  };
}

async function batchplayExecuteConfirmed(params) {
  const descriptors = params?.descriptors || (params?.descriptor ? [params.descriptor] : []);
  const result = await executeTypedBatchPlay({
    descriptors,
    requireConfirmation: Boolean(params?.confirm_write),
    sandboxOnly: true,
    commandName: "KORYAO Typed BatchPlay",
  });
  const document = activeDocumentOrNull();
  return {
    ...result,
    preview_path: String(params?.output_path || ""),
    layers_snapshot: document ? flattenLayers(document.layers || []) : [],
    photoshop_host: currentHost(),
  };
}

const handlers = {
  "starbridge.ping": ping,
  "ps.document.info": documentInfo,
  "ps.layers.list": layersList,
  "ps.preview.export": previewExport,
  "ps.camera_raw.tune": cameraRawTune,
  "ps.batchplay.validate.local": batchplayValidate,
  "ps.batchplay.execute_confirmed": batchplayExecuteConfirmed,
  "ps.production.execute_confirmed": productionExecuteConfirmed,
};

const panelElements = {
  card: document.querySelector("#session-card"),
  phase: document.querySelector("#session-phase"),
  step: document.querySelector("#session-step"),
  message: document.querySelector("#session-message"),
  progress: document.querySelector("#session-progress"),
  progressTrack: document.querySelector(".progress-track"),
  mode: document.querySelector("#session-mode"),
  time: document.querySelector("#session-time"),
  connection: document.querySelector("#connection"),
};

const phaseLabels = {
  queued: "已排队",
  running: "Codex 正在工作",
  completed: "已完成",
  failed: "执行失败",
  cancelled: "已取消",
  needs_user: "等待确认",
};

function setPanelText(element, value) {
  if (element) element.textContent = value;
}

function onBridgeStatus(status) {
  const labels = { connecting: "连接中", connected: "已连接", disconnected: "已断开", error: "连接异常" };
  setPanelText(panelElements.connection, labels[status] || status);
}

function onLiveSession(update) {
  const progress = Math.max(0, Math.min(100, Number(update?.progress || 0)));
  if (panelElements.card) panelElements.card.dataset.phase = String(update?.phase || "idle");
  setPanelText(panelElements.phase, phaseLabels[update?.phase] || String(update?.phase || "等待任务"));
  setPanelText(panelElements.step, `${update?.step?.index || 0}/${update?.step?.total || 0} · ${update?.step?.label || ""}`);
  setPanelText(panelElements.message, String(update?.message || ""));
  setPanelText(panelElements.mode, update?.mode === "computer_use" ? "界面操作" : "结构化命令");
  setPanelText(panelElements.time, update?.at ? new Date(update.at).toLocaleTimeString() : "—");
  if (panelElements.progress) panelElements.progress.style.width = `${progress}%`;
  if (panelElements.progressTrack) panelElements.progressTrack.setAttribute("aria-valuenow", String(progress));
}

const client = new BridgeClient({ handlers, onStatus: onBridgeStatus, onSession: onLiveSession });
document.querySelector("#reconnect")?.addEventListener("click", () => client.reconnect());
document
  .querySelector("#camera-raw-recorder-start")
  ?.addEventListener("click", () => startCameraRawRecording());
document
  .querySelector("#camera-raw-recorder-stop")
  ?.addEventListener("click", () => stopCameraRawRecording());
renderCameraRawRecording("尚未录制。此入口只生成待审查候选，不会执行 Camera Raw。");
client.connect();

if (entrypoints) {
  entrypoints.setup({
    commands: {
      starbridgePing: async () => ping(),
    },
    panels: {
      starbridgePhotoshopLivePanel: {
        show() {
          onBridgeStatus(client.connected ? "connected" : "connecting");
        },
      },
    },
  });
}
