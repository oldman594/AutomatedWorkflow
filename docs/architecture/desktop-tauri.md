# AutoFlow Desktop Architecture

## Decision

AutoFlow is split into a remotely deployed control plane and a locally installed execution
plane. Tauri is the desktop application framework; it does not replace the server implementation.
The existing server is Python/FastAPI, not C++.

```text
Tauri Desktop (Rust)                     AutoFlow Server (Python)
┌────────────────────────────┐          ┌────────────────────────────┐
│ trusted controller WebView │          │ email authentication       │
│ config + system keyring    │  HTTPS   │ projects / RBAC / queue    │
│ Runner lifecycle + logs    ├─────────►│ agent orchestration        │
│                            │ WebSocket│ PostgreSQL / SMTP / Git API│
│ Python Runner sidecar      ├─────────►│ task gateway               │
│ Git / Docker / AI client   │          └────────────────────────────┘
└──────────────┬─────────────┘
               │
       local repositories
```

## Trust Boundaries

- The `controller` WebView loads only bundled assets and is the only window granted Tauri IPC.
- The task dashboard is an external Server URL in a separate WebView. It does not receive Tauri
  IPC permissions and therefore cannot read local paths, start processes, or access secrets.
- Runner Token and provider API keys are stored in the OS keyring. They are injected into the
  Runner environment and never written to desktop JSON, browser storage, logs, or command args.
- The Runner is launched with a fixed executable plus an argument array. No shell evaluation is
  used. Repository roots are canonicalized and must already exist.
- Closing the application terminates the managed Runner process. The Runner keeps its protocol
  state and unacknowledged envelopes under the Tauri application data directory for reconnect.

## Runtime Ownership

The Rust desktop layer owns native windows, configuration, Keyring integration, process lifecycle,
readiness probes, and log display. The Python sidecar owns the proven repository, sandbox, Agent,
build, test, retry, and Runner protocol behavior. The Server owns identity, authorization, durable
queue state, project configuration, SMTP login, and GitHub/GitLab publishing.

Rewriting the Server or Runner in Rust is explicitly deferred. A migration is justified only when
measurements show startup size, memory, security review, or updater constraints that cannot be
solved by the signed sidecar. Protocol compatibility tests must precede any such replacement.

## Packaging

`scripts/build_desktop.sh` builds `autoflow-runner` with PyInstaller, copies it into the Tauri
resource directory, installs the locked Node CLI dependency, and produces the native installer.
The sidecar remains a separate process so it can be signed, versioned, crash-isolated, and later
replaced without changing the desktop IPC contract.

Development can set `Runner executable` to the virtual environment entry point, normally
`.venv/bin/autoflow-runner`. Release builds leave this field empty and resolve the packaged
resource automatically.
