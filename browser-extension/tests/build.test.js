import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import {execFileSync} from "node:child_process";
import {fileURLToPath} from "node:url";
import path from "node:path";

test("browser builds are deterministic, contain the correct manifest and exclude development files", async () => {
  const source = fileURLToPath(new URL("..", import.meta.url));
  const script = fileURLToPath(new URL("../../scripts/build-browser-extension.py", import.meta.url));
  const output = await fs.mkdtemp(path.join(source, "tests", ".build-test-"));
  const python = process.env.ATEABLE_TEST_PYTHON || "python3";
  try {
    const first = JSON.parse(execFileSync(python, [script, "--output-dir", output], {encoding: "utf8"}));
    const second = JSON.parse(execFileSync(python, [script, "--output-dir", output], {encoding: "utf8"}));
    assert.deepEqual(first, second);
    assert.equal(first.packages.length, 3);
    const firefox = first.packages.find(item => item.path.endsWith(".xpi"));
    const firefoxZip = first.packages.find(item => item.browser === "firefox" && item.path.endsWith(".zip"));
    assert.equal(firefox.sha256, firefoxZip.sha256);
    const inspect = "import json,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); print(json.dumps({'names':z.namelist(),'manifest':json.loads(z.read('manifest.json')),'dates':[i.date_time for i in z.infolist()]}))";
    for (const item of first.packages) {
      const archive = JSON.parse(execFileSync(python, ["-c", inspect, item.path], {encoding: "utf8"}));
      assert.ok(archive.names.includes("extension-api.js"));
      assert.equal(archive.names.some(name => name.startsWith("tests/") || name === "manifest.firefox.json" || name === "package.json"), false);
      assert.ok(archive.dates.every(value => JSON.stringify(value) === "[1980,1,1,0,0,0]"));
      if (item.browser === "firefox") {
        assert.equal(archive.manifest.background.service_worker, undefined);
        assert.deepEqual(archive.manifest.background.scripts, ["background.js"]);
      } else assert.equal(archive.manifest.background.service_worker, "background.js");
    }
  } finally { await fs.rm(output, {recursive: true, force: true}); }
});
