// Fails when src/lib/api-types.ts is out of date with apps/api/openapi.json.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const out = join(mkdtempSync(join(tmpdir(), "aq-api-")), "api-types.ts");
execFileSync("npx", ["openapi-typescript", "../api/openapi.json", "-o", out], { stdio: "ignore" });
if (readFileSync(out, "utf8") !== readFileSync("src/lib/api-types.ts", "utf8")) {
  console.error("src/lib/api-types.ts is stale: run `npm run gen:api` and commit the result.");
  process.exit(1);
}
console.log("api-types.ts matches apps/api/openapi.json");
