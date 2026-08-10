import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const environment = { ...process.env };

if (
  process.platform === "linux" &&
  isWsl() &&
  environment.AUTOFLOW_WSL_SOFTWARE_RENDERING !== "0" &&
  environment.LIBGL_ALWAYS_SOFTWARE === undefined
) {
  environment.LIBGL_ALWAYS_SOFTWARE = "1";
  console.log("AutoFlow: using Mesa software rendering for WSL compatibility.");
}

const tauriCli = fileURLToPath(
  new URL("../node_modules/@tauri-apps/cli/tauri.js", import.meta.url),
);
const child = spawn(process.execPath, [tauriCli, "dev", ...process.argv.slice(2)], {
  env: environment,
  stdio: "inherit",
});

child.on("error", (error) => {
  console.error(`Unable to start the Tauri CLI: ${error.message}`);
  process.exitCode = 1;
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exitCode = code ?? 1;
});

function isWsl() {
  if (process.env.WSL_DISTRO_NAME || process.env.WSL_INTEROP) {
    return true;
  }

  try {
    return /microsoft/i.test(readFileSync("/proc/sys/kernel/osrelease", "utf8"));
  } catch {
    return false;
  }
}
