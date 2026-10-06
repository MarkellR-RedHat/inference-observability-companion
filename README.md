# Inference observability on OpenShift AI

Companion code for the blog series on observability for AI inference on Red Hat OpenShift AI. Every manifest here was applied to a cluster and every script was run against one. Nothing is simulated and no output is edited.

Blog 1, *Where did that request go?*, uses the files below to turn on OpenTelemetry tracing in vLLM with one argument, send fifty requests, and read where the time went inside the slowest one.

## Contents

```
manifests/
  01-jaeger-all-in-one.yaml                  Jaeger (collector, storage and UI in one pod), Service, Route
  02-inferenceservice-phi4-tracing.yaml      The model from Blog 1: Phi-4 on one GPU with tracing on
  03-inferenceservice-tracing-fragment.yaml  Add tracing to a model you already serve
  04-vllm-cpu-servingruntime-with-tracing.yaml  The same setup without a GPU (CPU runtime, for laptops and small clusters)

loadgen/
  loadgen.py        Sends chat requests with a W3C traceparent header, records client-side timings, prints the slowest five with their trace IDs
  join_traces.py    Joins a loadgen CSV to the vLLM llm_request spans by trace ID, so client total sits next to queue, prefill and decode
```

Both scripts are standard library only. Python 3.9 or newer, no `pip install`.

## Prerequisites

- Red Hat OpenShift AI 3.x with KServe model serving, and a project you can deploy into
- `oc` logged in to the cluster
- A vLLM ServingRuntime in your project. The ones the OpenShift AI dashboard creates work as they are. vLLM has bundled the OpenTelemetry libraries since v0.17.0, so there is nothing to install in the image.

## Walkthrough

### 1. Deploy Jaeger in your project

```bash
oc new-project obs-demo        # or any project you own
oc apply -n obs-demo -f manifests/01-jaeger-all-in-one.yaml
oc get route jaeger-ui -n obs-demo -o jsonpath='{.spec.host}{"\n"}'
```

Jaeger accepts OpenTelemetry data on port 4317 (gRPC) and 4318 (HTTP) and serves its UI on the Route. It is the quickest collector to stand up inside one project for a demo. On OpenShift AI 3.6 builds a NetworkPolicy allows only the monitoring namespace to push OTLP to the platform's own collector, which is why this path keeps everything inside your project.

### 2. Turn on tracing in vLLM

Tracing in vLLM is off until `--otlp-traces-endpoint` is set. The flag enables it. Environment variables alone never do. `OTEL_SERVICE_NAME` only names the service in the trace viewer.

**A model you already serve.** Add the argument and the variable to the InferenceService. The OpenShift AI dashboard's "Additional serving runtime arguments" and "Additional environment variables" fields write to the same two places.

```yaml
spec:
  predictor:
    model:
      args:
        - --otlp-traces-endpoint=http://jaeger:4317
      env:
        - name: OTEL_SERVICE_NAME
          value: vllm-server
```

`manifests/03-inferenceservice-tracing-fragment.yaml` has the same fragment with an `oc patch` command that appends to any args and env already there.

**The Blog 1 model.** `manifests/02-inferenceservice-phi4-tracing.yaml` is the InferenceService from the post: Phi-4 quantized to W4A16, one GPU, `--max-num-seqs=2` so that one GPU queues under eight concurrent clients. Set the three values marked at the top of the file (your ServingRuntime name, hardware profile name and its namespace), then:

```bash
oc apply -n obs-demo -f manifests/02-inferenceservice-phi4-tracing.yaml
oc get inferenceservice obs-demo-model -n obs-demo -w
```

**No GPU.** `manifests/04-vllm-cpu-servingruntime-with-tracing.yaml` is a ServingRuntime on the Red Hat vLLM CPU image plus a matching InferenceService. It is slow, and it is enough to see queueing and a real trace on a cluster with no accelerator.

**Check that tracing is on** once the pod is Running:

```bash
POD=$(oc get pod -n obs-demo -l serving.kserve.io/inferenceservice=obs-demo-model -o name | head -1)
oc logs -n obs-demo "$POD" -c kserve-container | grep -i -E 'otlp|tracing'
```

The log shows the OTLP endpoint vLLM is exporting to. If it says OpenTelemetry is not available, the image predates vLLM v0.17.0.

### 3. Send traffic

```bash
LLM_URL="$(oc get inferenceservice obs-demo-model -n obs-demo -o jsonpath='{.status.url}')/v1"
python3 loadgen/loadgen.py --url "$LLM_URL" --model obs-demo-model -n 50 -c 8 --heavy 2 -o loadgen-with-tracing.csv
```

Add `--insecure` if the Route uses a self-signed certificate, and `--key <token>` if the endpoint needs one. The token is never written to any output file.

Fifty chat requests, eight at a time. Forty-eight are short support-desk questions. Two, placed at random, are heavy on purpose: a long document to rewrite and a long answer to write. There is no `sleep()` anywhere. Every request carries its own W3C `traceparent` header with the sampled flag set, and vLLM files its span under that trace ID, so the ID printed next to each request is the ID you look up in Jaeger.

The run ends with the latency summary and the five slowest requests:

```
slowest five (look these trace IDs up in Jaeger):
    idx  kind       total_s   ttft_s  out_tok  trace_id
     40  heavy       6.1686    1.271      506  d72544d890b964705b3358913cb040af
      7  heavy       6.1135   0.6866      577  6a7c451a4c4d725ae8108688c2bb553e
      4  ordinary    4.3889   3.8366       64  3beb30aed2f791c925f1b2c963d52808
     49  ordinary    4.0581   3.4668       64  5f0c4f10ce5e6e5475bd78867141d9d4
     10  ordinary    4.0362   3.4291       64  ac52f09455f582effbfbece783022bf5
```

That block is the real output from the Blog 1 run (Phi-4 W4A16 on one H200). The CSV has every request.

### 4. Open the slowest trace

Paste a trace ID into the Jaeger UI. The trace holds one span, `llm_request`, and the timing is in its attributes:

| Attribute | What it is |
|---|---|
| `gen_ai.latency.time_in_queue` | waited for a batch slot |
| `gen_ai.latency.time_in_model_prefill` | reading the prompt |
| `gen_ai.latency.time_in_model_decode` | generating tokens |
| `gen_ai.latency.time_to_first_token` | queue plus prefill, roughly |
| `gen_ai.latency.e2e` | arrival to last token, inside vLLM |
| `gen_ai.usage.prompt_tokens`, `gen_ai.usage.completion_tokens` | tokens in and out |

Request 4 above: 3.94 s end to end, 3.29 s of it in the queue, 0.62 s of actual work on a 17-token prompt. It was slow because of what was ahead of it.

### 5. Join the client's view to vLLM's

```bash
JAEGER="https://$(oc get route jaeger-ui -n obs-demo -o jsonpath='{.spec.host}')"
python3 loadgen/join_traces.py loadgen-with-tracing.csv --jaeger-url "$JAEGER" --top 5 --save-dir traces/
```

One table, one row per request: client total, vLLM end to end, the difference (route and network), queue, prefill, decode, time to first token and token counts. `--save-dir` keeps each trace as JSON, which is the evidence behind any screenshot. `--markdown` prints the table ready to paste. Add `--insecure` for a self-signed Route.

## Things worth knowing

- **Sampling.** vLLM honors the sampled flag in an incoming `traceparent`, which is why the load generator sets it. Without a client trace context the span still exists, but a sampled parent is what guarantees the ID in your terminal is the ID in Jaeger. `--no-traceparent` turns the header off so you can see the difference.
- **Endpoint scheme.** `http://host:4317` and `grpc://host:4317` are equivalent for vLLM's gRPC exporter. `https://` forces TLS, so don't use it unless the collector serves TLS.
- **One span.** vLLM emits one `llm_request` span per request, created when the request finishes, with the phases as attributes. There are no child spans for queue, prefill and decode.
- **Metrics are separate.** vLLM's `/metrics` endpoint is on regardless of tracing, and OpenShift AI's dashboards read it. Traces answer "what happened to this request". Metrics answer "how is this server doing".

## The series

1. **Where did that request go?** Pod metrics say healthy, dashboards say which model is slow, one trace says why one request waited.
2. Tracing the inference path from gateway to GPU, through the llm-d router. In progress.
3. What your GPUs are actually doing, and what it costs. In progress.
4. Talk to your platform: AI-native operations with MCP. In progress.

## License

Apache License 2.0. See `LICENSE`.
