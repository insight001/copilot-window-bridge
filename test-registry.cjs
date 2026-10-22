"use strict";

// Filesystem registry tests; these do not launch VS Code or Copilot.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { getConnectionDirectory, writeConnection, removeConnection } = require("./registry");

const firstId = "a31e7902-853e-4ef8-98c4-3a97b716d112";
const secondId = "b20c6841-0f95-4267-b332-741227889dd2";

function descriptor(instanceId, port, token) {
  return {
    metadata: {
      instanceId,
      workspace: "same-project",
      folders: ["file:///example/same-project"],
      bridgeVersion: "0.2.0",
      vscodeVersion: "1.143.0"
    },
    url: `http://127.0.0.1:${port}`,
    token
  };
}

async function temporaryDirectory(t) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "copilot-registry-test-"));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return directory;
}

test("registry discovery uses one home directory and accepts only absolute overrides", () => {
  const home = path.resolve("example-home");
  assert.equal(getConnectionDirectory({}, home), path.join(home, ".copilot-window-bridge", "connections"));
  const custom = path.join(home, "custom-connections");
  assert.equal(getConnectionDirectory({ COPILOT_WINDOW_BRIDGE_DIR: custom }, home), custom);
  for (const invalid of ["", "relative/connections"]) {
    assert.throws(() => getConnectionDirectory({ COPILOT_WINDOW_BRIDGE_DIR: invalid }, home), /absolute/);
  }
});

test("same-workspace windows keep independent descriptors and stopping one leaves the other", async t => {
  const directory = path.join(await temporaryDirectory(t), "connections");
  const first = descriptor(firstId, 43121, "1".repeat(64));
  const second = descriptor(secondId, 43122, "2".repeat(64));
  const [firstFile, secondFile] = await Promise.all([
    writeConnection(first, directory),
    writeConnection(second, directory)
  ]);
  assert.notEqual(firstFile, secondFile);
  assert.deepEqual((await fs.readdir(directory)).sort(), [path.basename(firstFile), path.basename(secondFile)].sort());
  const firstRecord = JSON.parse(await fs.readFile(firstFile, "utf8"));
  const secondRecord = JSON.parse(await fs.readFile(secondFile, "utf8"));
  assert.deepEqual(firstRecord, { ...first.metadata, url: first.url, token: first.token });
  assert.deepEqual(secondRecord, { ...second.metadata, url: second.url, token: second.token });
  if (process.platform !== "win32") {
    assert.equal((await fs.stat(directory)).mode & 0o777, 0o700);
    for (const file of [firstFile, secondFile]) {
      assert.equal((await fs.stat(file)).mode & 0o777, 0o600);
    }
  }

  await removeConnection(firstFile);
  await removeConnection(firstFile); // Graceful cleanup also tolerates an absent file.
  await assert.rejects(fs.stat(firstFile), { code: "ENOENT" });
  assert.deepEqual(JSON.parse(await fs.readFile(secondFile, "utf8")), secondRecord);
});

test("an accidental duplicate instance ID cannot overwrite or remove another descriptor", async t => {
  const directory = path.join(await temporaryDirectory(t), "connections");
  const first = descriptor(firstId, 43121, "1".repeat(64));
  const file = await writeConnection(first, directory);
  const original = await fs.readFile(file, "utf8");
  await assert.rejects(writeConnection(descriptor(firstId, 43122, "2".repeat(64)), directory), { code: "EEXIST" });
  assert.equal(await fs.readFile(file, "utf8"), original);
});

test("invalid registry paths fail without writing a descriptor", async t => {
  const directory = await temporaryDirectory(t);
  const data = descriptor(firstId, 43121, "1".repeat(64));
  await assert.rejects(writeConnection(data, "relative-connections"), /absolute/);
  const ordinaryFile = path.join(directory, "ordinary-file");
  await fs.writeFile(ordinaryFile, "leave this file alone", { mode: 0o600 });
  await assert.rejects(writeConnection(data, ordinaryFile));
  assert.equal(await fs.readFile(ordinaryFile, "utf8"), "leave this file alone");
  await assert.rejects(writeConnection({ ...data, metadata: { ...data.metadata, instanceId: "../wrong" } }, directory), /UUID/);
});

test("registry rejects symlinks and directories readable by other users", { skip: process.platform === "win32" }, async t => {
  const directory = await temporaryDirectory(t);
  const data = descriptor(firstId, 43121, "1".repeat(64));
  const realDirectory = path.join(directory, "real");
  await fs.mkdir(realDirectory, { mode: 0o700 });
  const link = path.join(directory, "link");
  await fs.symlink(realDirectory, link, "dir");
  await assert.rejects(writeConnection(data, link), /symbolic link/);
  assert.deepEqual(await fs.readdir(realDirectory), []);

  await fs.chmod(realDirectory, 0o755);
  await assert.rejects(writeConnection(data, realDirectory), /private/);
  assert.equal((await fs.stat(realDirectory)).mode & 0o777, 0o755);
  assert.deepEqual(await fs.readdir(realDirectory), []);
});
