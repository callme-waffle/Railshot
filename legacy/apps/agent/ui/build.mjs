import { build } from "esbuild";
import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
const output = await build({
  entryPoints: [fileURLToPath(new URL("./insights-app.js", import.meta.url))],
  bundle: true,
  minify: true,
  write: false,
  format: "esm",
  target: "es2022",
  legalComments: "inline",
});
const script = output.outputFiles[0].text.replace(/<\/script/gi, "<\\/script");
const html = `<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Railshot 운영 인사이트</title><body style="margin:0;background:#f5f7f4"><main id="insights">배포 정보를 기다리고 있습니다.</main><script type="module">${script}</script></body></html>`;
const destination = new URL("../src/insights-app.html", import.meta.url);
// A committed, reproducible resource keeps the existing image/release pipeline unchanged.
if (process.argv.includes("--check")) {
  if ((await readFile(destination, "utf8")) !== html)
    throw new Error(
      "Rebuild MCP App with npm run build:insights --workspace @railshot/agent",
    );
} else await writeFile(destination, html);
