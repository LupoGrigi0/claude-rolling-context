/* Run the ferry-metrics.html parser under node, without a browser.
 *
 *   node tools/check-parser.mjs
 *
 * It slices the FM-CORE block straight out of ferry-metrics.html and evaluates
 * it, so what is asserted here is byte-for-byte what the page ships. If someone
 * edits the parser in the HTML, this file tests the edit — that is the point of
 * the markers.  stdlib only (node's vm + fs).
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "ferry-metrics.html"), "utf8");

const BEGIN = "FM-CORE-BEGIN";
const END = "FM-CORE-END";
const a = html.indexOf(BEGIN);
const b = html.indexOf(END);
if (a < 0 || b < 0 || b < a) {
  console.error("FAIL: could not find the FM-CORE markers in ferry-metrics.html");
  process.exit(1);
}
const core = html.slice(html.indexOf("\n", a) + 1, html.lastIndexOf("\n", b));
// NB: a bare /\bwindow\b/ guard is WRONG here — "window" is one of the twelve
// contract columns, so the pure block legitimately says window all over. Only a
// *dereference* of the browser global counts.
for (const bad of [/\bdocument\s*\./, /(?<![.\w])window\s*\./, /\blocalStorage\b/, /\bfetch\s*\(/]) {
  if (bad.test(core)) {
    console.error("FAIL: FM-CORE touches the DOM (" + bad + ") — it must stay pure so it can be tested here.");
    process.exit(1);
  }
}
const FM = vm.runInNewContext(core + "\n;FM;", { Date, Math, Number, isFinite, String, RegExp, JSON });

let pass = 0, fail = 0;
const ok = (name, cond, extra) => {
  if (cond) { pass++; console.log("  ok   " + name); }
  else { fail++; console.log("  FAIL " + name + (extra === undefined ? "" : "  -> " + JSON.stringify(extra))); }
};
const eq = (name, got, want) => ok(name + "  (" + JSON.stringify(got) + ")", Object.is(got, want), { got, want });

const H = FM.HEADER.join(",");

console.log("\n[1] header contract");
eq("header is the exact contract line", H,
  "ts_iso,event,cycle,context_tokens,tokens_in,tokens_evicted,archive_lines,archive_bytes,carry_chars,model,window,note");

console.log("\n[2] degenerate inputs never throw");
for (const [name, text] of [
  ["empty string", ""],
  ["whitespace only", "\n\n  \n"],
  ["header only", H + "\n"],
  ["header, no trailing newline", H],
  ["garbage", "not a csv at all\nreally not\n"],
  ["null-ish", undefined],
]) {
  let state = "(threw)";
  try { state = FM.parseMetrics(text).state; } catch (e) { fail++; }
  ok(`${name} -> ${state}`, state !== "(threw)");
}
eq("empty -> empty", FM.parseMetrics("").state, "empty");
eq("header only -> headeronly", FM.parseMetrics(H + "\n").state, "headeronly");
eq("no header -> noheader", FM.parseMetrics("a,b,c\n1,2,3\n").state, "noheader");
ok("shape() on an empty parse returns a usable model",
  FM.shape(FM.parseMetrics("")).requests.length === 0);

console.log("\n[3] blank fields are EMPTY, never 0");
{
  const csv = H + "\n" +
    "2026-08-04T10:00:00Z,request,,,,,,,,claude-x,200000,\n" +
    "2026-08-04T10:00:01Z,curation,1,,,0,,,,,,zero really means zero here\n";
  const p = FM.parseMetrics(csv);
  eq("blank context_tokens -> null", p.rows[0].ctx, null);
  eq("blank tokens_in -> null", p.rows[0].tin, null);
  eq("blank cycle -> null", p.rows[0].cycle, null);
  eq("literal 0 stays 0", p.rows[1].evicted, 0);
  eq("num('') is null", FM.num(""), null);
  eq("num('  ') is null", FM.num("   "), null);
  eq("num('0') is 0", FM.num("0"), 0);
  eq("num('abc') is null", FM.num("abc"), null);
  eq("num('12abc') is null", FM.num("12abc"), null);
  eq("num('-1200') is -1200", FM.num("-1200"), -1200);
}

console.log("\n[4] quoting: commas and quotes inside note");
{
  const csv = H + "\n" +
    '2026-08-04T10:00:00Z,curation,1,,,12400,,,,,,"archived 6 turns -> archive_20260804.jsonl#L1-L6, blobs: 2"\n' +
    '2026-08-04T10:00:05Z,error,,,,,,,,,,"write failed: ""disk full"", retrying"\n';
  const p = FM.parseMetrics(csv);
  eq("row count", p.rows.length, 2);
  eq("comma inside a quoted note survives", p.rows[0].note,
    "archived 6 turns -> archive_20260804.jsonl#L1-L6, blobs: 2");
  eq("doubled quotes unescape", p.rows[1].note, 'write failed: "disk full", retrying');
  eq("the quoted comma did not shift the columns", p.rows[0].evicted, 12400);
}

console.log("\n[5] a torn final line (partial write) is dropped, not parsed");
{
  const good = H + "\n" +
    "2026-08-04T10:00:00Z,request,,41200,,,,,,claude-x,200000,\n" +
    "2026-08-04T10:00:30Z,request,,52900,11700,,,,,claude-x,200000,\n";
  const torn = good + "2026-08-04T10:01:00Z,request,,63";     // cut mid-row
  const p = FM.parseMetrics(torn);
  eq("torn flagged", p.torn, true);
  eq("only the two whole rows survive", p.rows.length, 2);
  eq("no phantom 63-token request", p.rows[p.rows.length - 1].ctx, 52900);

  const tornQuote = good + '2026-08-04T10:01:00Z,curation,3,,,9100,,,,,,"archiving turns 12';
  const q = FM.parseMetrics(tornQuote);
  eq("torn inside an open quote is flagged", q.torn, true);
  eq("torn inside an open quote drops the record", q.rows.length, 2);

  const noNewline = good.slice(0, -1);                       // complete row, just no \n yet
  const n = FM.parseMetrics(noNewline);
  eq("a complete final row without a trailing newline is KEPT", n.rows.length, 2);
  eq("...and is not flagged torn", n.torn, false);
}

console.log("\n[6] a malformed middle row is skipped, the rest still parses");
{
  const csv = H + "\n" +
    "2026-08-04T10:00:00Z,request,,41200,,,,,,claude-x,200000,\n" +
    "oops,short,row\n" +
    "2026-08-04T10:00:30Z,request,,52900,11700,,,,,claude-x,200000,\n";
  const p = FM.parseMetrics(csv);
  eq("skipped count", p.skipped, 1);
  eq("surviving rows", p.rows.length, 2);
}

console.log("\n[7] CRLF, BOM and a blank line in the middle");
{
  const csv = "﻿" + H + "\r\n" +
    "2026-08-04T10:00:00Z,request,,41200,,,,,,claude-x,200000,\r\n" +
    "\r\n" +
    "2026-08-04T10:00:30Z,request,,52900,11700,,,,,claude-x,200000,\r\n";
  const p = FM.parseMetrics(csv);
  eq("BOM does not break the header", p.state, "ok");
  eq("CRLF rows parse", p.rows.length, 2);
  eq("blank line skipped without counting as malformed", p.skipped, 0);
  eq("value after CRLF is clean", p.rows[1].tin, 11700);
}

// Sections 8-9 need a real metrics.csv; 10-13 do not. Skipping 8-9 must not
// skip everything after them — it used to exit here, which quietly took the
// self-contained sections down with it.
const SAMPLE = process.env.FERRY_SAMPLE_CSV;
if (!SAMPLE) {
  console.error("\nset FERRY_SAMPLE_CSV=<path to a metrics.csv> to run sections 8-9");
  console.error("(tools/make-sample-metrics.py writes one) — running the rest\n");
}

if (SAMPLE) {
console.log("\n[8] growing file: every prefix of a real log parses");
{
  const full = readFileSync(SAMPLE, "utf8");
  let worst = null;
  for (let i = 0; i <= full.length; i++) {
    try {
      const p = FM.parseMetrics(full.slice(0, i));
      FM.shape(p);
    } catch (e) { worst = { i, e: String(e) }; break; }
  }
  ok("no prefix of the sample log throws", worst === null, worst);
  const p = FM.parseMetrics(full);
  const m = FM.shape(p);
  console.log("      sample: " + p.rows.length + " rows, " + m.requests.length + " requests, " +
    m.curations.length + " curations, " + m.archives.length + " archive_writes, torn=" + p.torn);
}

// These hold for ANY metrics.csv, not just the bundled sample, so this section
// can be pointed at a real run. (An earlier version asserted `xmode === "time"`
// and "failed" on genuine collector output whose 37 rows all landed inside the
// same second — the assertion was wrong, not the page.)
console.log("\n[9] shaping invariants on " + SAMPLE);
{
  const full = readFileSync(SAMPLE, "utf8");
  const p = FM.parseMetrics(full);
  const m = FM.shape(p);
  const distinctTs = new Set(p.rows.filter(r => r.t !== null).map(r => r.t)).size;
  eq("x mode follows the number of distinct timestamps (" + distinctTs + " distinct)",
    m.xmode, distinctTs >= 2 ? "time" : "index");
  ok("cycle count = max cycle number = number of curation rows (" + m.stats.cycles + ")",
    m.stats.cycles === m.curations.length &&
    m.stats.cycles === Math.max(0, ...m.curations.map(r => r.cycle ?? 0)));
  ok("total evicted is the sum of the cycles (" + m.stats.evicted + ")",
    m.stats.evicted === m.curations.reduce((a, r) => a + (r.evicted ?? 0), 0) && m.stats.evicted > 0);
  ok("latest context_tokens picked up (" + m.stats.ctx + ")", m.stats.ctx !== null);
  // `window` is blank unless FERRY_WINDOW was exported — nothing in the API
  // reports it — so a REAL collector log usually has none. Assert the
  // RELATIONSHIP, which holds either way; asserting its presence made this
  // section fail on truthful output.
  ok("window is either blank or a positive number (" + m.stats.window + ")",
    m.stats.window === null || m.stats.window > 0);
  ok("% of window agrees with the window, and is null when there is none",
    m.stats.window === null
      ? m.stats.pct === null
      : Math.abs(m.stats.pct - m.stats.ctx / m.stats.window) < 1e-9);
  ok("archive bytes are the LAST value, not the max-so-far (" + m.stats.abytes + ")",
    m.stats.abytes === m.archives[m.archives.length - 1].abytes);
  ok("archive bytes grow monotonically",
    m.archives.every((r, i, all) => i === 0 || r.abytes >= all[i - 1].abytes));
  const note = (m.starts[0] || {}).note || "";
  ok("trigger sniffed from the proxy_start note (" + m.trigger + ")",
    m.trigger === Number((/trigger[\s=:]+(\d+)/i.exec(note) || [])[1]));
  ok("target sniffed from the proxy_start note (" + m.target + ")",
    m.target === Number((/target[\s=:]+(\d+)/i.exec(note) || [])[1]));
  ok("mode sniffed (" + m.mode + ")", m.mode === "ferry");
  ok("every row got an x", m.rows.every(r => typeof r.x === "number" && isFinite(r.x)));
  ok("x is non-decreasing (append-only log)", m.rows.every((r, i, all) => i === 0 || r.x >= all[i - 1].x));
  // Shape-dependent: a real log need not contain a sawtooth, an error or a
  // restart, and the checker must not call a truthful log broken. Each is
  // asserted when the shape is present and reported as n/a when it is not.
  const pairs = m.curations.map(c => [
    m.requests.filter(r => r.x <= c.x && r.ctx !== null).pop(),
    m.requests.filter(r => r.x > c.x && r.ctx !== null)[0]
  ]).filter(([b, a]) => b && a && b.ctx !== a.ctx);
  if (pairs.length) {
    ok("the sawtooth is real: context drops after each curation (" + pairs.length + " measurable)",
      pairs.every(([b, a]) => a.ctx < b.ctx));
  } else {
    console.log("  n/a  sawtooth: this log has no curation with differing context on both sides");
  }
  if (m.errors.length) ok("errors surfaced (" + m.stats.errCount + ")", m.stats.errCount === m.errors.length);
  else console.log("  n/a  errors: this log has none (a clean run is allowed)");
  if (m.fetches.length) ok("fetch events kept (" + m.fetches.length + ")", m.fetches.length >= 1);
  else console.log("  n/a  fetch: this log has none");
  if (m.restarts.length) ok("restart event kept", m.restarts.length >= 1);
  else console.log("  n/a  restart: this log has none (a first boot is allowed)");
  ok("carry_chars picked up (" + m.stats.carry + ")", m.stats.carry !== null);
}
}   // end: sections 8-9 (sample-dependent)

console.log("\n[10] index fallback when timestamps are unreadable");
{
  const csv = H + "\n" +
    "not-a-date,request,,41200,,,,,,m,200000,\n" +
    "also-not,request,,52900,11700,,,,,m,200000,\n";
  const p = FM.parseMetrics(csv);
  const m = FM.shape(p);
  eq("badTs counted", p.badTs, 2);
  eq("falls back to event ordinal", m.xmode, "index");
  eq("x is the ordinal", m.rows[1].x, 1);
  eq("stats still work", m.stats.ctx, 52900);
}

console.log("\n[11] downsample keeps the peaks (the sawtooth is the story)");
{
  const pts = [];
  for (let i = 0; i < 5000; i++) pts.push({ x: i, i, y: (i % 100) * 1000 });   // 50 sawteeth
  const d = FM.downsample(pts, 400);
  ok("shrinks (" + pts.length + " -> " + d.length + ")", d.length < pts.length && d.length > 0);
  ok("keeps x order", d.every((p, i, all) => i === 0 || p.x >= all[i - 1].x));
  ok("keeps the global max", d.some(p => p.y === 99000));
  ok("keeps the global min", d.some(p => p.y === 0));
  ok("keeps first and last", d[0].i === 0 && d[d.length - 1].i === 4999);
  ok("short input passes through untouched", FM.downsample(pts.slice(0, 10), 400).length === 10);
}

console.log("\n[12] formatters");
eq("fmtInt", FM.fmtInt(1234567), "1,234,567");
eq("fmtInt(null)", FM.fmtInt(null), "—");
eq("fmtSigned +", FM.fmtSigned(11700), "+11,700");
eq("fmtSigned -", FM.fmtSigned(-9100), "-9,100");
eq("fmtCompact 12900", FM.fmtCompact(12900), "12.9K");
eq("fmtCompact 9999", FM.fmtCompact(9999), "9,999");
eq("fmtBytes B", FM.fmtBytes(512), "512 B");
eq("fmtBytes KB", FM.fmtBytes(1536), "1.5 KB");
eq("fmtBytes MB", FM.fmtBytes(5 * 1048576), "5.00 MB");
eq("fmtBytes(null)", FM.fmtBytes(null), "—");
eq("fmtAge s", FM.fmtAge(4200), "4s ago");
eq("fmtAge m", FM.fmtAge(125000), "2m 5s ago");
eq("fmtAge(null)", FM.fmtAge(null), "—");
ok("niceTicks are round and inside the domain", (() => {
  const t = FM.niceTicks(0, 137000, 3);
  return t.length >= 2 && t[0] >= 0 && t[t.length - 1] <= 137000 && t.every(v => v % 1000 === 0);
})());
ok("niceTicks survives a degenerate domain", FM.niceTicks(0, 0, 3).length >= 1);

/* The model's context window is nowhere in the request or the response — the
 * client never sends it and the API never returns it — so `window` is BLANK
 * unless FERRY_WINDOW was exported. That is deliberate: a guessed window makes
 * every "% of window" number fiction. What must not happen is the page turning
 * that blank into NaN%, 0%, or a chart that silently stops drawing. */
console.log("\n[13] a blank window degrades, it does not lie");
{
  const csv = H + "\n" +
    "2026-08-22T10:00:00Z,proxy_start,,,,,,,,,,mode=ferry trigger=120000 target=60000\n" +
    "2026-08-22T10:00:05Z,request,,41200,,,,,,claude-x,,\n" +
    "2026-08-22T10:01:05Z,request,,52900,11700,,,,,claude-x,,\n" +
    "2026-08-22T10:02:05Z,curation,1,52900,,9400,,,120,,,estimate:chars/4 turns=4\n" +
    "2026-08-22T10:02:05Z,archive_write,1,,,,12,4096,,,,archive_20260822.jsonl#L1-L12\n";
  const p = FM.parseMetrics(csv);
  const m = FM.shape(p);
  eq("parses cleanly", p.state, "ok");
  eq("skipped nothing", p.skipped, 0);
  eq("window stays null (blank is not zero)", m.stats.window, null);
  eq("pct is null, not NaN and not 0", m.stats.pct, null);
  ok("pct is not NaN", !Number.isNaN(m.stats.pct));
  eq("every row's window is null", m.rows.every(r => r.window === null), true);
  ok("the rest of the model is unaffected by the missing window",
    m.stats.ctx === 52900 && m.stats.cycles === 1 && m.stats.evicted === 9400 &&
    m.stats.abytes === 4096 && m.stats.alines === 12 && m.stats.carry === 120);
  ok("the context series still has points to draw",
    m.requests.length === 2 && m.requests.every(r => r.ctx !== null && isFinite(r.x)));
  ok("the trigger (which IS logged) still gives the meter a reference",
    m.trigger === 120000);
  // Same arithmetic fillTiles does. With no window and no trigger there is no
  // reference at all, and the page must say so rather than divide by nothing.
  {
    const noRef = FM.shape(FM.parseMetrics(H + "\n2026-08-22T10:00:05Z,request,,41200,,,,,,claude-x,,\n"));
    const ref = noRef.trigger || noRef.stats.window || null;
    eq("no trigger and no window -> no reference (null, not 0)", ref, null);
    eq("meter fraction is null, never NaN",
      (noRef.stats.ctx !== null && ref) ? noRef.stats.ctx / ref : null, null);
  }
  // And a window that IS logged still works, so the blank path is not the only path.
  {
    const withWin = FM.shape(FM.parseMetrics(H + "\n2026-08-22T10:00:05Z,request,,41200,,,,,,claude-x,200000,\n"));
    eq("a logged window is still honoured", withWin.stats.window, 200000);
    ok("% of window computed when it is known",
      Math.abs(withWin.stats.pct - 41200 / 200000) < 1e-9);
  }
  // A window is a positive token count or it is not a window. FERRY_WINDOW is
  // typed by a human, so "0", "-1" and "unknown" all reach the page; each of
  // them means "we were not told", and none of them may become a divisor.
  // (window=-1 used to render "% of window: -5000000.0%".)
  for (const [w, why] of [["unknown", "non-numeric"], ["0", "zero"],
                          ["-1", "negative"], ["-200000", "very negative"],
                          ["", "blank"]]) {
    const s = FM.shape(FM.parseMetrics(H +
      "\n2026-08-22T10:00:05Z,request,,41200,,,,,,claude-x," + w + ",\n")).stats;
    eq("a " + why + " window is not a window (" + JSON.stringify(w) + ")", s.window, null);
    eq("...and pct stays null, not a fiction", s.pct, null);
  }
  eq("posNum keeps a real window", FM.posNum("200000"), 200000);
  eq("posNum rejects 0", FM.posNum("0"), null);
  eq("posNum rejects a negative", FM.posNum("-1"), null);
}

console.log("\n" + (fail ? "FAILED" : "PASSED") + ": " + pass + " assertions passed, " + fail + " failed\n");
process.exit(fail ? 1 : 0);
