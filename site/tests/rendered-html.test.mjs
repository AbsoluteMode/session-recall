import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the finished Session Recall landing page", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /Session Recall — shared memory for coding agents/i);
  assert.match(html, /Your coding agents forget/i);
  assert.match(html, /Session Recall doesn/i);
  assert.match(html, /recall_search/);
  assert.match(html, /expand_around/);
  assert.match(html, /recent_sessions/);
  assert.match(html, /Claude Code/);
  assert.match(html, /Codex/);
  assert.match(html, /Cursor/);
  assert.match(html, /Local-first/i);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape|react-loading-skeleton/i);
});

test("ships the bespoke social card and removes the disposable preview", async () => {
  const og = await readFile(new URL("../public/og.png", import.meta.url));
  assert.deepEqual([...og.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);

  await assert.rejects(access(new URL("../app/_sites-preview", import.meta.url)));
});
