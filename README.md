# Trace GUI

Local read-only dashboard for CoherentKV and Agentic Serving Observatory run
artifacts.

## Scope

This directory is the only write scope for the dashboard. The server reads from:

- `../coherent_kv/runs`
- `../agentic_serving_observatory/schemas`

It does not create cache files, mutate run folders, or write outside
`trace_gui`.

## Run

```bash
cd /home/seralab-server/swwoo/lab_project/trace_gui
python3 app.py --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765
```

## Views

- Overview: run inventory, request count, latency, prefix hit ratio, and files.
- Ablations: paired `repair_on` versus `reject_only` comparison.
- Run Detail: gateway request timeline, tool counts, and per-request cache data.
- Cache Events: vLLM event mix, CoherentKV reject reasons, and prefix token sequence.
