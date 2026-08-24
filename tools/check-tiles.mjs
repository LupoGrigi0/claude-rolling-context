/* Drive ferry-metrics.html's ACTUAL fillTiles() (the DOM half of the page, the
 * part check-parser.mjs deliberately cannot reach) against a metrics.csv with a
 * blank `window`, and assert what a human would see: no "NaN", no "0%", no
 * empty tile — and a chart that still has points to draw.
 *
 * Both fillTiles and the FM core are sliced out of the shipped HTML, so this
 * tests the page, not a copy of it. Companion to check-parser.mjs, which
 * covers the pure core; this covers the half that touches the DOM.
 *
 *   node tools/check-tiles.mjs
 *
 * stdlib only (node's vm + fs).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "ferry-metrics.html"), "utf8");

// --- FM core (pure) -------------------------------------------------------
const a = html.indexOf("FM-CORE-BEGIN"), b = html.indexOf("FM-CORE-END");
const core = html.slice(html.indexOf("\n", a) + 1, html.lastIndexOf("\n", b));

// --- fillTiles (DOM), sliced by brace matching ----------------------------
function slice(fnName) {
  const start = html.indexOf("function " + fnName + "(");
  if (start < 0) throw new Error("no " + fnName);
  let i = html.indexOf("{", start), depth = 0;
  for (let j = i; j < html.length; j++) {
    const c = html[j];
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (depth === 0) return html.slice(start, j + 1); }
  }
  throw new Error("unbalanced " + fnName);
}
const fillTiles = slice("fillTiles");

// --- the smallest DOM that fillTiles actually touches ---------------------
const nodes = {};
function node(id) {
  if (!nodes[id]) nodes[id] = {
    id, textContent: "", title: "", className: "",
    style: {}, children: [],
    firstChild: { style: {} },
    appendChild(c) { this.children.push(c); this.textContent += (c.textContent || ""); }
  };
  return nodes[id];
}
const document = {
  getElementById: node,
  createElement: () => ({ textContent: "", className: "", style: {} }),
  createTextNode: (t) => ({ textContent: String(t) })
};

const sandbox = { Date, Math, Number, isFinite, String, RegExp, JSON, console, document };
vm.createContext(sandbox);
vm.runInContext(core, sandbox);
vm.runInContext(`
  var FMx = FM;
  function $(id){ return document.getElementById(id); }
  var model = null, lastGood = 0;
  function refreshAge(){ $("t-age").textContent = "just now"; }
  function setLive(){}
  ${fillTiles}
`, sandbox);

let pass = 0, fail = 0;
const ok = (n, c, x) => { if (c) { pass++; console.log("  ok   " + n); }
                          else { fail++; console.log("  FAIL " + n + (x === undefined ? "" : " -> " + JSON.stringify(x))); } };

const H = "ts_iso,event,cycle,context_tokens,tokens_in,tokens_evicted,archive_lines,archive_bytes,carry_chars,model,window,note";

function drive(csv, label) {
  for (const k of Object.keys(nodes)) delete nodes[k];
  sandbox.CSV = csv;
  vm.runInContext(`
    var __p = FM.parseMetrics(CSV), __m = FM.shape(__p);
    model = __m;
    fillTiles(__m);
    var __out = {};
    __out.tiles = {};
    ["t-ctx","t-pct","t-cyc","t-evict","t-arch","t-age","t-win","t-cycfoot",
     "t-evictfoot","t-archfoot","t-agefoot","t-state"].forEach(function(id){
       __out.tiles[id] = $(id).textContent;
     });
    __out.meterClass = $("t-meter").className;
    __out.meterWidth = $("t-meter").firstChild.style.width;
    __out.points = __m.requests.length;
    __out.pct = __m.stats.pct;
    __out;
  `, sandbox);
  const out = vm.runInContext("__out", sandbox);
  console.log("\n" + label);
  console.log("  tiles:", JSON.stringify(out.tiles, null, 0));
  console.log("  meter:", out.meterClass, "width=", out.meterWidth);
  return out;
}

const NOWIN = H + "\n" +
  '2026-08-22T10:00:00Z,proxy_start,,,,,,,,,,"mode=ferry, trigger=100000, target=40000"\n' +
  "2026-08-22T10:00:05Z,request,,41200,,,,,,claude-x,,\n" +
  "2026-08-22T10:01:05Z,request,,52900,11700,,,,,claude-x,,\n" +
  "2026-08-22T10:02:05Z,curation,1,52900,,9400,,,120,,,estimate:chars/4\n" +
  "2026-08-22T10:02:05Z,archive_write,1,,,,12,4096,,,,archive_20260822.jsonl#L1-L12\n";

console.log("[A] blank window, trigger known");
{
  const o = drive(NOWIN, "  rendered:");
  const all = Object.values(o.tiles).join(" | ");
  ok("nothing renders as NaN", !/NaN/.test(all), all);
  ok("nothing renders as undefined/null text", !/undefined|null/.test(all), all);
  ok("% of window tile shows an em dash, not 0.0%", o.tiles["t-pct"] === "—", o.tiles["t-pct"]);
  ok("its footer says the window is not logged",
     o.tiles["t-win"] === "window not logged", o.tiles["t-win"]);
  ok("the context tile still shows the real number",
     o.tiles["t-ctx"] === "52,900", o.tiles["t-ctx"]);
  ok("the meter falls back to the TRIGGER and still fills",
     /trigger 100,000/.test(o.tiles["t-state"]) && o.meterWidth !== "0%", [o.tiles["t-state"], o.meterWidth]);
  ok("meter width is a real percentage, not NaN%", /^[\d.]+%$/.test(o.meterWidth), o.meterWidth);
  ok("the chart still has points to draw", o.points === 2, o.points);
  ok("evicted / archive tiles unaffected",
     o.tiles["t-evict"] === "9,400" && o.tiles["t-arch"] === "4.0 KB",
     [o.tiles["t-evict"], o.tiles["t-arch"]]);
}

console.log("\n[B] blank window AND no trigger (nothing to measure against)");
{
  const o = drive(H + "\n2026-08-22T10:00:05Z,request,,41200,,,,,,claude-x,,\n", "  rendered:");
  const all = Object.values(o.tiles).join(" | ");
  ok("still no NaN anywhere", !/NaN/.test(all), all);
  ok("the page SAYS there is no reference rather than drawing one",
     o.tiles["t-state"] === "no trigger or window recorded", o.tiles["t-state"]);
  ok("the meter is empty and unstyled, not red at 0%",
     o.meterWidth === "0%" && o.meterClass === "meter", [o.meterWidth, o.meterClass]);
  ok("% of window is an em dash", o.tiles["t-pct"] === "—", o.tiles["t-pct"]);
}

console.log("\n[C] window IS logged (the other branch still works)");
{
  const o = drive(H + "\n2026-08-22T10:00:05Z,request,,50000,,,,,,claude-x,200000,\n", "  rendered:");
  ok("% of window is computed", o.tiles["t-pct"] === "25.0%", o.tiles["t-pct"]);
  ok("footer names the window", /200,000-token window/.test(o.tiles["t-win"]), o.tiles["t-win"]);
}

console.log("\n[D] a window of 0 or junk must not become a division");
for (const w of ["0", "unknown", "-1"]) {
  const o = drive(H + "\n2026-08-22T10:00:05Z,request,,50000,,,,,,claude-x," + w + ",\n", "  window=" + JSON.stringify(w) + ":");
  ok("window=" + JSON.stringify(w) + " -> em dash, no NaN/Infinity/negative %",
     o.tiles["t-pct"] === "—" && o.tiles["t-win"] === "window not logged" &&
     !/NaN|Infinity|-\d/.test(Object.values(o.tiles).join(" ")),
     [o.tiles["t-pct"], o.tiles["t-win"]]);
}

console.log("\n" + (fail ? "FAILED" : "PASSED") + ": " + pass + " passed, " + fail + " failed\n");
process.exit(fail ? 1 : 0);
