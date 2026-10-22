"use strict";

// Workbench command contracts were checked against VS Code tag 1.143.0.
// They are implementation interfaces, not a stable external Copilot API.
const COMMANDS = Object.freeze({
  reveal: "workbench.action.chat.open",
  newEditor: "workbench.action.openChat",
  local: "workbench.action.chat.newLocalChat",
  submit: "workbench.action.chat.submit",
  queue: "workbench.action.chat.queueMessage",
  steer: "workbench.action.chat.steerWithMessage"
});

class BridgeError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.statusCode = statusCode;
  }
}

function validateMessage(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new BridgeError(400, "The request must be a JSON object.");
  }
  const allowed = new Set(["message", "target", "behavior", "inputMode"]);
  if (Object.keys(value).some(key => !allowed.has(key))) {
    throw new BridgeError(400, "Allowed fields: message, target, behavior, inputMode.");
  }
  if (typeof value.message !== "string" || !value.message.trim()) {
    throw new BridgeError(400, "message must be a nonempty string.");
  }
  if (Buffer.byteLength(value.message, "utf8") > 65536) {
    throw new BridgeError(413, "message must be at most 64 KiB of UTF-8 text.");
  }
  const request = {
    message: value.message,
    target: value.target === undefined ? "current" : value.target,
    behavior: value.behavior === undefined ? "queue" : value.behavior,
    inputMode: value.inputMode === undefined ? "preserve" : value.inputMode
  };
  for (const [key, values] of [
    ["target", ["current", "new"]],
    ["behavior", ["queue", "steer", "interrupt"]],
    ["inputMode", ["preserve", "composer"]]
  ]) {
    if (!values.includes(request[key])) {
      throw new BridgeError(400, `${key} must be one of: ${values.join(", ")}.`);
    }
  }
  return request;
}

async function dispatchMessage(vscode, request) {
  const available = new Set(await vscode.commands.getCommands(true));
  const required = [COMMANDS.reveal, COMMANDS.submit];
  if (request.target === "new") required.push(COMMANDS.newEditor, COMMANDS.local);
  if (request.inputMode === "composer" && request.behavior !== "interrupt") {
    required.push(COMMANDS[request.behavior]);
  }
  const missing = required.filter(id => !available.has(id));
  if (missing.length) {
    throw new BridgeError(409, `Required VS Code commands are unavailable: ${missing.join(", ")}. Enable Copilot Chat and use a supported VS Code version.`);
  }
  const run = (id, ...args) => vscode.commands.executeCommand(id, ...args);

  if (request.target === "new") {
    // A fresh editor is created BEFORE newLocalChat. Calling newLocalChat on
    // the old widget would stop/replace its session. Do not use sendToNewChat:
    // that command explicitly cancels the previous active request.
    await run(COMMANDS.newEditor);
    await run(COMMANDS.local);
  }

  if (request.inputMode === "composer") {
    // Explicit opt-in: replaces the current draft and uses its attachments.
    await run(COMMANDS.reveal, {
      query: request.message,
      isPartialQuery: true
    });
    if (request.behavior === "interrupt") {
      await run(COMMANDS.submit, {
        acceptInputOptions: { cancelCurrentRequest: true }
      });
    } else {
      // Both queue and steer handlers also send normally when idle.
      await run(COMMANDS[request.behavior]);
    }
  } else {
    await run(COMMANDS.reveal);
    const acceptInputOptions = {
      preserveInput: true,
      preserveFocus: true,
      enableImplicitContext: false
    };
    if (request.behavior === "interrupt") {
      acceptInputOptions.cancelCurrentRequest = true;
    } else {
      acceptInputOptions.queue = request.behavior === "queue" ? "queued" : "steering";
    }
    await run(COMMANDS.submit, {
      inputValue: request.message,
      acceptInputOptions
    });
  }

  // The workbench handler does not await acceptInput. Never claim delivery,
  // model completion, a response stream, or an acknowledgment from Copilot.
  return {
    status: "dispatched",
    target: request.target,
    behavior: request.behavior,
    inputMode: request.inputMode,
    note: "Command dispatched to VS Code. Check chat for acceptance, approvals, and the response."
  };
}

module.exports = { BridgeError, COMMANDS, validateMessage, dispatchMessage };
