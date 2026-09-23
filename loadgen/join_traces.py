#!/usr/bin/env python3
"""Join a loadgen CSV to the vLLM spans for the same requests, by trace ID, and print one table.

The client knows how long each request took. vLLM's `llm_request` span knows where that time went
(queue, prefill, decode). The trace ID that loadgen.py sent in `traceparent` ties the two together.

Reads spans from the Jaeger query API (also what the Tempo operator's Jaeger UI serves), or from a saved JSON dump.

  # live, one API call per trace ID
  python3 join_traces.py loadgen-with-tracing.csv --jaeger-url https://<jaeger-ui-route> --top 10 --save-dir traces/

  # from a dump:  curl -s "$JAEGER/api/traces?service=vllm-server&limit=500&lookback=1h" > dump.json
  python3 join_traces.py loadgen-with-tracing.csv --json dump.json

Standard library only. Prints nothing it did not read from the two sources.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import sys
import urllib.request

LAT = "gen_ai.latency."


def fetch(url: str, token: str | None, insecure: bool) -> dict:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    ctx = None
    if url.startswith("https"):
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="CSV written by loadgen.py")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--jaeger-url", help="base URL of a Jaeger query API, e.g. https://tempo-demo-jaegerui-<ns>.apps...")
    src.add_argument("--json", help="a saved response from <jaeger>/api/traces?...")
    ap.add_argument("--token", default=os.environ.get("JAEGER_TOKEN"), help="bearer token if the Route needs one (env JAEGER_TOKEN). Never written anywhere.")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification for a self-signed Route")
    ap.add_argument("--span-name", default="llm_request", help="the vLLM span to read (default llm_request)")
    ap.add_argument("--top", type=int, default=0, help="only the N slowest requests (default: all)")
    ap.add_argument("--save-dir", help="save each fetched trace as <dir>/trace-<idx>.json")
    ap.add_argument("--markdown", action="store_true", help="print a Markdown table instead of fixed width")
    args = ap.parse_args()

    rows = [r for r in csv.DictReader(open(args.csv)) if r["trace_id"] and not r["error"]]
    rows.sort(key=lambda r: -float(r["total_s"]))
    if args.top:
        rows = rows[: args.top]

    traces: dict[str, dict] = {}
    if args.json:
        for t in json.load(open(args.json)).get("data", []):
            traces[t["traceID"]] = t
    else:
        if args.save_dir:
            os.makedirs(args.save_dir, exist_ok=True)
        for r in rows:
            try:
                d = fetch(f"{args.jaeger_url.rstrip('/')}/api/traces/{r['trace_id']}", args.token, args.insecure)
            except Exception as e:  # a missing trace is a result, not a crash
                print(f"  idx {r['idx']}: no trace fetched ({type(e).__name__}: {e})", file=sys.stderr)
                continue
            for t in d.get("data") or []:
                traces[t["traceID"]] = t
                if args.save_dir:
                    json.dump(t, open(os.path.join(args.save_dir, f"trace-{r['idx']}.json"), "w"), indent=1)

    head = ["idx", "kind", "client_s", "e2e_s", "outside_vllm_s", "queue_s", "prefill_s", "decode_s", "ttft_s", "in_tok", "out_tok", "spans_in_trace", "services"]
    table, missing = [], []
    for r in rows:
        t = traces.get(r["trace_id"])
        if not t:
            missing.append(r["idx"])
            continue
        span = next((s for s in t["spans"] if s["operationName"] == args.span_name), None)
        if not span:
            missing.append(r["idx"])
            continue
        a = {x["key"]: x["value"] for x in span["tags"]}
        g = lambda k: float(a.get(LAT + k, "nan"))
        services = sorted({p["serviceName"] for p in t["processes"].values()})
        client = float(r["total_s"])
        table.append([r["idx"], r["kind"], f"{client:.2f}", f"{g('e2e'):.2f}", f"{client - g('e2e'):.2f}",
                      f"{g('time_in_queue'):.2f}", f"{g('time_in_model_prefill'):.2f}", f"{g('time_in_model_decode'):.2f}",
                      f"{g('time_to_first_token'):.2f}", str(a.get("gen_ai.usage.prompt_tokens", "")),
                      str(a.get("gen_ai.usage.completion_tokens", "")), str(len(t["spans"])), ",".join(services)])

    print(f"requests in CSV (ok, with trace ID): {len(rows)}   matched to a '{args.span_name}' span: {len(table)}   no span found: {len(missing)} {missing if missing else ''}")
    if args.markdown:
        print("| " + " | ".join(head) + " |")
        print("|" + "---|" * len(head))
        for row in table:
            print("| " + " | ".join(row) + " |")
    else:
        widths = [max(len(head[i]), *(len(row[i]) for row in table)) if table else len(head[i]) for i in range(len(head))]
        print("  ".join(h.rjust(w) for h, w in zip(head, widths)))
        for row in table:
            print("  ".join(c.rjust(w) for c, w in zip(row, widths)))
    if table:
        q = sum(float(row[5]) for row in table)
        print(f"\nsum of time in queue over these spans: {q:.2f} s   (compare with the increase in vllm:request_queue_time_seconds_sum on /metrics)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
