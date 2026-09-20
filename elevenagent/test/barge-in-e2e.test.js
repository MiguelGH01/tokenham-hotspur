import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("end-to-end: post-interruption audio is dropped, clear fires once, next turn flows", () => {
  const helper = fileURLToPath(new URL("../e2e-helpers/barge-in-e2e.mjs", import.meta.url));
  const result = spawnSync(process.execPath, [helper], { encoding: "utf8", timeout: 30_000 });
  assert.equal(result.status, 0, result.stderr || result.stdout);
});
