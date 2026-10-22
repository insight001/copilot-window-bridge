"use strict";

// Local transport and mocked workbench tests. These do not run Copilot.
const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { COMMANDS, validateMessage, dispatchMessage } = require("./dispatch");
const { startServer } = require("./server");

function mockCode(missing = []) {
  const calls = [];
  return {
    calls,
    commands: {
      getCommands: async () => Object.values(COMMANDS).filter(id => !missing.includes(id)),
      executeCommand: async (id, ...args) => { calls.push([id, ...args]); }
    }
  };
}

function call(url, token, body, headers = {}, method = "POST", route = "/message") {
  const data = body === undefined ? undefined : typeof body === "string" ? body : JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const req = http.request(url + route, {
      method,
      headers: {
        Authorization: "Bearer " + token,
        "Content-Type": "application/json",
        ...headers
      }
    }, res => {
      const chunks = [];
      res.on("data", chunk => chunks.push(chunk));
      res.on("end", () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(Buffer.concat(chunks)) }); }
        catch (error) { reject(error); }
      });
    });
    req.on("error", reject);
    req.end(data);
  });
}

test("invalid payloads and missing commands fail before any UI mutation", async () => {
  for (const input of [null, [], {}, { message: " " }, { message: "ok", target: "wrong" },
    { message: "ok", typo: true }, { message: "é".repeat(32769) }]) {
    assert.throws(() => validateMessage(input));
  }
  const vscode = mockCode([COMMANDS.local]);
  await assert.rejects(dispatchMessage(vscode, validateMessage({ message: "hi", target: "new" })), /unavailable/);
  assert.deepEqual(vscode.calls, []);
});

test("preserve mode sends the supplied text without prefilling or borrowing a draft", async () => {
  for (const behavior of ["queue", "steer", "interrupt"]) {
    const vscode = mockCode();
    const text = "Keep the original\nformatting 🧪";
    const result = await dispatchMessage(vscode, validateMessage({ message: text, behavior }));
    assert.equal(result.status, "dispatched");
    assert.deepEqual(vscode.calls[0], [COMMANDS.reveal]);
    assert.equal(vscode.calls.length, 2);
    const payload = vscode.calls[1][1];
    assert.equal(vscode.calls[1][0], COMMANDS.submit);
    assert.equal(payload.inputValue, text);
    assert.equal(payload.acceptInputOptions.preserveInput, true);
    assert.equal(payload.acceptInputOptions.enableImplicitContext, false);
    if (behavior === "interrupt") {
      assert.equal(payload.acceptInputOptions.cancelCurrentRequest, true);
      assert.equal(payload.acceptInputOptions.queue, undefined);
    } else {
      assert.equal(payload.acceptInputOptions.queue, behavior === "queue" ? "queued" : "steering");
      assert.equal(payload.acceptInputOptions.cancelCurrentRequest, undefined);
    }
  }
});

test("new chat opens a separate editor before selecting Local and never invokes sendToNewChat", async () => {
  const vscode = mockCode();
  await dispatchMessage(vscode, validateMessage({ message: "fresh task", target: "new" }));
  assert.deepEqual(vscode.calls.map(call => call[0]), [COMMANDS.newEditor, COMMANDS.local, COMMANDS.reveal, COMMANDS.submit]);
  assert.ok(!vscode.calls.some(call => /cancel|sendToNewChat/.test(call[0])));
});

test("composer mode explicitly prefills then uses queue, steer or stop-and-send", async () => {
  for (const behavior of ["queue", "steer", "interrupt"]) {
    const vscode = mockCode();
    await dispatchMessage(vscode, validateMessage({ message: "hello", inputMode: "composer", behavior }));
    assert.deepEqual(vscode.calls[0], [COMMANDS.reveal, { query: "hello", isPartialQuery: true }]);
    assert.deepEqual(vscode.calls[1], behavior === "interrupt"
      ? [COMMANDS.submit, { acceptInputOptions: { cancelCurrentRequest: true } }]
      : [COMMANDS[behavior]]);
  }
});

test("HTTP boundary rejects invalid auth, origin, Host, MIME, JSON and oversized payload before dispatch", async t => {
  const token = "a".repeat(64);
  let calls = 0;
  const server = await startServer({ token, metadata: { instanceId: "test-window" },
    dispatch: async () => { calls++; return { status: "dispatched" }; } });
  t.after(() => server.close());
  const payload = { message: "hi" };
  assert.equal((await call(server.url, "wrong", payload)).status, 401);
  assert.equal((await call(server.url, token, payload, { Origin: "https://example.test" })).status, 403);
  assert.equal((await call(server.url, token, payload, { Host: "example.test" })).status, 403);
  assert.equal((await call(server.url, token, payload, { "Content-Type": "text/plain" })).status, 415);
  assert.equal((await call(server.url, token, payload, { "Content-Type": "application/jsonx" })).status, 415);
  assert.equal((await call(server.url, token, "{bad")).status, 400);
  assert.equal((await call(server.url, token, { message: "é".repeat(32769) })).status, 413);
  assert.equal((await call(server.url, token, JSON.stringify({ message: "x".repeat(85000) }))).status, 413);
  assert.equal(calls, 0);
  const health = await call(server.url, token, undefined, {}, "GET", "/health");
  assert.equal(health.status, 200);
  assert.equal(health.body.instanceId, "test-window");
  assert.ok(!JSON.stringify(health).includes(token));
  assert.equal(calls, 0);
});

test("concurrent HTTP messages serialize and a failed dispatch does not poison later messages", async t => {
  const token = "b".repeat(64);
  let active = 0;
  let peak = 0;
  const seen = [];
  const server = await startServer({ token, metadata: { instanceId: "serial" }, dispatch: async request => {
    active++;
    peak = Math.max(peak, active);
    seen.push(request.message);
    await new Promise(resolve => setTimeout(resolve, 15));
    active--;
    if (request.message === "fail") throw new Error("Expected test failure");
    return { status: "dispatched" };
  } });
  t.after(() => server.close());
  const responses = await Promise.all(["first", "fail", "last"].map(message => call(server.url, token, { message })));
  assert.equal(peak, 1);
  assert.equal(seen.length, 3);
  assert.deepEqual(responses.map(result => result.status), [202, 500, 202]);
  assert.equal(responses[2].body.status, "dispatched");
});

test("Python client sends Unicode and action controls through the actual local HTTP server", async t => {
  const token = "c".repeat(64);
  const instanceId = "ccc00000-0000-4000-8000-000000000001";
  let received;
  const server = await startServer({ token, metadata: { instanceId, workspace: "python-test" }, dispatch: async request => {
    received = request;
    return { status: "dispatched", target: request.target, behavior: request.behavior };
  } });
  t.after(() => server.close());
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "copilot-bridge-test-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const connection = path.join(directory, "connection.json");
  await fs.writeFile(connection, JSON.stringify({ url: server.url, token, instanceId, workspace: "python-test" }), { mode: 0o600 });
  // Fits the 64 KiB UTF-8 limit; ASCII JSON escaping would exceed the body cap.
  const prompt = "Review the café code 🧪\n" + "界".repeat(16000);
  const promptFile = path.join(directory, "prompt.txt");
  await fs.writeFile(promptFile, prompt, "utf8");
  const result = await new Promise((resolve, reject) => {
    const child = spawn("python3", [path.join(__dirname, "send.py"), "--connection", connection,
      "--new", "--behavior", "steer", "--file", promptFile]);
    let out = "";
    let err = "";
    child.stdout.on("data", part => out += part);
    child.stderr.on("data", part => err += part);
    child.on("error", reject);
    child.on("close", code => resolve({ code, out, err }));
  });
  assert.equal(result.code, 0, result.err);
  assert.equal(JSON.parse(result.out).status, "dispatched");
  assert.deepEqual(received, { message: prompt, target: "new", behavior: "steer", inputMode: "preserve" });
  assert.ok(!result.out.includes(token));
});
