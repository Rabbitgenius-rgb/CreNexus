use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
#[cfg(windows)]
use std::process::{Command, Stdio};
#[cfg(windows)]
use std::thread;
#[cfg(windows)]
use std::time::{Duration, Instant};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::AppHandle;

use super::{starbridge_data_root, valid_vector_id};

#[cfg(windows)]
const EXPORT_TIMEOUT: Duration = Duration::from_secs(120);
#[cfg(not(windows))]
const NATIVE_ADOBE_UNAVAILABLE: &str =
    "PSD/AI 原生导出当前仅支持 Windows；当前平台不会读取来源、打开保存窗口或创建暂存文件。";

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AdobeExportReceipt {
    receipt_id: String,
    format: String,
    file_name: String,
    size_bytes: u64,
    source_basename: String,
    sha256: String,
    created_at_unix_seconds: u64,
    native_reopen_validated: bool,
    #[serde(default)]
    save_completion_validated: bool,
    #[serde(default)]
    artboard_count: u32,
    #[serde(default)]
    page_item_count: u32,
    #[serde(default)]
    stable_size_samples: u32,
    #[serde(default)]
    recovery_attempts: u8,
    source_overwritten: bool,
    target_path_persisted: bool,
    history_recorded: bool,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AdobeBatchExportResult {
    batch_id: String,
    item_count: usize,
    completed_count: usize,
    failed_count: usize,
    needs_user: bool,
    resumed: bool,
    receipts: Vec<AdobeExportReceipt>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct AdobeBatchItemState {
    item_id: String,
    source_relative_path: String,
    target_file_name: String,
    status: String,
    attempts: u8,
    #[serde(default)]
    error_code: Option<String>,
    #[serde(default)]
    receipt: Option<AdobeExportReceipt>,
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct AdobeBatchState {
    schema_version: u8,
    batch_id: String,
    project_id: String,
    format: String,
    target_directory_hash: String,
    target_path_persisted: bool,
    items: Vec<AdobeBatchItemState>,
}

#[derive(Clone, Default, Deserialize)]
#[serde(rename_all = "camelCase")]
struct NativeExportEvidence {
    #[serde(default)]
    artboard_count: u32,
    #[serde(default)]
    page_item_count: u32,
    #[serde(default)]
    stable_size_samples: u32,
    #[serde(default)]
    attempts: u8,
}

fn receipt_directory(data_root: &Path, project_id: &str) -> PathBuf {
    data_root.join("adobe-export-receipts").join(project_id)
}

fn hash_file(path: &Path) -> Result<String, String> {
    let mut file =
        fs::File::open(path).map_err(|_| "无法读取已验证的 Adobe 交付文件。".to_string())?;
    let mut digest = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|_| "无法计算 Adobe 交付文件校验值。".to_string())?;
        if read == 0 {
            break;
        }
        digest.update(&buffer[..read]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn hash_text(value: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(value.as_bytes());
    format!("{:x}", digest.finalize())
}

fn persist_receipt(
    data_root: &Path,
    project_id: &str,
    receipt: &AdobeExportReceipt,
) -> Result<(), String> {
    let directory = receipt_directory(data_root, project_id);
    fs::create_dir_all(&directory).map_err(|_| "无法创建 Adobe 导出历史目录。".to_string())?;
    let encoded =
        serde_json::to_vec_pretty(receipt).map_err(|_| "无法编码 Adobe 导出历史。".to_string())?;
    let target = directory.join(format!("receipt-{}.json", receipt.receipt_id));
    let temporary = directory.join(format!(".receipt-{}.tmp", receipt.receipt_id));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|_| "无法创建 Adobe 导出历史临时文件。".to_string())?;
    if file.write_all(&encoded).is_err() || file.sync_all().is_err() {
        drop(file);
        let _ = fs::remove_file(&temporary);
        return Err("无法完整写入 Adobe 导出历史。".into());
    }
    drop(file);
    if fs::rename(&temporary, &target).is_err() {
        let _ = fs::remove_file(&temporary);
        return Err("无法提交 Adobe 导出历史。".into());
    }
    Ok(())
}

fn persist_batch_state(data_root: &Path, state: &AdobeBatchState) -> Result<(), String> {
    let directory = data_root
        .join("adobe-export-batches")
        .join(&state.project_id);
    fs::create_dir_all(&directory).map_err(|_| "无法创建 Adobe 批量恢复目录。".to_string())?;
    let target = directory.join(format!("batch-{}.json", state.batch_id));
    let temporary = directory.join(format!(".batch-{}.tmp", state.batch_id));
    let previous = directory.join(format!(".batch-{}.previous", state.batch_id));
    let encoded = serde_json::to_vec_pretty(state)
        .map_err(|_| "无法编码 Adobe 批量恢复状态。".to_string())?;
    if temporary.exists() {
        fs::remove_file(&temporary).map_err(|_| "无法更新 Adobe 批量恢复临时文件。".to_string())?;
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|_| "无法创建 Adobe 批量恢复临时文件。".to_string())?;
    if file.write_all(&encoded).is_err() || file.sync_all().is_err() {
        drop(file);
        let _ = fs::remove_file(&temporary);
        return Err("无法完整写入 Adobe 批量恢复状态。".into());
    }
    drop(file);
    if previous.exists() {
        fs::remove_file(&previous).map_err(|_| "无法清理 Adobe 批量恢复旧版本。".to_string())?;
    }
    if target.exists() {
        fs::rename(&target, &previous)
            .map_err(|_| "无法准备 Adobe 批量恢复状态更新。".to_string())?;
    }
    if fs::rename(&temporary, &target).is_err() {
        let _ = fs::remove_file(&temporary);
        if previous.exists() && !target.exists() {
            let _ = fs::rename(&previous, &target);
        }
        return Err("无法提交 Adobe 批量恢复状态。".into());
    }
    let _ = fs::remove_file(&previous);
    Ok(())
}

fn read_batch_state(data_root: &Path, project_id: &str, batch_id: &str) -> Option<AdobeBatchState> {
    let directory = data_root.join("adobe-export-batches").join(project_id);
    for path in [
        directory.join(format!("batch-{batch_id}.json")),
        directory.join(format!(".batch-{batch_id}.previous")),
    ] {
        let Ok(bytes) = fs::read(path) else {
            continue;
        };
        if bytes.len() <= 512 * 1024 {
            if let Ok(state) = serde_json::from_slice(&bytes) {
                return Some(state);
            }
        }
    }
    None
}

fn read_receipts(data_root: &Path, project_id: &str) -> Vec<AdobeExportReceipt> {
    let Ok(entries) = fs::read_dir(receipt_directory(data_root, project_id)) else {
        return Vec::new();
    };
    let mut receipts = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        let valid_name = path
            .file_name()
            .and_then(|value| value.to_str())
            .is_some_and(|value| value.starts_with("receipt-") && value.ends_with(".json"));
        let valid_size = entry
            .metadata()
            .map(|metadata| metadata.is_file() && metadata.len() <= 64 * 1024)
            .unwrap_or(false);
        if !valid_name || !valid_size {
            continue;
        }
        let Ok(bytes) = fs::read(path) else {
            continue;
        };
        let Ok(receipt) = serde_json::from_slice::<AdobeExportReceipt>(&bytes) else {
            continue;
        };
        if receipt.target_path_persisted
            || receipt.source_overwritten
            || !receipt.native_reopen_validated
            || !matches!(receipt.format.as_str(), "psd" | "ai")
        {
            continue;
        }
        receipts.push(receipt);
    }
    receipts.sort_by(|left, right| {
        right
            .created_at_unix_seconds
            .cmp(&left.created_at_unix_seconds)
            .then_with(|| right.receipt_id.cmp(&left.receipt_id))
    });
    receipts.truncate(100);
    receipts
}

fn valid_relative_artifact(relative_path: &str) -> bool {
    !relative_path.is_empty()
        && !relative_path.starts_with('/')
        && !relative_path.contains(['\\', ':'])
        && !relative_path.chars().any(char::is_control)
        && relative_path
            .split('/')
            .all(|segment| !segment.is_empty() && !matches!(segment, "." | ".."))
}

fn svg_has_external_reference(svg: &str) -> bool {
    let mut remaining = svg;
    while let Some(index) = remaining.find("href") {
        let after_name = &remaining[index + 4..];
        let trimmed = after_name.trim_start();
        if let Some(after_equals) = trimmed.strip_prefix('=') {
            let value = after_equals.trim_start();
            let parsed = if let Some(quoted) = value.strip_prefix('"') {
                quoted.split_once('"').map(|(candidate, _)| candidate)
            } else if let Some(quoted) = value.strip_prefix('\'') {
                quoted.split_once('\'').map(|(candidate, _)| candidate)
            } else {
                value.split_whitespace().next()
            };
            if parsed.is_some_and(|candidate| !candidate.is_empty() && !candidate.starts_with('#'))
            {
                return true;
            }
        }
        remaining = after_name;
    }

    let mut css = svg;
    while let Some(index) = css.find("url(") {
        let after = &css[index + 4..];
        let Some((raw_value, tail)) = after.split_once(')') else {
            return true;
        };
        let value = raw_value.trim().trim_matches(['\'', '"']);
        if !value.starts_with('#') {
            return true;
        }
        css = tail;
    }
    false
}

fn validate_svg_source(path: &Path) -> Result<(), String> {
    let metadata = fs::metadata(path).map_err(|_| "AI 来源 SVG 不可读取。".to_string())?;
    if !metadata.is_file() || metadata.len() < 16 || metadata.len() > 128 * 1024 * 1024 {
        return Err("AI 来源 SVG 为空或超过 128 MB 安全上限。".into());
    }
    let bytes = fs::read(path).map_err(|_| "AI 来源 SVG 不可读取。".to_string())?;
    let svg = std::str::from_utf8(&bytes)
        .map_err(|_| "AI 来源 SVG 必须是有效 UTF-8 文本。".to_string())?
        .to_ascii_lowercase();
    if !svg.contains("<svg") {
        return Err("AI 来源不是有效 SVG 文档。".into());
    }
    for forbidden in [
        "<script",
        "<image",
        "<foreignobject",
        "<!doctype",
        "<!entity",
        "javascript:",
        "data:",
        "@import",
    ] {
        if svg.contains(forbidden) {
            return Err("AI 来源 SVG 含脚本、嵌入位图或不允许的外部内容。".into());
        }
    }
    if svg_has_external_reference(&svg) {
        return Err("AI 来源 SVG 含外链资源；仅允许文档内部 # 引用。".into());
    }
    if ![
        "<path",
        "<rect",
        "<circle",
        "<ellipse",
        "<line",
        "<polyline",
        "<polygon",
        "<text",
    ]
    .iter()
    .any(|tag| svg.contains(tag))
    {
        return Err("AI 来源 SVG 没有可交付的矢量或文本对象。".into());
    }
    Ok(())
}

fn resolve_source(
    data_root: &Path,
    project_id: &str,
    relative_path: &str,
    format: &str,
) -> Result<PathBuf, String> {
    if !valid_vector_id(project_id, "project-") || !valid_relative_artifact(relative_path) {
        return Err("交付来源无效，请重新选择项目产物。".into());
    }
    let artifacts_root = data_root.join("artifacts");
    let project_root = artifacts_root.join(project_id);
    let source = data_root.join(relative_path);
    let resolved_project = project_root
        .canonicalize()
        .map_err(|_| "项目交付目录不存在。".to_string())?;
    let resolved_source = source
        .canonicalize()
        .map_err(|_| "所选交付产物不存在。".to_string())?;
    if !resolved_source.is_file() || !resolved_source.starts_with(&resolved_project) {
        return Err("所选产物超出当前项目的安全交付目录。".into());
    }
    let extension = resolved_source
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    let compatible = match format {
        "psd" => matches!(extension.as_str(), "png" | "jpg" | "jpeg"),
        "ai" => extension == "svg",
        _ => false,
    };
    if !compatible {
        return Err(match format {
            "psd" => "PSD 导出请选择真实 PNG 或 JPEG 预览产物。".into(),
            "ai" => "AI 导出请选择真实 SVG 矢量产物。".into(),
            _ => "只支持导出 PSD 或 AI 文件。".into(),
        });
    }
    if format == "ai" {
        validate_svg_source(&resolved_source)?;
    }
    Ok(resolved_source)
}

fn normalized_target(mut target: PathBuf, format: &str) -> Result<PathBuf, String> {
    match target.extension().and_then(|value| value.to_str()) {
        None => {
            target.set_extension(format);
        }
        Some(extension) if extension.eq_ignore_ascii_case(format) => {}
        Some(_) => return Err(format!("目标文件扩展名必须是 .{format}。")),
    }
    if target.exists() {
        return Err("目标文件已经存在。KORYAO 不会覆盖它，请选择新文件名。".into());
    }
    if !target.is_absolute() {
        return Err("请选择一个完整的本机保存路径。".into());
    }
    Ok(target)
}

#[cfg(windows)]
const PHOTOSHOP_SCRIPT: &str = r#"
$ErrorActionPreference = 'Stop'
$source = $env:KORYAO_ADOBE_SOURCE
$target = $env:KORYAO_ADOBE_TARGET
if (-not $source -or -not $target) { throw 'missing export environment' }
$sourceJs = $source.Replace('\', '/') | ConvertTo-Json -Compress
$targetJs = $target.Replace('\', '/') | ConvertTo-Json -Compress
$jsx = @"
var previousDialogs = app.displayDialogs;
var sourceFile = new File($sourceJs);
var outputFile = new File($targetJs);
var bridgeResult = '';
try {
    app.displayDialogs = DialogModes.NO;
    if (!sourceFile.exists || outputFile.exists) throw new Error('invalid source or existing target');
    var document = app.open(sourceFile);
    document.activeLayer.name = 'KORYAO Artwork';
    var options = new PhotoshopSaveOptions();
    options.layers = true;
    options.alphaChannels = true;
    document.saveAs(outputFile, options, false, Extension.LOWERCASE);
    document.close(SaveOptions.DONOTSAVECHANGES);
    var stableSamples = 0;
    var lastSize = -1;
    var saveDeadline = Date.now() + 10000;
    while (Date.now() < saveDeadline && stableSamples < 3) {
        if (!outputFile.exists || outputFile.length < 64) {
            stableSamples = 0;
            lastSize = -1;
        } else if (outputFile.length === lastSize) {
            stableSamples++;
        } else {
            lastSize = outputFile.length;
            stableSamples = 1;
        }
        if (stableSamples < 3) $.sleep(250);
    }
    if (stableSamples < 3) throw new Error('save completion timeout');
    var persisted = app.open(outputFile);
    var valid = persisted.width.as('px') > 0 && persisted.height.as('px') > 0 && persisted.layers.length > 0;
    var persistedLayers = persisted.layers.length;
    persisted.close(SaveOptions.DONOTSAVECHANGES);
    if (!valid) throw new Error('native reopen validation failed');
    bridgeResult = JSON.stringify({
        artboardCount: 0,
        pageItemCount: persistedLayers,
        stableSizeSamples: stableSamples
    });
} catch (error) {
    try { if (outputFile.exists) outputFile.remove(); } catch (cleanupError) {}
    throw error;
} finally {
    app.displayDialogs = previousDialogs;
}
bridgeResult;
"@
$application = New-Object -ComObject Photoshop.Application
$result = $application.DoJavaScript($jsx)
if (-not $result -or -not $result.StartsWith('{')) { throw 'photoshop export validation failed' }
Write-Output ('KORYAO_EXPORT_OK|' + $result)
"#;

#[cfg(windows)]
const ILLUSTRATOR_SCRIPT: &str = r#"
$ErrorActionPreference = 'Stop'
$source = $env:KORYAO_ADOBE_SOURCE
$target = $env:KORYAO_ADOBE_TARGET
if (-not $source -or -not $target) { throw 'missing export environment' }
$sourceJs = $source.Replace('\', '/') | ConvertTo-Json -Compress
$targetJs = $target.Replace('\', '/') | ConvertTo-Json -Compress
$jsx = @"
var previousInteraction = app.userInteractionLevel;
var sourceFile = new File($sourceJs);
var outputFile = new File($targetJs);
var bridgeResult = '';
try {
    app.userInteractionLevel = UserInteractionLevel.DONTDISPLAYALERTS;
    if (!sourceFile.exists || outputFile.exists) throw new Error('invalid source or existing target');
    var document = app.open(sourceFile);
    if (document.artboards.length < 1 || document.pageItems.length < 1) throw new Error('source vector is empty');
    var sourceArtboardCount = document.artboards.length;
    var sourcePageItemCount = document.pageItems.length;
    var sourceRect = document.artboards[0].artboardRect;
    var options = new IllustratorSaveOptions();
    options.pdfCompatible = true;
    options.compressed = true;
    document.saveAs(outputFile, options);
    document.close(SaveOptions.DONOTSAVECHANGES);
    var stableSamples = 0;
    var lastSize = -1;
    var saveDeadline = Date.now() + 15000;
    while (Date.now() < saveDeadline && stableSamples < 3) {
        if (!outputFile.exists || outputFile.length < 64) {
            stableSamples = 0;
            lastSize = -1;
        } else if (outputFile.length === lastSize) {
            stableSamples++;
        } else {
            lastSize = outputFile.length;
            stableSamples = 1;
        }
        if (stableSamples < 3) $.sleep(250);
    }
    if (stableSamples < 3) throw new Error('save completion timeout');
    var persisted = app.open(outputFile);
    var reopenedRect = persisted.artboards[0].artboardRect;
    var sameBounds = Math.abs((sourceRect[2] - sourceRect[0]) - (reopenedRect[2] - reopenedRect[0])) < 0.5
        && Math.abs((sourceRect[1] - sourceRect[3]) - (reopenedRect[1] - reopenedRect[3])) < 0.5;
    var valid = persisted.artboards.length === sourceArtboardCount
        && persisted.pageItems.length > 0
        && sourcePageItemCount > 0
        && sameBounds;
    var reopenedArtboardCount = persisted.artboards.length;
    var reopenedPageItemCount = persisted.pageItems.length;
    persisted.close(SaveOptions.DONOTSAVECHANGES);
    if (!valid) throw new Error('native reopen validation failed');
    bridgeResult = JSON.stringify({
        artboardCount: reopenedArtboardCount,
        pageItemCount: reopenedPageItemCount,
        stableSizeSamples: stableSamples
    });
} catch (error) {
    try { if (outputFile.exists) outputFile.remove(); } catch (cleanupError) {}
    throw error;
} finally {
    app.userInteractionLevel = previousInteraction;
}
bridgeResult;
"@
$application = New-Object -ComObject Illustrator.Application
$result = $application.DoJavaScript($jsx)
if (-not $result -or -not $result.StartsWith('{')) { throw 'illustrator export validation failed' }
Write-Output ('KORYAO_EXPORT_OK|' + $result)
"#;

#[cfg(windows)]
fn adobe_failure_is_transient(stderr: &str) -> bool {
    let normalized = stderr.to_ascii_lowercase();
    [
        "rpc_e_call_rejected",
        "call was rejected",
        "application is busy",
        "host busy",
        "sharing violation",
        "file is locked",
        "server unavailable",
        "server execution failed",
        "disconnected",
        "save completion timeout",
    ]
    .iter()
    .any(|token| normalized.contains(token))
}

#[cfg(windows)]
fn classified_adobe_failure(stderr: &str, format: &str) -> (String, bool) {
    let normalized = stderr.to_ascii_lowercase();
    if normalized.contains("source vector is empty")
        || normalized.contains("invalid source or existing target")
    {
        return (
            "adobe_source_invalid|Adobe 来源文件为空、无效或与暂存目标冲突。".into(),
            false,
        );
    }
    if normalized.contains("native reopen validation failed") {
        return (
            "adobe_native_reopen_failed|Adobe 原生重开或结构检查失败，未发布半成品。".into(),
            false,
        );
    }
    if normalized.contains("save completion timeout") {
        return (
            "adobe_save_timeout|Adobe 保存未在限定时间内稳定完成。".into(),
            true,
        );
    }
    if adobe_failure_is_transient(stderr) {
        return (
            "adobe_host_busy|Adobe 暂时繁忙、锁定或连接中断。".into(),
            true,
        );
    }
    if [
        "class not registered",
        "activex",
        "comobject",
        "server unavailable",
        "license",
        "sign in",
        "login",
        "unauthorized",
    ]
    .iter()
    .any(|token| normalized.contains(token))
    {
        return (
            "adobe_host_needs_user|Adobe 未授权、未登录或本机自动化接口不可用。".into(),
            false,
        );
    }
    (
        format!(
            "adobe_item_failed|{} 没有完成保存与原生重开；源产物未被修改。",
            if format == "psd" {
                "Photoshop"
            } else {
                "Illustrator"
            }
        ),
        false,
    )
}

fn customer_adobe_error(error: &str) -> String {
    error
        .split_once('|')
        .map(|(_, message)| message)
        .filter(|message| !message.trim().is_empty())
        .unwrap_or(error)
        .to_string()
}

#[cfg(windows)]
fn execute_adobe_export_once(
    source: &Path,
    target: &Path,
    format: &str,
) -> Result<NativeExportEvidence, (String, bool)> {
    use std::os::windows::process::CommandExt;

    let script = if format == "psd" {
        PHOTOSHOP_SCRIPT
    } else {
        ILLUSTRATOR_SCRIPT
    };
    let mut child = Command::new("powershell.exe")
        .args([
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ])
        .env("KORYAO_ADOBE_SOURCE", source)
        .env("KORYAO_ADOBE_TARGET", target)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(0x08000000)
        .spawn()
        .map_err(|_| {
            (
                "adobe_host_needs_user|无法启动本机 Adobe 导出桥接。".to_string(),
                false,
            )
        })?;

    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let output = child.wait_with_output().map_err(|_| {
                    (
                        "adobe_host_needs_user|无法读取 Adobe 导出结果。".to_string(),
                        false,
                    )
                })?;
                let stdout = String::from_utf8_lossy(&output.stdout);
                let stderr = String::from_utf8_lossy(&output.stderr);
                let evidence = stdout
                    .lines()
                    .find_map(|line| line.strip_prefix("KORYAO_EXPORT_OK|"))
                    .and_then(|value| serde_json::from_str::<NativeExportEvidence>(value).ok());
                if !status.success() || evidence.is_none() {
                    let _ = fs::remove_file(target);
                    return Err(classified_adobe_failure(&stderr, format));
                }
                return Ok(evidence.unwrap_or_default());
            }
            Ok(None) if started.elapsed() < EXPORT_TIMEOUT => {
                thread::sleep(Duration::from_millis(100));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = fs::remove_file(target);
                return Err((
                    "adobe_host_needs_user|Adobe 导出超过 120 秒，已停止等待并清理未完成文件。请在 Adobe 中完成登录、授权或关闭阻塞弹窗后重试。".into(),
                    false,
                ));
            }
            Err(_) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = fs::remove_file(target);
                return Err(("adobe_host_busy|无法确认 Adobe 导出进程状态。".into(), true));
            }
        }
    }
}

#[cfg(windows)]
fn execute_adobe_export(
    source: &Path,
    target: &Path,
    format: &str,
) -> Result<NativeExportEvidence, String> {
    let mut last_error = "Adobe 导出没有完成。".to_string();
    for attempt in 1_u8..=3 {
        match execute_adobe_export_once(source, target, format) {
            Ok(mut evidence) => {
                evidence.attempts = attempt;
                return Ok(evidence);
            }
            Err((error, retryable)) => {
                last_error = error;
                if !retryable || attempt == 3 {
                    break;
                }
                let _ = fs::remove_file(target);
                thread::sleep(Duration::from_millis(u64::from(attempt) * 500));
            }
        }
    }
    Err(last_error)
}

#[cfg(not(windows))]
fn execute_adobe_export(
    _source: &Path,
    _target: &Path,
    _format: &str,
) -> Result<NativeExportEvidence, String> {
    Err(NATIVE_ADOBE_UNAVAILABLE.into())
}

#[cfg(windows)]
fn require_native_adobe_export() -> Result<(), String> {
    Ok(())
}

#[cfg(not(windows))]
fn require_native_adobe_export() -> Result<(), String> {
    Err(NATIVE_ADOBE_UNAVAILABLE.into())
}

fn validate_native_file(path: &Path, format: &str) -> Result<u64, String> {
    let mut file = fs::File::open(path).map_err(|_| "Adobe 导出文件不存在。".to_string())?;
    let size = file
        .metadata()
        .map_err(|_| "无法读取 Adobe 导出文件信息。".to_string())?
        .len();
    let mut bytes = [0_u8; 16];
    let header_size = file
        .read(&mut bytes)
        .map_err(|_| "无法读取 Adobe 导出文件签名。".to_string())?;
    let header = &bytes[..header_size];
    let valid_signature = match format {
        "psd" => header.starts_with(b"8BPS"),
        "ai" => header.starts_with(b"%PDF-") || header.starts_with(b"%!PS-Adobe"),
        _ => false,
    };
    if size < 64 || !valid_signature {
        let _ = fs::remove_file(path);
        return Err("Adobe 导出文件没有通过原生格式签名验证，未完成文件已清理。".into());
    }
    Ok(size)
}

#[cfg(windows)]
fn wait_for_stable_file(path: &Path, timeout: Duration) -> Result<u32, String> {
    let started = Instant::now();
    let mut last_size = None;
    let mut stable_samples = 0_u32;
    while started.elapsed() < timeout {
        match fs::File::open(path).and_then(|file| file.metadata()) {
            Ok(metadata) if metadata.is_file() && metadata.len() >= 64 => {
                if last_size == Some(metadata.len()) {
                    stable_samples += 1;
                } else {
                    last_size = Some(metadata.len());
                    stable_samples = 1;
                }
                if stable_samples >= 3 {
                    return Ok(stable_samples);
                }
            }
            _ => {
                last_size = None;
                stable_samples = 0;
            }
        }
        thread::sleep(Duration::from_millis(250));
    }
    Err("Adobe 文件在限定时间内没有完成稳定写入。".into())
}

#[cfg(not(windows))]
fn wait_for_stable_file(_path: &Path, _timeout: std::time::Duration) -> Result<u32, String> {
    Err(NATIVE_ADOBE_UNAVAILABLE.into())
}

fn publish_without_overwrite(staged: &Path, target: &Path) -> Result<u64, String> {
    let mut source =
        fs::File::open(staged).map_err(|_| "无法读取已验证的 Adobe 暂存文件。".to_string())?;
    let mut destination = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(target)
        .map_err(|error| {
            if error.kind() == io::ErrorKind::AlreadyExists {
                "目标文件已经存在。KORYAO 不会覆盖它，请选择新文件名。".to_string()
            } else {
                "无法在所选路径创建交付文件，请检查文件夹写入权限。".to_string()
            }
        })?;
    let published = match io::copy(&mut source, &mut destination) {
        Ok(size) => size,
        Err(_) => {
            drop(destination);
            let _ = fs::remove_file(target);
            return Err("复制 Adobe 交付文件时发生错误，未完成目标已清理。".into());
        }
    };
    if destination.flush().is_err() || destination.sync_all().is_err() {
        drop(destination);
        let _ = fs::remove_file(target);
        return Err("无法完整写入 Adobe 交付文件，未完成目标已清理。".into());
    }
    Ok(published)
}

fn cleanup_owned_staging(staging_directory: &Path) {
    let _ = fs::remove_dir_all(staging_directory);
}

fn transactional_export(
    data_root: &Path,
    project_id: &str,
    source: &Path,
    target: &Path,
    format: &str,
    staging_key: Option<&str>,
) -> Result<AdobeExportReceipt, String> {
    let source_hash_before = hash_file(source)?;
    let task_id = staging_key
        .map(|value| format!("batch-{}", &hash_text(value)[..32]))
        .unwrap_or_else(|| uuid::Uuid::new_v4().simple().to_string());
    let staging_directory = data_root
        .join("staging")
        .join("adobe-exports")
        .join(&task_id);
    if staging_key.is_some() && staging_directory.exists() {
        cleanup_owned_staging(&staging_directory);
    }
    fs::create_dir_all(&staging_directory)
        .map_err(|_| "无法准备 Adobe 导出暂存目录。".to_string())?;
    let staged = staging_directory.join(format!("delivery.{format}"));

    let native_evidence = match execute_adobe_export(source, &staged, format) {
        Ok(evidence) => evidence,
        Err(error) => {
            cleanup_owned_staging(&staging_directory);
            return Err(error);
        }
    };
    let external_stable_samples =
        match wait_for_stable_file(&staged, std::time::Duration::from_secs(15)) {
            Ok(samples) => samples,
            Err(error) => {
                cleanup_owned_staging(&staging_directory);
                return Err(error);
            }
        };
    if native_evidence.stable_size_samples < 3
        || (format == "ai"
            && (native_evidence.artboard_count < 1 || native_evidence.page_item_count < 1))
    {
        cleanup_owned_staging(&staging_directory);
        return Err("Adobe 原生重开结构或保存完成证据不足，未发布暂存文件。".into());
    }
    let staged_size = match validate_native_file(&staged, format) {
        Ok(size) => size,
        Err(error) => {
            cleanup_owned_staging(&staging_directory);
            return Err(error);
        }
    };
    let sha256 = match hash_file(&staged) {
        Ok(hash) => hash,
        Err(error) => {
            cleanup_owned_staging(&staging_directory);
            return Err(error);
        }
    };
    let source_hash_after = match hash_file(source) {
        Ok(hash) => hash,
        Err(error) => {
            cleanup_owned_staging(&staging_directory);
            return Err(error);
        }
    };
    if source_hash_after != source_hash_before {
        cleanup_owned_staging(&staging_directory);
        return Err("Adobe 导出期间来源文件发生变化，已停止发布。".into());
    }
    let size_bytes = match publish_without_overwrite(&staged, target) {
        Ok(size) => size,
        Err(error) => {
            cleanup_owned_staging(&staging_directory);
            return Err(error);
        }
    };
    let target_size = match validate_native_file(target, format) {
        Ok(size) => size,
        Err(error) => {
            let _ = fs::remove_file(target);
            cleanup_owned_staging(&staging_directory);
            return Err(error);
        }
    };
    let target_hash = match hash_file(target) {
        Ok(hash) => hash,
        Err(error) => {
            let _ = fs::remove_file(target);
            cleanup_owned_staging(&staging_directory);
            return Err(error);
        }
    };
    if size_bytes != staged_size || target_size != staged_size || target_hash != sha256 {
        let _ = fs::remove_file(target);
        cleanup_owned_staging(&staging_directory);
        return Err("Adobe 交付发布后哈希或大小不一致，未完成目标已清理。".into());
    }
    cleanup_owned_staging(&staging_directory);

    let mut receipt = AdobeExportReceipt {
        receipt_id: uuid::Uuid::new_v4().simple().to_string(),
        format: format.to_string(),
        file_name: target
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("delivery")
            .to_string(),
        size_bytes,
        source_basename: source
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("artifact")
            .to_string(),
        sha256,
        created_at_unix_seconds: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs(),
        native_reopen_validated: true,
        save_completion_validated: true,
        artboard_count: native_evidence.artboard_count,
        page_item_count: native_evidence.page_item_count,
        stable_size_samples: native_evidence
            .stable_size_samples
            .max(external_stable_samples),
        recovery_attempts: native_evidence.attempts.saturating_sub(1),
        source_overwritten: false,
        target_path_persisted: false,
        history_recorded: true,
    };
    if persist_receipt(data_root, project_id, &receipt).is_err() {
        receipt.history_recorded = false;
    }
    Ok(receipt)
}

fn batch_identity(
    project_id: &str,
    format: &str,
    relative_paths: &[String],
    target_directory: &Path,
) -> Result<(String, String), String> {
    let target_directory_hash = hash_text(&target_directory.to_string_lossy());
    let encoded = serde_json::to_string(&serde_json::json!({
        "projectId": project_id,
        "format": format,
        "relativePaths": relative_paths,
        "targetDirectoryHash": target_directory_hash,
    }))
    .map_err(|_| "无法建立 Adobe 批量幂等标识。".to_string())?;
    Ok((
        format!("batch-{}", &hash_text(&encoded)[..24]),
        target_directory_hash,
    ))
}

fn batch_target_file_names(
    sources: &[PathBuf],
    relative_paths: &[String],
    format: &str,
) -> Vec<String> {
    let mut stem_counts = std::collections::HashMap::<String, usize>::new();
    for source in sources {
        let stem = source
            .file_stem()
            .and_then(|value| value.to_str())
            .unwrap_or("KORYAO-delivery")
            .to_ascii_lowercase();
        *stem_counts.entry(stem).or_default() += 1;
    }
    sources
        .iter()
        .zip(relative_paths)
        .map(|(source, relative_path)| {
            let stem = source
                .file_stem()
                .and_then(|value| value.to_str())
                .unwrap_or("KORYAO-delivery");
            let duplicate = stem_counts
                .get(&stem.to_ascii_lowercase())
                .copied()
                .unwrap_or_default()
                > 1;
            if duplicate {
                format!("{stem}-{}.{}", &hash_text(relative_path)[..8], format)
            } else {
                format!("{stem}.{format}")
            }
        })
        .collect()
}

fn batch_state_matches_request(
    state: &AdobeBatchState,
    project_id: &str,
    format: &str,
    batch_id: &str,
    target_directory_hash: &str,
    relative_paths: &[String],
    target_file_names: &[String],
) -> bool {
    if state.schema_version != 1
        || state.project_id != project_id
        || state.format != format
        || state.batch_id != batch_id
        || state.target_directory_hash != target_directory_hash
        || state.target_path_persisted
        || state.items.len() != relative_paths.len()
        || state.items.len() != target_file_names.len()
    {
        return false;
    }
    state
        .items
        .iter()
        .zip(relative_paths.iter().zip(target_file_names))
        .all(|(item, (relative_path, target_file_name))| {
            let receipt_safe = item.receipt.as_ref().is_none_or(|receipt| {
                receipt.format == format
                    && receipt.file_name == *target_file_name
                    && receipt.native_reopen_validated
                    && receipt.save_completion_validated
                    && !receipt.source_overwritten
                    && !receipt.target_path_persisted
            });
            item.item_id == format!("item-{}", &hash_text(relative_path)[..24])
                && item.source_relative_path == *relative_path
                && item.target_file_name == *target_file_name
                && !item.target_file_name.contains(['/', '\\', ':'])
                && matches!(
                    item.status.as_str(),
                    "queued" | "running" | "completed" | "failed" | "cancelled"
                )
                && (item.status != "completed" || item.receipt.is_some())
                && receipt_safe
        })
}

#[tauri::command]
pub async fn export_adobe_file(
    app: AppHandle,
    project_id: String,
    artifact_relative_path: String,
    format: String,
    confirm_export: bool,
) -> Result<Option<AdobeExportReceipt>, String> {
    // This must remain the first operation: unsupported platforms fail before
    // app-data resolution, source parsing, a save picker, staging, or writes.
    require_native_adobe_export()?;
    if !confirm_export {
        return Err("导出到用户选择的路径前需要明确确认。".into());
    }
    if !matches!(format.as_str(), "psd" | "ai") {
        return Err("只支持导出 PSD 或 AI 文件。".into());
    }
    let data_root = starbridge_data_root(&app)?;
    let source = resolve_source(&data_root, &project_id, &artifact_relative_path, &format)?;
    let suggested_name = format!(
        "{}.{format}",
        source
            .file_stem()
            .and_then(|value| value.to_str())
            .unwrap_or("KORYAO-delivery")
    );
    let picker_format = format.clone();
    let target = tauri::async_runtime::spawn_blocking(move || {
        let label = if picker_format == "psd" {
            "Photoshop 文档"
        } else {
            "Illustrator 文件"
        };
        rfd::FileDialog::new()
            .add_filter(label, &[picker_format.as_str()])
            .set_file_name(&suggested_name)
            .set_title("选择 KORYAO 交付文件的保存路径")
            .save_file()
    })
    .await
    .map_err(|_| "无法打开保存路径选择窗口。".to_string())?;
    let Some(target) = target else {
        return Ok(None);
    };
    let target = normalized_target(target, &format)?;
    let source_for_export = source.clone();
    let target_for_export = target.clone();
    let data_root_for_export = data_root.clone();
    let project_for_export = project_id.clone();
    let format_for_export = format.clone();
    let receipt = tauri::async_runtime::spawn_blocking(move || {
        transactional_export(
            &data_root_for_export,
            &project_for_export,
            &source_for_export,
            &target_for_export,
            &format_for_export,
            None,
        )
    })
    .await
    .map_err(|_| "Adobe 导出任务意外停止。".to_string())?
    .map_err(|error| customer_adobe_error(&error))?;
    Ok(Some(receipt))
}

#[tauri::command]
pub async fn export_adobe_batch(
    app: AppHandle,
    project_id: String,
    artifact_relative_paths: Vec<String>,
    format: String,
    confirm_export: bool,
) -> Result<Option<AdobeBatchExportResult>, String> {
    require_native_adobe_export()?;
    if !confirm_export {
        return Err("批量导出到用户选择的目录前需要明确确认。".into());
    }
    if !matches!(format.as_str(), "psd" | "ai") {
        return Err("只支持批量导出 PSD 或 AI 文件。".into());
    }
    if !(2..=32).contains(&artifact_relative_paths.len()) {
        return Err("Adobe 批量需要选择 2 到 32 个真实产物。".into());
    }
    let unique_paths = artifact_relative_paths
        .iter()
        .collect::<std::collections::HashSet<_>>();
    if unique_paths.len() != artifact_relative_paths.len() {
        return Err("Adobe 批量不能重复选择同一产物。".into());
    }
    if artifact_relative_paths
        .iter()
        .any(|relative_path| !valid_relative_artifact(relative_path))
    {
        return Err("Adobe 批量包含无效的项目产物标识。".into());
    }
    let data_root = starbridge_data_root(&app)?;
    let sources = artifact_relative_paths
        .iter()
        .map(|relative_path| resolve_source(&data_root, &project_id, relative_path, &format))
        .collect::<Vec<_>>();
    let output_directory = tauri::async_runtime::spawn_blocking(|| {
        rfd::FileDialog::new()
            .set_title("选择 KORYAO Adobe 批量交付目录")
            .pick_folder()
    })
    .await
    .map_err(|_| "无法打开批量交付目录选择窗口。".to_string())?;
    let Some(output_directory) = output_directory else {
        return Ok(None);
    };
    if !output_directory.is_absolute() || !output_directory.is_dir() {
        return Err("请选择一个存在且可写的完整文件夹路径。".into());
    }
    let (batch_id, target_directory_hash) = batch_identity(
        &project_id,
        &format,
        &artifact_relative_paths,
        &output_directory,
    )?;
    let source_names = artifact_relative_paths
        .iter()
        .map(PathBuf::from)
        .collect::<Vec<_>>();
    let target_file_names =
        batch_target_file_names(&source_names, &artifact_relative_paths, &format);
    let existing = read_batch_state(&data_root, &project_id, &batch_id);
    let resumed = existing.is_some();
    let mut state = existing.unwrap_or_else(|| AdobeBatchState {
        schema_version: 1,
        batch_id: batch_id.clone(),
        project_id: project_id.clone(),
        format: format.clone(),
        target_directory_hash: target_directory_hash.clone(),
        target_path_persisted: false,
        items: artifact_relative_paths
            .iter()
            .zip(&target_file_names)
            .map(|(relative_path, target_file_name)| AdobeBatchItemState {
                item_id: format!("item-{}", &hash_text(relative_path)[..24]),
                source_relative_path: relative_path.clone(),
                target_file_name: target_file_name.clone(),
                status: "queued".into(),
                attempts: 0,
                error_code: None,
                receipt: None,
            })
            .collect(),
    });
    if !batch_state_matches_request(
        &state,
        &project_id,
        &format,
        &batch_id,
        &target_directory_hash,
        &artifact_relative_paths,
        &target_file_names,
    ) || state.items.len() != sources.len()
    {
        return Err("Adobe 批量恢复状态与本次选择不一致，请重新建立批量。".into());
    }
    persist_batch_state(&data_root, &state)?;

    let mut needs_user = false;
    for (index, source_result) in sources.iter().enumerate().take(state.items.len()) {
        let source = match source_result {
            Ok(source) => source,
            Err(_) => {
                state.items[index].status = "failed".into();
                state.items[index].error_code = Some("source_invalid".into());
                state.items[index].receipt = None;
                persist_batch_state(&data_root, &state)?;
                continue;
            }
        };
        if state.items[index].status == "cancelled"
            || (state.items[index].status == "failed"
                && !matches!(
                    state.items[index].error_code.as_deref(),
                    Some(
                        "adobe_host_needs_user"
                            | "adobe_host_busy"
                            | "adobe_save_timeout"
                            | "transaction_failed"
                    )
                ))
        {
            continue;
        }
        let target = output_directory.join(&state.items[index].target_file_name);
        if state.items[index].status == "completed" {
            let verified = state.items[index].receipt.as_ref().is_some_and(|receipt| {
                target.is_file()
                    && hash_file(&target)
                        .map(|value| value == receipt.sha256)
                        .unwrap_or(false)
            });
            if verified {
                continue;
            }
            if target.exists() {
                state.items[index].status = "failed".into();
                state.items[index].error_code = Some("target_conflict".into());
                persist_batch_state(&data_root, &state)?;
                continue;
            }
            state.items[index].status = "queued".into();
            state.items[index].receipt = None;
        }
        if target.exists() {
            state.items[index].status = "failed".into();
            state.items[index].error_code = Some("target_conflict".into());
            persist_batch_state(&data_root, &state)?;
            continue;
        }
        state.items[index].status = "running".into();
        state.items[index].attempts = state.items[index].attempts.saturating_add(1);
        state.items[index].error_code = None;
        persist_batch_state(&data_root, &state)?;

        let staging_key = format!(
            "{}:{}:{}",
            state.batch_id, state.items[index].item_id, state.items[index].source_relative_path
        );
        match transactional_export(
            &data_root,
            &project_id,
            source,
            &target,
            &format,
            Some(&staging_key),
        ) {
            Ok(receipt) => {
                state.items[index].status = "completed".into();
                state.items[index].receipt = Some(receipt);
                persist_batch_state(&data_root, &state)?;
            }
            Err(error) => {
                let error_code = error.split('|').next().unwrap_or("transaction_failed");
                let host_needs_user =
                    matches!(error_code, "adobe_host_needs_user" | "adobe_host_busy");
                state.items[index].status = "failed".into();
                state.items[index].error_code = Some(
                    if error_code.starts_with("adobe_") {
                        error_code
                    } else {
                        "transaction_failed"
                    }
                    .into(),
                );
                persist_batch_state(&data_root, &state)?;
                if host_needs_user {
                    needs_user = true;
                    break;
                }
            }
        }
    }
    let receipts = state
        .items
        .iter()
        .filter_map(|item| item.receipt.clone())
        .collect::<Vec<_>>();
    let completed_count = state
        .items
        .iter()
        .filter(|item| item.status == "completed")
        .count();
    let failed_count = state
        .items
        .iter()
        .filter(|item| item.status == "failed")
        .count();
    Ok(Some(AdobeBatchExportResult {
        batch_id,
        item_count: state.items.len(),
        completed_count,
        failed_count,
        needs_user,
        resumed,
        receipts,
    }))
}

#[tauri::command]
pub fn list_adobe_exports(
    app: AppHandle,
    project_id: String,
) -> Result<Vec<AdobeExportReceipt>, String> {
    require_native_adobe_export()?;
    if !valid_vector_id(&project_id, "project-") {
        return Err("项目标识无效。".into());
    }
    let data_root = starbridge_data_root(&app)?;
    Ok(read_receipts(&data_root, &project_id))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn relative_artifacts_reject_escape_and_absolute_paths() {
        for path in [
            "artifacts/project-test/job-test/vector.svg",
            "artifacts/project-123/preview/image.png",
        ] {
            assert!(valid_relative_artifact(path), "{path}");
        }
        for path in [
            "",
            "/private/vector.svg",
            "//server/share/vector.svg",
            "C:/private/vector.svg",
            "C:\\private\\vector.svg",
            "\\\\server\\share\\vector.svg",
            "artifacts\\project-test\\vector.svg",
            "artifacts//project-test/vector.svg",
            "artifacts/./project-test/vector.svg",
            "artifacts/project-test/../private.svg",
            "artifacts/project:test/vector.svg",
            "artifacts/project-test/vector.svg\n",
        ] {
            assert!(!valid_relative_artifact(path), "{path:?}");
        }
    }

    #[test]
    fn svg_preflight_accepts_internal_references_and_rejects_active_content() {
        let base = std::env::temp_dir().join(format!("koryao-svg-safe-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&base).expect("svg test directory");
        let safe = base.join("safe.svg");
        fs::write(
            &safe,
            br##"<svg xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="g"><stop offset="0"/></linearGradient></defs><path fill="url(#g)" d="M0 0L10 10"/></svg>"##,
        )
        .expect("safe svg");
        assert!(validate_svg_source(&safe).is_ok());

        for (name, body) in [
            (
                "script.svg",
                r#"<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script><path d="M0 0"/></svg>"#,
            ),
            (
                "bitmap.svg",
                r#"<svg xmlns="http://www.w3.org/2000/svg"><image href="data:image/png;base64,AA"/><path d="M0 0"/></svg>"#,
            ),
            (
                "external.svg",
                r#"<svg xmlns="http://www.w3.org/2000/svg"><use href="https://example.invalid/a.svg#x"/><path d="M0 0"/></svg>"#,
            ),
        ] {
            let path = base.join(name);
            fs::write(&path, body).expect("unsafe svg");
            assert!(validate_svg_source(&path).is_err(), "{name}");
        }
        fs::remove_dir_all(base).expect("remove svg test directory");
    }

    #[test]
    fn target_extension_and_overwrite_policy_are_strict() {
        let base = std::env::temp_dir().join(format!("koryao-adobe-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&base).expect("temp export directory");
        let without_extension = normalized_target(base.join("delivery"), "ai").expect("target");
        assert_eq!(
            without_extension
                .extension()
                .and_then(|value| value.to_str()),
            Some("ai")
        );
        assert!(normalized_target(base.join("delivery.psd"), "ai").is_err());
        fs::write(&without_extension, b"existing").expect("existing target");
        assert!(normalized_target(without_extension.clone(), "ai").is_err());
        fs::remove_file(without_extension).expect("remove target");
        fs::remove_dir(base).expect("remove temp directory");
    }

    #[test]
    fn native_signature_validation_rejects_disguised_files() {
        let base = std::env::temp_dir().join(format!("koryao-signature-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&base).expect("temp signature directory");
        let valid_psd = base.join("valid.psd");
        fs::write(&valid_psd, [b"8BPS".as_slice(), &[0_u8; 80]].concat()).expect("psd");
        assert!(validate_native_file(&valid_psd, "psd").is_ok());
        let disguised_ai = base.join("disguised.ai");
        fs::write(&disguised_ai, [b"<svg".as_slice(), &[0_u8; 80]].concat()).expect("ai");
        assert!(validate_native_file(&disguised_ai, "ai").is_err());
        assert!(!disguised_ai.exists());
        fs::remove_file(valid_psd).expect("remove psd");
        fs::remove_dir(base).expect("remove temp directory");
    }

    #[test]
    fn verified_staging_publish_never_overwrites_a_customer_file() {
        let base = std::env::temp_dir().join(format!("koryao-publish-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&base).expect("temp publish directory");
        let staged = base.join("staged.ai");
        fs::write(&staged, [b"%PDF-1.7\n".as_slice(), &[0_u8; 80]].concat()).expect("stage");
        let target = base.join("customer.ai");
        fs::write(&target, b"customer-owned").expect("customer target");
        assert!(publish_without_overwrite(&staged, &target).is_err());
        assert_eq!(
            fs::read(&target).expect("customer target"),
            b"customer-owned"
        );
        fs::remove_file(&target).expect("remove customer target");
        assert!(publish_without_overwrite(&staged, &target).is_ok());
        assert_eq!(
            fs::read(&target).expect("published target"),
            fs::read(&staged).expect("stage")
        );
        fs::remove_file(staged).expect("remove stage");
        fs::remove_file(target).expect("remove target");
        fs::remove_dir(base).expect("remove temp directory");
    }

    #[test]
    fn batch_state_is_resumable_without_persisting_the_customer_directory() {
        let base = std::env::temp_dir().join(format!("koryao-batch-{}", uuid::Uuid::new_v4()));
        let project_id = "project-batch-test";
        let relative_paths = vec![
            "artifacts/project-batch-test/job-one/vector.svg".to_string(),
            "artifacts/project-batch-test/job-two/vector.svg".to_string(),
        ];
        let customer_directory = base.join("private-customer-output");
        fs::create_dir_all(&customer_directory).expect("customer directory");
        let (batch_id, directory_hash) =
            batch_identity(project_id, "ai", &relative_paths, &customer_directory)
                .expect("batch identity");
        let mut state = AdobeBatchState {
            schema_version: 1,
            batch_id: batch_id.clone(),
            project_id: project_id.into(),
            format: "ai".into(),
            target_directory_hash: directory_hash,
            target_path_persisted: false,
            items: relative_paths
                .iter()
                .enumerate()
                .map(|(index, relative_path)| AdobeBatchItemState {
                    item_id: format!("item-{}", &hash_text(relative_path)[..24]),
                    source_relative_path: relative_path.clone(),
                    target_file_name: format!("vector-{index}.ai"),
                    status: "queued".into(),
                    attempts: 0,
                    error_code: None,
                    receipt: None,
                })
                .collect(),
        };
        persist_batch_state(&base, &state).expect("first checkpoint");
        state.items[0].status = "completed".into();
        state.items[0].attempts = 1;
        persist_batch_state(&base, &state).expect("second checkpoint");

        let restored = read_batch_state(&base, project_id, &batch_id).expect("restored state");
        assert_eq!(restored.items[0].status, "completed");
        assert_eq!(restored.items[0].attempts, 1);
        let target_names = vec!["vector-0.ai".to_string(), "vector-1.ai".to_string()];
        let mut request_state = restored.clone();
        request_state.items[0].status = "queued".into();
        assert!(batch_state_matches_request(
            &request_state,
            project_id,
            "ai",
            &batch_id,
            &request_state.target_directory_hash,
            &relative_paths,
            &target_names,
        ));
        let mut tampered = request_state;
        tampered.items[0].target_file_name = "../outside.ai".into();
        assert!(!batch_state_matches_request(
            &tampered,
            project_id,
            "ai",
            &batch_id,
            &tampered.target_directory_hash,
            &relative_paths,
            &target_names,
        ));
        let encoded = serde_json::to_string(&restored).expect("state json");
        assert!(!encoded.contains(&customer_directory.to_string_lossy().to_string()));
        assert!(!restored.target_path_persisted);
        fs::remove_dir_all(base).expect("remove batch directory");
    }

    #[test]
    fn duplicate_batch_basenames_receive_deterministic_unique_targets() {
        let sources = vec![
            PathBuf::from("first/vector.svg"),
            PathBuf::from("second/vector.svg"),
        ];
        let relative_paths = vec![
            "artifacts/project-test/first/vector.svg".to_string(),
            "artifacts/project-test/second/vector.svg".to_string(),
        ];
        let names = batch_target_file_names(&sources, &relative_paths, "ai");
        assert_eq!(names.len(), 2);
        assert_ne!(names[0], names[1]);
        assert!(names.iter().all(|name| name.starts_with("vector-")));
        assert!(names.iter().all(|name| name.ends_with(".ai")));
    }

    #[cfg(windows)]
    #[test]
    fn save_completion_wait_requires_repeated_stable_size_samples() {
        let base = std::env::temp_dir().join(format!("koryao-stable-{}", uuid::Uuid::new_v4()));
        fs::create_dir_all(&base).expect("stable directory");
        let file = base.join("delivery.ai");
        fs::write(&file, [b"%PDF-1.7\n".as_slice(), &[0_u8; 80]].concat()).expect("stable file");
        assert!(wait_for_stable_file(&file, Duration::from_secs(2)).expect("stable samples") >= 3);
        fs::remove_dir_all(base).expect("remove stable directory");
    }

    #[cfg(windows)]
    #[test]
    fn retry_classifier_excludes_login_and_license_failures() {
        assert!(adobe_failure_is_transient(
            "RPC_E_CALL_REJECTED: application is busy"
        ));
        assert!(adobe_failure_is_transient("save completion timeout"));
        assert!(!adobe_failure_is_transient("Adobe login required"));
        assert!(!adobe_failure_is_transient("license verification failed"));
        assert!(classified_adobe_failure("source vector is empty", "ai")
            .0
            .starts_with("adobe_source_invalid|"));
        assert!(
            classified_adobe_failure("license verification failed", "ai")
                .0
                .starts_with("adobe_host_needs_user|")
        );
        assert_eq!(
            customer_adobe_error("adobe_host_needs_user|请先恢复 Adobe 授权。"),
            "请先恢复 Adobe 授权。"
        );
        assert_eq!(
            customer_adobe_error("Adobe 导出任务意外停止。"),
            "Adobe 导出任务意外停止。"
        );
    }

    #[test]
    fn owned_staging_cleanup_does_not_touch_siblings() {
        let base =
            std::env::temp_dir().join(format!("koryao-owned-staging-{}", uuid::Uuid::new_v4()));
        let owned = base.join("owned");
        let sibling = base.join("sibling");
        fs::create_dir_all(&owned).expect("owned staging");
        fs::create_dir_all(&sibling).expect("sibling staging");
        fs::write(owned.join("delivery.ai"), b"partial").expect("owned partial");
        fs::write(sibling.join("keep.ai"), b"customer-owned").expect("sibling file");

        cleanup_owned_staging(&owned);

        assert!(!owned.exists());
        assert!(sibling.join("keep.ai").exists());
        fs::remove_dir_all(base).expect("remove cleanup test directory");
    }

    #[test]
    fn receipts_persist_without_a_customer_destination_path() {
        let base = std::env::temp_dir().join(format!("koryao-receipt-{}", uuid::Uuid::new_v4()));
        let project_id = "project-receipt-test";
        let receipt = AdobeExportReceipt {
            receipt_id: "newer".into(),
            format: "ai".into(),
            file_name: "customer.ai".into(),
            size_bytes: 4096,
            source_basename: "vector.svg".into(),
            sha256: "a".repeat(64),
            created_at_unix_seconds: 20,
            native_reopen_validated: true,
            save_completion_validated: true,
            artboard_count: 1,
            page_item_count: 4,
            stable_size_samples: 3,
            recovery_attempts: 0,
            source_overwritten: false,
            target_path_persisted: false,
            history_recorded: true,
        };
        persist_receipt(&base, project_id, &receipt).expect("persist receipt");
        let older = AdobeExportReceipt {
            receipt_id: "older".into(),
            created_at_unix_seconds: 10,
            ..receipt.clone()
        };
        persist_receipt(&base, project_id, &older).expect("persist older receipt");

        let receipt_path = receipt_directory(&base, project_id).join("receipt-newer.json");
        let encoded = fs::read_to_string(&receipt_path).expect("receipt json");
        assert!(!encoded.contains("C:\\\\Customers\\\\Secret"));
        assert!(!encoded.contains("\"targetPath\":"));
        assert!(!encoded.contains("destinationPath"));
        assert!(encoded.contains("\"targetPathPersisted\": false"));

        let receipts = read_receipts(&base, project_id);
        assert_eq!(receipts.len(), 2);
        assert_eq!(receipts[0].receipt_id, "newer");
        assert_eq!(receipts[1].receipt_id, "older");
        fs::remove_dir_all(base).expect("remove receipt directory");
    }

    #[cfg(not(windows))]
    #[test]
    fn unsupported_platform_preflight_has_zero_filesystem_side_effects() {
        let base =
            std::env::temp_dir().join(format!("koryao-adobe-fail-closed-{}", uuid::Uuid::new_v4()));
        let source = base.join("artifacts/project-test/vector.svg");
        let staging = base.join("staging/adobe-exports");
        let destination = base.join("customer.ai");

        let error = require_native_adobe_export().expect_err("non-Windows must fail closed");

        assert_eq!(error, NATIVE_ADOBE_UNAVAILABLE);
        assert!(!base.exists());
        assert!(!source.exists());
        assert!(!staging.exists());
        assert!(!destination.exists());
    }
}
