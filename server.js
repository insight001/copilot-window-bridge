"use strict";

const http = require("node:http");
const crypto = require("node:crypto");
const { BridgeError, validateMessage } = require("./dispatch");

function json(res, status, value) {
  if (res.destroyed || res.writableEnded) return;
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff"
  });
  res.end(JSON.stringify(value));
}

function equalToken(actual, expected) {
  if (typeof actual !== "string") return false;
  const a = Buffer.from(actual);
  const b = Buffer.from(expected);
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let tooLarge = false;
    req.on("data", chunk => {
      size += chunk.length;
      if (size > 80 * 1024) {
        if (!tooLarge) reject(new BridgeError(413, "JSON body exceeds 80 KiB."));
        tooLarge = true;
        return;
      }
      if (!tooLarge) chunks.push(chunk);
    });
    req.on("end", () => {
      if (tooLarge) return;
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      } catch {
        reject(new BridgeError(400, "Invalid JSON."));
      }
    });
    req.on("aborted", () => reject(new BridgeError(400, "Request aborted.")));
    req.on("error", reject);
  });
}

async function startServer({ token, metadata, dispatch }) {
  let chain = Promise.resolve();
  let pending = 0;
  let stopped = false;
  let port;

  const server = http.createServer(async (req, res) => {
    try {
      if (stopped) throw new BridgeError(503, "Bridge is stopping.");
      if (req.headers.host !== `127.0.0.1:${port}`) {
        throw new BridgeError(403, "Use the exact loopback URL in the connection file.");
      }
      // This bridge is for local programs, not cross-origin browser requests.
      if (req.headers.origin !== undefined) {
        throw new BridgeError(403, "Browser-origin requests are not accepted.");
      }
      if (!equalToken(req.headers.authorization, `Bearer ${token}`)) {
        throw new BridgeError(401, "Missing or invalid bridge token.");
      }
      if (req.method === "GET" && req.url === "/health") {
        json(res, 200, { status: "ready", ...metadata, pendingDispatches: pending });
        return;
      }
      if (req.method !== "POST" || req.url !== "/message") {
        throw new BridgeError(404, "Use GET /health or POST /message.");
      }
      const mediaType = (req.headers["content-type"] || "").split(";", 1)[0].trim().toLowerCase();
      if (mediaType !== "application/json") {
        throw new BridgeError(415, "Content-Type must be application/json.");
      }
      const request = validateMessage(await readJson(req));
      if (stopped) throw new BridgeError(503, "Bridge is stopping.");
      if (pending >= 32) throw new BridgeError(429, "Too many pending dispatches.");
      const requestId = crypto.randomUUID();
      pending++;
      // Serialize bridge command sequences. This does not lock user focus or
      // wait for Copilot to finish; VS Code owns its own request queue.
      const work = chain.then(async () => {
        if (stopped) throw new BridgeError(503, "Bridge stopped before dispatch.");
        return dispatch(request);
      });
      chain = work.then(() => undefined, () => undefined);
      try {
        const result = await work;
        json(res, 202, { ...result, requestId, instanceId: metadata.instanceId });
      } finally {
        pending--;
      }
    } catch (error) {
      json(res, error.statusCode || 500, { status: "error", error: error.message || "Bridge failed." });
    }
  });
  server.requestTimeout = 15000;
  server.headersTimeout = 10000;
  server.keepAliveTimeout = 1000;
  server.on("clientError", (_err, socket) => {
    if (socket.writable) socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  port = server.address().port;
  return {
    url: `http://127.0.0.1:${port}`,
    async close() {
      stopped = true;
      await new Promise(resolve => {
        server.close(resolve);
        server.closeAllConnections();
      });
    }
  };
}

module.exports = { startServer };
