# Ferry live metrics

    python3 tools/ferry-metrics-serve.py --data $FERRY_DATA --port 5610          # loopback
    python3 tools/ferry-metrics-serve.py --data $FERRY_DATA --port 5610 --bind 100.x.y.z   # tailnet

It prints the URL it chose, serves `ferry-metrics.html` at `/` and `$FERRY_DATA/metrics.csv`
at `/metrics.csv`, and **refuses to bind `0.0.0.0`** — this is a mind's private telemetry.
The page re-fetches the CSV every 2s and redraws; phone-sized and dark-mode aware.

The four panels share one x axis, so a drop in one lines up with a spike in the next:

| Panel | Reads as |
|---|---|
| **Context fill** | the sawtooth — real input tokens climbing per request, falling at each curation. A labelled magenta rule marks every cycle (`cycle 3: -12,400 tok`); the hairlines are TRIGGER and TARGET, sniffed from the `proxy_start` note. |
| **Tokens in** | per-request delta. Blue = context grew; magenta = it shrank (the request right after a curation). |
| **Tokens evicted** | tokens moved to disk per cycle (a `chars/4` estimate — see the row's note). |
| **Archive growth** | total `archive_*.jsonl` bytes: monotonic, because nothing is ever summarised away. This is where the tokens went. |

Blank CSV cells are **unmeasured, never zero**, and are drawn as gaps rather than as 0.
A half-written final line (the collector appending mid-read) is dropped and announced in
the banner. Empty, header-only and unparseable files each get a message, never a blank page.
`Table view` at the bottom is the WCAG-clean twin: every logged row, unrounded.

### `window` is blank unless you say otherwise

Nothing in the API reports the model's context window — the client never sends it and the
response never returns it — so the `window` column is **empty** unless `FERRY_WINDOW` is
exported. That is deliberate: a guessed window would make every "% of window" figure
fiction. With it blank the page says `window not logged`, shows `—` for the percentage,
and measures the fill meter against the **trigger** instead (the number that actually
decides whether curation fires). `0`, a negative and any non-numeric `window` are all
treated as "not told", never as a divisor.

    node tools/check-parser.mjs                # assertions against the page's own parser
    FERRY_SAMPLE_CSV=… node tools/check-parser.mjs      # plus shaping, on a real metrics.csv
    node tools/check-tiles.mjs                 # drives the page's real fillTiles() (DOM half)
    python3 tools/make-sample-metrics.py /tmp/x         # writes a 3-cycle sample to test with

`check-parser.mjs` slices the `FM-CORE` block straight out of the HTML and runs it under
node, so it tests the parser the page actually ships — keep those two marker comments.
`check-tiles.mjs` slices `fillTiles()` out the same way and renders it against a stub DOM,
so the tiles a human reads are asserted too, not just the numbers behind them.
