use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::Manager;

struct PythonProcess(Mutex<Option<Child>>);

/// 查找 backend 目录（支持 release exe 和开发模式两种路径）
fn find_backend_dir() -> Result<std::path::PathBuf, String> {
    let exe = std::env::current_exe().map_err(|e| e.to_string())?;
    let mut debug_info = format!("exe={}", exe.display());

    // 从 exe 向上查找，直到找到包含 backend/main.py 的目录
    let mut dir = exe.as_path();
    for _ in 0..8 {
        if let Some(parent) = dir.parent() {
            dir = parent;
            let backend = dir.join("backend");
            if backend.join("main.py").exists() {
                return Ok(backend);
            }
        } else {
            break;
        }
    }

    // 再从 cwd 向上找
    if let Ok(cwd) = std::env::current_dir() {
        debug_info.push_str(&format!(", cwd={}", cwd.display()));
        let mut dir = cwd.as_path();
        for _ in 0..5 {
            let backend = dir.join("backend");
            if backend.join("main.py").exists() {
                return Ok(backend);
            }
            match dir.parent() {
                Some(p) => dir = p,
                None => break,
            }
        }
    }

    Err(format!("找不到 backend/main.py ({})", debug_info))
}

#[tauri::command]
fn start_backend(state: tauri::State<PythonProcess>) -> Result<String, String> {
    // 如果已有进程在跑，先杀掉
    if let Ok(mut guard) = state.0.lock() {
        if let Some(ref mut child) = *guard {
            let _ = child.kill();
            let _ = child.wait();
        }
        *guard = None;
    }

    let backend_dir = find_backend_dir()?;

    // 优先使用 venv 中的 python
    let venv_python = backend_dir.join(".venv").join("Scripts").join("python.exe");
    let python = if venv_python.exists() {
        venv_python.clone()
    } else {
        std::path::PathBuf::from("python")
    };

    // 日志文件放在 backend 目录，方便排查
    let log_file = backend_dir.join("_backend.log");
    let log_handle = std::fs::File::create(&log_file)
        .map_err(|e| format!("创建日志文件失败: {}", e))?;
    let err_handle = log_handle.try_clone()
        .map_err(|e| format!("克隆日志句柄失败: {}", e))?;

    let child = Command::new(&python)
        .arg("main.py")
        .current_dir(&backend_dir)
        .env("PYTHONIOENCODING", "utf-8")
        .stdout(Stdio::from(log_handle))
        .stderr(Stdio::from(err_handle))
        .spawn()
        .map_err(|e| format!("启动 Python 后端失败: {} (python={}, dir={})",
            e, python.display(), backend_dir.display()))?;

    let pid = child.id();

    // 存入状态
    if let Ok(mut guard) = state.0.lock() {
        *guard = Some(child);
    }

    Ok(format!("后端已启动 (PID: {}, dir: {}, python: {})", pid, backend_dir.display(), python.display()))
}

#[tauri::command]
fn stop_backend(state: tauri::State<PythonProcess>) -> Result<String, String> {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(ref mut child) = *guard {
            let _ = child.kill();
            let _ = child.wait();
            *guard = None;
            return Ok("后端已停止".into());
        }
    }
    Ok("后端未运行".into())
}

fn kill_python(state: &PythonProcess) {
    if let Ok(mut guard) = state.0.lock() {
        if let Some(ref mut child) = *guard {
            let _ = child.kill();
            let _ = child.wait();
        }
        *guard = None;
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(PythonProcess(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![start_backend, stop_backend])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                // 窗口关闭时杀掉 Python 进程
                if let Some(state) = window.try_state::<PythonProcess>() {
                    kill_python(state.inner());
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
