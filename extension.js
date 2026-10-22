"use strict";

const vscode = require("vscode");
const crypto = require("node:crypto");
const { startServer } = require("./server");
const { dispatchMessage } = require("./dispatch");
const { writeConnection, removeConnection } = require("./registry");
const { version: bridgeVersion } = require("./package.json");

let active;
let starting;
let output;
let badge;

async function stop() {
  if (starting) {
    try { await starting; } catch { /* startup error is reported by its caller */ }
  }
  const previous = active;
  active = undefined;
  if (badge) badge.hide();
  if (!previous) return;
  try {
    await previous.server.close();
  } finally {
    await removeConnection(previous.file);
  }
  if (output) output.appendLine("Bridge stopped for this window.");
}

async function start() {
  if (active) return active;
  if (starting) return starting;
  starting = (async () => {
    const instanceId = crypto.randomUUID();
    const token = crypto.randomBytes(32).toString("hex");
    const metadata = {
      instanceId,
      workspace: vscode.workspace.name || "Untitled window",
      folders: (vscode.workspace.workspaceFolders || []).map(folder => folder.uri.toString()),
      vscodeVersion: vscode.version,
      bridgeVersion,
      appName: vscode.env.appName,
      pid: process.pid,
      startedAt: new Date().toISOString()
    };
    const server = await startServer({
      token,
      metadata,
      dispatch: request => dispatchMessage(vscode, request)
    });
    let file;
    try {
      file = await writeConnection({ metadata, url: server.url, token });
    } catch (error) {
      await server.close();
      throw error;
    }
    active = { server, file, metadata };
    output.appendLine(`Window: ${metadata.workspace}`);
    output.appendLine(`Instance ID: ${metadata.instanceId}`);
    output.appendLine(`Connection file: ${file}`);
    output.appendLine("Current targets the last-focused chat. Responses acknowledge dispatch, not delivery.");
    badge.text = "$(plug) Copilot Bridge";
    badge.tooltip = `Bridge active for ${metadata.workspace} (${instanceId.slice(0, 8)}). Click to copy its connection file path.`;
    badge.show();
    return active;
  })();
  try {
    return await starting;
  } finally {
    starting = undefined;
  }
}

function activate(context) {
  output = vscode.window.createOutputChannel("Copilot Window Bridge");
  badge = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 20);
  badge.command = "copilotWindowBridge.copyConnectionPath";
  const register = (id, fn) => vscode.commands.registerCommand(id, async () => {
    try {
      await fn();
    } catch (error) {
      vscode.window.showErrorMessage(`Copilot Bridge: ${error.message || error}`);
    }
  });
  context.subscriptions.push(
    output,
    badge,
    register("copilotWindowBridge.start", async () => {
      const bridge = await start();
      const action = await vscode.window.showInformationMessage(
        `Copilot Bridge is ready for ${bridge.metadata.workspace} (${bridge.metadata.instanceId.slice(0, 8)}).`,
        "Copy Connection File Path",
        "Show Details"
      );
      if (action === "Copy Connection File Path") {
        await vscode.env.clipboard.writeText(bridge.file);
      } else if (action === "Show Details") {
        output.show();
      }
    }),
    register("copilotWindowBridge.copyConnectionPath", async () => {
      const bridge = await start();
      await vscode.env.clipboard.writeText(bridge.file);
      vscode.window.showInformationMessage("Copied this window's bridge connection file path.");
    }),
    register("copilotWindowBridge.stop", async () => {
      await stop();
      vscode.window.showInformationMessage("Copilot Bridge stopped for this window.");
    })
  );
}

async function deactivate() {
  await stop();
}

module.exports = { activate, deactivate };
