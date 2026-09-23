#!/usr/bin/env python3
"""Send chat completion requests to an OpenAI-compatible endpoint and record what each one cost in time.

Standard library only, so it runs on any laptop or in a UBI Python pod without installs.

What it is for: the opening scene of the observability series. Send a lot of ordinary
requests, plus a few heavy ones, find the slow request in the CSV, and look up its
trace ID in Tempo. Every request carries its own W3C `traceparent` header, so if the
server joins incoming trace context the trace ID in the CSV is the trace ID in Tempo.

Examples:
  # 200 requests, 8 at a time, 3 heavy ones mixed in
  python3 loadgen.py --url $LLM_URL --model $LLM_MODEL --key $LLM_KEY -n 200 -c 8 --heavy 3

  # rehearsal against Ollama on a laptop
  python3 loadgen.py --url http://localhost:11434/v1 --model llama3.2:latest -n 20 -c 4 --heavy 1

Nothing here invents data: every number in the CSV is measured on the client side.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

ORDINARY_PROMPTS = [
    "What's the status of order 4521?",
    "Summarize the return policy in two sentences.",
    "Write a one-line shipping update for a customer.",
    "What does HTTP status 429 mean?",
    "Give me three names for a support chatbot.",
    "Translate 'your order has shipped' into Spanish.",
    "Explain what a KV cache is in one sentence.",
    "Is a tomato a fruit? Answer in one sentence.",
]

# A heavy request is a long prompt plus a long answer. It is an honest way to make one
# request slow: more prefill, more decode, more KV cache. No sleep() anywhere.
HEAVY_PARAGRAPH = (
    "The warehouse received the shipment on Monday and logged every pallet by hand, "
    "then the carrier updated the manifest twice before the truck left the dock. "
)


def new_traceparent() -> tuple[str, str]:
    """W3C Trace Context: version-traceid-parentid-flags. Flags 01 = sampled."""
    trace_id = secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    return trace_id, f"00-{trace_id}-{span_id}-01"


def build_body(model: str, heavy: bool, heavy_repeat: int, max_tokens: int, heavy_max_tokens: int) -> dict:
    if heavy:
        prompt = (
            HEAVY_PARAGRAPH * heavy_repeat
            + "\n\nRewrite everything above as a detailed incident report with a timeline, "
            "a root cause section and ten numbered follow-up actions."
        )
        limit = heavy_max_tokens
    else:
        prompt = random.choice(ORDINARY_PROMPTS)
        limit = max_tokens
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": limit,
        "temperature": 0.2,
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def one_request(idx: int, args, heavy: bool, ctx: ssl.SSLContext | None) -> dict:
    trace_id, traceparent = new_traceparent()
    body = build_body(args.model, heavy, args.heavy_repeat, args.max_tokens, args.heavy_max_tokens)
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if args.key:
        headers["Authorization"] = f"Bearer {args.key}"
    if not args.no_traceparent:
        headers["traceparent"] = traceparent
    req = urllib.request.Request(
        args.url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    row = {
        "idx": idx,
        "kind": "heavy" if heavy else "ordinary",
        "trace_id": "" if args.no_traceparent else trace_id,
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "status": "",
        "ttft_s": "",
        "total_s": "",
        "prompt_tokens": "",
        "completion_tokens": "",
        "chunks": 0,
        "error": "",
    }
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=args.timeout, context=ctx) as resp:
            row["status"] = resp.status
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if (delta.get("content") or delta.get("reasoning_content")) and row["ttft_s"] == "":
                        row["ttft_s"] = round(time.perf_counter() - t0, 4)
                    row["chunks"] += 1
                usage = event.get("usage")
                if usage:
                    row["prompt_tokens"] = usage.get("prompt_tokens", "")
                    row["completion_tokens"] = usage.get("completion_tokens", "")
    except urllib.error.HTTPError as e:
        row["status"] = e.code
        row["error"] = e.read(300).decode("utf-8", "replace").replace("\n", " ")
    except Exception as e:  # timeouts, resets, TLS
        row["error"] = f"{type(e).__name__}: {e}"
    row["total_s"] = round(time.perf_counter() - t0, 4)
    # A 200 whose stream carried no content is a failure (the server died or dropped the stream mid-request).
    if not row["error"] and str(row["status"]) == "200" and row["ttft_s"] == "":
        row["error"] = "empty stream: HTTP 200 but no content chunk arrived"
    return row


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("LLM_URL"), help="base URL ending in /v1 (env LLM_URL)")
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL"), help="model name (env LLM_MODEL)")
    ap.add_argument("--key", default=os.environ.get("LLM_KEY", ""), help="bearer token (env LLM_KEY). Never written to any output file.")
    ap.add_argument("-n", "--requests", type=int, default=100)
    ap.add_argument("-c", "--concurrency", type=int, default=8)
    ap.add_argument("--heavy", type=int, default=1, help="how many heavy requests to mix in at random positions")
    ap.add_argument("--heavy-repeat", type=int, default=60, help="how many times the filler paragraph repeats in a heavy prompt")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--heavy-max-tokens", type=int, default=1500)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--seed", type=int, default=None, help="fix the positions of heavy requests and prompt choice")
    ap.add_argument("--no-traceparent", action="store_true", help="do not send a traceparent header")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed cluster routes)")
    ap.add_argument("-o", "--out", default=None, help="CSV path (default loadgen-<utc timestamp>.csv)")
    args = ap.parse_args()
    if not args.url or not args.model:
        ap.error("--url and --model are required (or set LLM_URL and LLM_MODEL)")
    if args.seed is not None:
        random.seed(args.seed)

    ctx = None
    if args.url.startswith("https"):
        ctx = ssl.create_default_context()
        if args.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

    heavy_at = set(random.sample(range(args.requests), min(args.heavy, args.requests)))
    out = args.out or f"loadgen-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
    rows: list[dict] = []
    lock = threading.Lock()
    done = 0
    wall0 = time.perf_counter()
    print(f"target={args.url} model={args.model} requests={args.requests} concurrency={args.concurrency} heavy={len(heavy_at)}")
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(one_request, i, args, i in heavy_at, ctx) for i in range(args.requests)]
        for f in as_completed(futures):
            r = f.result()
            with lock:
                rows.append(r)
                done += 1
                if done % max(1, args.requests // 10) == 0 or r["error"]:
                    note = f" ERROR {r['error'][:80]}" if r["error"] else ""
                    print(f"  {done}/{args.requests} last={r['total_s']}s kind={r['kind']}{note}")
    wall = time.perf_counter() - wall0

    rows.sort(key=lambda r: r["idx"])
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    ok = [r for r in rows if not r["error"] and str(r["status"]) == "200"]
    totals = [float(r["total_s"]) for r in ok]
    ttfts = [float(r["ttft_s"]) for r in ok if r["ttft_s"] != ""]
    print()
    print(f"wrote {out}")
    print(f"ok={len(ok)}/{len(rows)} wall={wall:.1f}s")
    if totals:
        print(f"total latency  p50={percentile(totals, .5):.2f}s  p95={percentile(totals, .95):.2f}s  p99={percentile(totals, .99):.2f}s  max={max(totals):.2f}s")
    if ttfts:
        print(f"time to first token  p50={percentile(ttfts, .5):.2f}s  p95={percentile(ttfts, .95):.2f}s  max={max(ttfts):.2f}s")
    print()
    print("slowest five (look these trace IDs up in Tempo):")
    print(f"  {'idx':>5}  {'kind':8}  {'total_s':>8}  {'ttft_s':>7}  {'out_tok':>7}  trace_id")
    for r in sorted(ok, key=lambda r: -float(r["total_s"]))[:5]:
        print(f"  {r['idx']:>5}  {r['kind']:8}  {r['total_s']:>8}  {str(r['ttft_s']):>7}  {str(r['completion_tokens']):>7}  {r['trace_id']}")
    return 0 if len(ok) == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
