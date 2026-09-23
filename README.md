# Inference Observability on OpenShift AI

Companion repo for the blog series on observability for AI inference on Red Hat OpenShift AI. Everything here runs on a real cluster. Nothing is simulated.

## What's in here

```
manifests/
  01-jaeger-all-in-one.yaml        Jaeger deployment, service, and route
  02-vllm-servingruntime-with-tracing.yaml   ServingRuntime + InferenceService with tracing enabled
  03-inferenceservice-tracing-fragment.yaml   Fragment to add tracing to an existing InferenceService

loadgen/
  loadgen.py                       Send chat requests, record latencies, print trace IDs
  join_traces.py                   Join loadgen CSV to vLLM span attributes from Jaeger
```

## Quick start (Blog 1)

You need a model served with vLLM on OpenShift AI. These steps add tracing and a place to send traces.

**1. Deploy a trace collector**

```bash
oc apply -f manifests/01-jaeger-all-in-one.yaml
```

Jaeger all-in-one accepts OpenTelemetry data on port 4317 (gRPC) and serves a UI on port 16686. The Route gives you browser access.

**2. Add the tracing argument to your model deployment**

If you're deploying fresh, use `02-vllm-servingruntime-with-tracing.yaml` as a starting point. The two things that matter are:

```yaml
args:
  - --otlp-traces-endpoint=http://jaeger.<your-namespace>.svc.cluster.local:4317
env:
  - name: OTEL_SERVICE_NAME
    value: vllm-server
```

If you already have a model deployed, see `03-inferenceservice-tracing-fragment.yaml` for a patch example.

**3. Send traffic and look at traces**

```bash
cd loadgen

python3 loadgen.py \
  --url "https://<your-model-route>/v1/chat/completions" \
  --requests 8 \
  --concurrency 4 \
  --heavy-index 1 \
  --max-tokens 30 \
  --heavy-max-tokens 200 \
  --seed 42 \
  --insecure
```

The load generator prints a trace ID next to every request. It sends a W3C `traceparent` header, and vLLM files its span under the same trace ID. Paste the slowest one into Jaeger.

**4. Join client latency to server-side breakdown**

```bash
python3 join_traces.py results.csv \
  --jaeger-url "https://<your-jaeger-route>" \
  --top 10 \
  --save-dir traces/ \
  --insecure
```

This prints a table with the client's total latency next to vLLM's queue time, prefill time, and decode time for each request.

## Requirements

- Red Hat OpenShift AI with a vLLM-based model deployment
- Python 3.9+ (standard library only, no pip installs)
- `oc` CLI authenticated to your cluster

## Blog series

1. **Where did that request go?** One slow request, one argument, one trace.
2. Tracing the inference path from gateway to GPU (coming soon)
3. What your GPUs are actually doing (coming soon)
4. Talk to your platform: AI-native ops with MCP (coming soon)
