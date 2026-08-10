use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;

use keyring::Entry;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Manager, RunEvent, State, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_dialog::DialogExt;
use url::Url;

const KEYRING_SERVICE: &str = "com.autoflow.desktop";
const ROLES: [&str; 7] = [
    "product",
    "reader",
    "planner",
    "architecture",
    "coder",
    "reviewer",
    "acceptance",
];

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct AgentRoute {
    provider: String,
    model: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, rename_all = "camelCase")]
struct DesktopConfig {
    server_url: String,
    runner_id: String,
    runner_name: String,
    repository_roots: Vec<String>,
    runner_command: String,
    worktree_root: String,
    provider: String,
    model: String,
    mock_llm: bool,
    agent_routes: BTreeMap<String, AgentRoute>,
}

impl Default for DesktopConfig {
    fn default() -> Self {
        Self {
            server_url: "http://127.0.0.1:8765".into(),
            runner_id: "desktop-runner".into(),
            runner_name: "My computer".into(),
            repository_roots: Vec::new(),
            runner_command: String::new(),
            worktree_root: String::new(),
            provider: "deepseek".into(),
            model: "deepseek-chat".into(),
            mock_llm: false,
            agent_routes: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct SecretInput {
    runner_token: Option<String>,
    openai_api_key: Option<String>,
    deepseek_api_key: Option<String>,
    doubao_api_key: Option<String>,
    qwen_api_key: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopSnapshot {
    config: DesktopConfig,
    secrets_configured: BTreeMap<String, bool>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RunnerStatus {
    running: bool,
    pid: Option<u32>,
    log_path: String,
    last_exit: Option<String>,
}

#[derive(Default)]
struct RunnerState {
    process: Mutex<Option<Child>>,
    last_exit: Mutex<Option<String>>,
}

fn config_path(app: &AppHandle) -> Result<PathBuf, String> {
    let directory = app
        .path()
        .app_config_dir()
        .map_err(|error| error.to_string())?;
    fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    Ok(directory.join("desktop.json"))
}

fn data_directory(app: &AppHandle) -> Result<PathBuf, String> {
    let directory = app
        .path()
        .app_data_dir()
        .map_err(|error| error.to_string())?;
    fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    Ok(directory)
}

fn read_config(app: &AppHandle) -> Result<DesktopConfig, String> {
    let path = config_path(app)?;
    if !path.is_file() {
        return Ok(DesktopConfig::default());
    }
    let content = fs::read_to_string(path).map_err(|error| error.to_string())?;
    serde_json::from_str(&content).map_err(|error| format!("Invalid desktop config: {error}"))
}

fn validate_config(config: &mut DesktopConfig) -> Result<(), String> {
    let parsed = Url::parse(config.server_url.trim()).map_err(|_| "Server URL is invalid")?;
    if !matches!(parsed.scheme(), "http" | "https") || parsed.host_str().is_none() {
        return Err("Server URL must use HTTP or HTTPS".into());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("Server URL must not contain credentials".into());
    }
    config.server_url = config.server_url.trim().trim_end_matches('/').into();
    if config.runner_id.is_empty()
        || !config
            .runner_id
            .chars()
            .all(|value| value.is_ascii_alphanumeric() || ".-_".contains(value))
    {
        return Err("Runner ID may contain only letters, numbers, dot, dash and underscore".into());
    }
    if config.runner_name.trim().is_empty() {
        return Err("Runner name is required".into());
    }
    if config.repository_roots.is_empty() {
        return Err("Select at least one repository root".into());
    }
    config.repository_roots = config
        .repository_roots
        .iter()
        .map(PathBuf::from)
        .map(|path| {
            path.canonicalize()
                .map_err(|_| format!("Repository root does not exist: {}", path.display()))
        })
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .map(|path| path.to_string_lossy().into_owned())
        .collect();
    let providers = ["openai", "deepseek", "doubao", "qwen", "codex_cli"];
    if !providers.contains(&config.provider.as_str()) {
        return Err("Unsupported default LLM provider".into());
    }
    for (role, route) in &config.agent_routes {
        if !ROLES.contains(&role.as_str()) {
            return Err(format!("Unsupported agent role: {role}"));
        }
        if !providers.contains(&route.provider.as_str()) || route.model.trim().is_empty() {
            return Err(format!("Invalid route for {role}"));
        }
    }
    Ok(())
}

fn keyring_entry(runner_id: &str, name: &str) -> Result<Entry, String> {
    Entry::new(KEYRING_SERVICE, &format!("{runner_id}:{name}"))
        .map_err(|error| format!("Unable to access system keyring: {error}"))
}

fn get_secret(runner_id: &str, name: &str) -> Result<Option<String>, String> {
    let entry = keyring_entry(runner_id, name)?;
    match entry.get_password() {
        Ok(value) => Ok(Some(value)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(error) => Err(format!(
            "Unable to read {name} from system keyring: {error}"
        )),
    }
}

fn save_secret(runner_id: &str, name: &str, value: Option<&str>) -> Result<(), String> {
    let Some(value) = value.map(str::trim).filter(|value| !value.is_empty()) else {
        return Ok(());
    };
    keyring_entry(runner_id, name)?
        .set_password(value)
        .map_err(|error| format!("Unable to save {name} in system keyring: {error}"))
}

#[tauri::command]
fn load_desktop_config(app: AppHandle) -> Result<DesktopSnapshot, String> {
    let config = read_config(&app)?;
    let configured = [
        "runner_token",
        "openai_api_key",
        "deepseek_api_key",
        "doubao_api_key",
        "qwen_api_key",
    ]
    .into_iter()
    .map(|name| {
        let present = get_secret(&config.runner_id, name)
            .map(|value| value.is_some())
            .unwrap_or(false);
        (name.to_string(), present)
    })
    .collect();
    Ok(DesktopSnapshot {
        config,
        secrets_configured: configured,
    })
}

#[tauri::command]
fn save_desktop_config(
    app: AppHandle,
    mut config: DesktopConfig,
    secrets: SecretInput,
) -> Result<DesktopSnapshot, String> {
    validate_config(&mut config)?;
    save_secret(
        &config.runner_id,
        "runner_token",
        secrets.runner_token.as_deref(),
    )?;
    save_secret(
        &config.runner_id,
        "openai_api_key",
        secrets.openai_api_key.as_deref(),
    )?;
    save_secret(
        &config.runner_id,
        "deepseek_api_key",
        secrets.deepseek_api_key.as_deref(),
    )?;
    save_secret(
        &config.runner_id,
        "doubao_api_key",
        secrets.doubao_api_key.as_deref(),
    )?;
    save_secret(
        &config.runner_id,
        "qwen_api_key",
        secrets.qwen_api_key.as_deref(),
    )?;
    let payload = serde_json::to_vec_pretty(&config).map_err(|error| error.to_string())?;
    let path = config_path(&app)?;
    let temporary = path.with_extension("tmp");
    fs::write(&temporary, payload).map_err(|error| error.to_string())?;
    fs::rename(temporary, path).map_err(|error| error.to_string())?;
    load_desktop_config(app)
}

#[tauri::command]
async fn probe_server(app: AppHandle) -> Result<serde_json::Value, String> {
    let config = read_config(&app)?;
    let response = reqwest::Client::new()
        .get(format!("{}/api/ready", config.server_url))
        .timeout(std::time::Duration::from_secs(8))
        .send()
        .await
        .map_err(|error| format!("Cannot connect to AutoFlow Server: {error}"))?;
    let status = response.status();
    let payload = response
        .json::<serde_json::Value>()
        .await
        .map_err(|error| format!("Server returned invalid JSON: {error}"))?;
    if !status.is_success() {
        return Err(format!("Server readiness failed ({status}): {payload}"));
    }
    Ok(payload)
}

#[tauri::command]
async fn pick_repository_root(app: AppHandle) -> Result<Option<String>, String> {
    let selected = app
        .dialog()
        .file()
        .set_title("选择允许 AutoFlow 访问的代码目录")
        .blocking_pick_folder();
    selected
        .map(|path| {
            path.into_path()
                .map(|value| value.to_string_lossy().into_owned())
                .map_err(|error| error.to_string())
        })
        .transpose()
}

fn runner_log_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(data_directory(app)?.join("runner.log"))
}

fn resolve_runner_command(app: &AppHandle, configured: &str) -> Result<PathBuf, String> {
    if !configured.trim().is_empty() {
        return Ok(PathBuf::from(configured.trim()));
    }
    let executable = if cfg!(windows) {
        "autoflow-runner.exe"
    } else {
        "autoflow-runner"
    };
    let bundled = app
        .path()
        .resource_dir()
        .map_err(|error| error.to_string())?
        .join("resources/bin")
        .join(executable);
    if bundled.is_file() {
        return Ok(bundled);
    }
    Ok(PathBuf::from(executable))
}

fn apply_secret_environment(command: &mut Command, config: &DesktopConfig) -> Result<(), String> {
    let mappings = [
        ("OPENAI_API_KEY", "openai_api_key"),
        ("DEEPSEEK_API_KEY", "deepseek_api_key"),
        ("ARK_API_KEY", "doubao_api_key"),
        ("QWEN_API_KEY", "qwen_api_key"),
    ];
    for (environment, name) in mappings {
        if let Some(secret) = get_secret(&config.runner_id, name)? {
            command.env(environment, secret);
        }
    }
    Ok(())
}

#[tauri::command]
fn start_runner(app: AppHandle, state: State<'_, RunnerState>) -> Result<RunnerStatus, String> {
    let mut process = state
        .process
        .lock()
        .map_err(|_| "Runner state lock failed")?;
    if let Some(child) = process.as_mut() {
        if child
            .try_wait()
            .map_err(|error| error.to_string())?
            .is_none()
        {
            drop(process);
            return runner_status(app, state);
        }
    }
    *process = None;
    let mut config = read_config(&app)?;
    validate_config(&mut config)?;
    let token = get_secret(&config.runner_id, "runner_token")?
        .ok_or("Runner Token is not configured in the system keyring")?;
    let data = data_directory(&app)?;
    let worktree = if config.worktree_root.trim().is_empty() {
        data.join("worktrees")
    } else {
        PathBuf::from(&config.worktree_root)
    };
    fs::create_dir_all(&worktree).map_err(|error| error.to_string())?;
    let log_path = runner_log_path(&app)?;
    let stdout = OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|error| error.to_string())?;
    let stderr = stdout.try_clone().map_err(|error| error.to_string())?;
    let runner_command = resolve_runner_command(&app, &config.runner_command)?;
    let mut command = Command::new(&runner_command);
    command
        .arg("--server")
        .arg(&config.server_url)
        .arg("--id")
        .arg(&config.runner_id)
        .arg("--name")
        .arg(&config.runner_name)
        .arg("--worktree-root")
        .arg(&worktree)
        .arg("--state-file")
        .arg(data.join("runner-state.json"))
        .env("AUTOFLOW_RUNNER_TOKEN", token)
        .env("AUTOFLOW_PROVIDER", &config.provider)
        .env("AUTOFLOW_MODEL", &config.model)
        .env("AUTOFLOW_MOCK_LLM", config.mock_llm.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    for root in &config.repository_roots {
        command.arg("--root").arg(root);
    }
    for (role, route) in &config.agent_routes {
        let prefix = format!("AUTOFLOW_AGENT_{}", role.to_ascii_uppercase());
        command.env(format!("{prefix}_PROVIDER"), &route.provider);
        command.env(format!("{prefix}_MODEL"), &route.model);
    }
    apply_secret_environment(&mut command, &config)?;
    let child = command.spawn().map_err(|error| {
        format!(
            "Unable to start Local Runner at {}: {error}",
            runner_command.display()
        )
    })?;
    *process = Some(child);
    *state
        .last_exit
        .lock()
        .map_err(|_| "Runner state lock failed")? = None;
    drop(process);
    runner_status(app, state)
}

#[tauri::command]
fn stop_runner(app: AppHandle, state: State<'_, RunnerState>) -> Result<RunnerStatus, String> {
    let mut process = state
        .process
        .lock()
        .map_err(|_| "Runner state lock failed")?;
    if let Some(child) = process.as_mut() {
        child.kill().map_err(|error| error.to_string())?;
        let _ = child.wait();
    }
    *process = None;
    *state
        .last_exit
        .lock()
        .map_err(|_| "Runner state lock failed")? = Some("Stopped".into());
    drop(process);
    runner_status(app, state)
}

#[tauri::command]
fn runner_status(app: AppHandle, state: State<'_, RunnerState>) -> Result<RunnerStatus, String> {
    let mut process = state
        .process
        .lock()
        .map_err(|_| "Runner state lock failed")?;
    let mut pid = None;
    if let Some(child) = process.as_mut() {
        match child.try_wait().map_err(|error| error.to_string())? {
            None => pid = Some(child.id()),
            Some(status) => {
                *state
                    .last_exit
                    .lock()
                    .map_err(|_| "Runner state lock failed")? = Some(status.to_string());
                *process = None;
            }
        }
    }
    Ok(RunnerStatus {
        running: pid.is_some(),
        pid,
        log_path: runner_log_path(&app)?.to_string_lossy().into_owned(),
        last_exit: state
            .last_exit
            .lock()
            .map_err(|_| "Runner state lock failed")?
            .clone(),
    })
}

#[tauri::command]
fn read_runner_log(app: AppHandle) -> Result<String, String> {
    let path = runner_log_path(&app)?;
    if !path.is_file() {
        return Ok(String::new());
    }
    let mut file = File::open(path).map_err(|error| error.to_string())?;
    let length = file.metadata().map_err(|error| error.to_string())?.len();
    file.seek(SeekFrom::Start(length.saturating_sub(64 * 1024)))
        .map_err(|error| error.to_string())?;
    let mut content = String::new();
    file.read_to_string(&mut content)
        .map_err(|error| error.to_string())?;
    Ok(content
        .lines()
        .rev()
        .take(200)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect::<Vec<_>>()
        .join("\n"))
}

#[tauri::command]
fn open_dashboard(app: AppHandle) -> Result<(), String> {
    let config = read_config(&app)?;
    let url = Url::parse(&config.server_url).map_err(|error| error.to_string())?;
    if let Some(window) = app.get_webview_window("dashboard") {
        window.set_focus().map_err(|error| error.to_string())?;
        return Ok(());
    }
    WebviewWindowBuilder::new(&app, "dashboard", WebviewUrl::External(url))
        .title("AutoFlow Tasks")
        .inner_size(1440.0, 920.0)
        .min_inner_size(920.0, 640.0)
        .build()
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn terminate_runner(app: &AppHandle) {
    if let Some(state) = app.try_state::<RunnerState>() {
        if let Ok(mut process) = state.process.lock() {
            if let Some(child) = process.as_mut() {
                let _ = child.kill();
                let _ = child.wait();
            }
            *process = None;
        }
    }
}

pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(RunnerState::default())
        .invoke_handler(tauri::generate_handler![
            load_desktop_config,
            save_desktop_config,
            probe_server,
            pick_repository_root,
            start_runner,
            stop_runner,
            runner_status,
            read_runner_log,
            open_dashboard,
        ])
        .build(tauri::generate_context!())
        .expect("error while building AutoFlow Desktop");
    app.run(|app_handle, event| {
        if matches!(event, RunEvent::Exit) {
            terminate_runner(app_handle);
        }
    });
}
