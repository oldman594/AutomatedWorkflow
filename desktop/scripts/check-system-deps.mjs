import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";

if (process.platform !== "linux") {
  process.exit(0);
}

const requiredModules = [
  "glib-2.0",
  "gobject-2.0",
  "gio-2.0",
  "gdk-3.0",
  "webkit2gtk-4.1",
];

const pkgConfig = spawnSync("pkg-config", ["--exists", ...requiredModules], {
  stdio: "ignore",
});

if (pkgConfig.error?.code === "ENOENT") {
  fail(["pkg-config"]);
}

const missing = requiredModules.filter((module) => {
  const result = spawnSync("pkg-config", ["--exists", module], {
    stdio: "ignore",
  });
  return result.status !== 0;
});

if (missing.length > 0) {
  fail(missing);
}

function fail(missingModules) {
  let distribution = "Linux";
  try {
    const osRelease = readFileSync("/etc/os-release", "utf8");
    const prettyName = osRelease.match(/^PRETTY_NAME="?([^"\n]+)"?$/m)?.[1];
    distribution = prettyName ?? distribution;
  } catch {
    // The generic Linux instructions below are still useful without os-release.
  }

  console.error(`\nTauri system dependency check failed on ${distribution}.`);
  console.error(`Missing pkg-config modules: ${missingModules.join(", ")}\n`);
  console.error("On Ubuntu/Debian, install them with:\n");
  console.error("  sudo apt-get update");
  console.error(
    "  sudo apt-get install -y build-essential ca-certificates pkg-config libssl-dev libgtk-3-dev libwebkit2gtk-4.1-dev libayatana-appindicator3-dev librsvg2-dev libdbus-1-dev",
  );
  console.error("\nThen rerun: npm run dev\n");
  process.exit(1);
}
