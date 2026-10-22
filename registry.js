"use strict";

const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

function getConnectionDirectory(env = process.env, home = os.homedir()) {
  const override = env.COPILOT_WINDOW_BRIDGE_DIR;
  if (override !== undefined) {
    if (!override || !path.isAbsolute(override)) {
      throw new Error("COPILOT_WINDOW_BRIDGE_DIR must be an absolute directory path.");
    }
    return path.normalize(override);
  }
  return path.join(home, ".copilot-window-bridge", "connections");
}

async function writeConnection({ metadata, url, token }, directory = getConnectionDirectory()) {
  if (!path.isAbsolute(directory)) throw new Error("Connection directory must be an absolute path.");
  if (!/^[a-f0-9-]{36}$/i.test(metadata.instanceId)) {
    throw new Error("A connection requires a UUID instance ID.");
  }
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const stat = await fs.lstat(directory);
  if (stat.isSymbolicLink() || !stat.isDirectory()) {
    throw new Error("Connection directory must be a real directory, not a symbolic link.");
  }
  if (process.platform !== "win32") {
    if (typeof process.getuid === "function" && stat.uid !== process.getuid()) {
      throw new Error("Connection directory must be owned by the current user.");
    }
    if ((stat.mode & 0o077) !== 0) {
      throw new Error("Connection directory must be private to the current user (permissions 0700).");
    }
  }

  const file = path.join(directory, `connection-${metadata.instanceId}.json`);
  // Exclusive creation prevents overwriting another window's descriptor.
  const handle = await fs.open(file, "wx", 0o600);
  try {
    await handle.writeFile(JSON.stringify({ ...metadata, url, token }, null, 2) + "\n", "utf8");
  } catch (error) {
    await handle.close();
    await fs.rm(file, { force: true });
    throw error;
  }
  await handle.close();
  return file;
}

async function removeConnection(file) {
  await fs.rm(file, { force: true });
}

module.exports = { getConnectionDirectory, writeConnection, removeConnection };
