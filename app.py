#!/usr/bin/env python3
"""Read-only experiment trace dashboard for CoherentKV/ASO runs.

The server writes nothing. It serves static assets from this directory and
reads experiment artifacts from sibling project directories.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


APP_DIR = Path(__file__).resolve().parent
LAB_PROJECT_DIR = APP_DIR.parent
RUNS_DIR = LAB_PROJECT_DIR / "coherent_kv" / "runs"
SCHEMA_DIR = LAB_PROJECT_DIR / "agentic_serving_observatory" / "schemas"
STATIC_DIR = APP_DIR / "static"

MAX_DETAIL_EVENTS = 12000
MAX_DETAIL_VLLM_EVENTS = 20000
MAX_TABLE_ROWS = 500
SCAN_CACHE_TTL_S = 4.0

_SCAN_CACHE: dict[str, Any] = {"expires_at": 0.0, "payload": None}


def safe_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def read_jsonl(path: Path, limit: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    truncated = False
    try:
        with path.open("r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                if limit is not None and idx >= limit:
                    truncated = True
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        rows.append(item)
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return rows, truncated


def count_jsonl(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def round_number(value: Any, places: int = 2) -> float | None:
    num = as_number(value)
    if num is None:
        return None
    return round(num, places)


def normalize_ratio(value: Any) -> float | None:
    num = as_number(value)
    if num is None:
        return None
    if num > 1.0:
        return round(num / 100.0, 4)
    return round(num, 4)


def file_meta(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        return {"exists": True, "bytes": stat.st_size}
    except OSError:
        return {"exists": False, "bytes": 0}


def first_model(run_dir: Path, manifest: dict[str, Any] | None = None) -> str | None:
    if manifest and isinstance(manifest.get("model"), str):
        return manifest["model"]
    models = safe_json(run_dir / "vllm_models.json", {})
    data = models.get("data") if isinstance(models, dict) else None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first.get("id") or first.get("root")
    return None


def parse_started_at(name: str, manifest: dict[str, Any] | None) -> str | None:
    if manifest and isinstance(manifest.get("started_at_utc"), str):
        return manifest["started_at_utc"]
    match = re.match(r"(?P<date>\d{8})T(?P<time>\d{6})Z", name)
    if match:
        raw = match.group("date") + match.group("time")
        try:
            return (
                datetime.strptime(raw, "%Y%m%d%H%M%S")
                .replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except ValueError:
            return None
    date_match = re.match(r"(?P<date>\d{8})T", name)
    if date_match:
        try:
            return (
                datetime.strptime(date_match.group("date"), "%Y%m%d")
                .replace(tzinfo=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except ValueError:
            return None
    return None


def classify_run(name: str) -> dict[str, Any]:
    lower = name.lower()
    repair_mode = None
    if "repair_on" in lower:
        repair_mode = "repair_on"
    elif "reject_only" in lower:
        repair_mode = "reject_only"

    policy_case = "other"
    for candidate in (
        "semantic-guard",
        "unsafe-exact",
        "unsafe-position",
        "no-publish",
        "semantic-compatible",
        "response-fingerprint",
        "exact-fingerprint",
    ):
        if candidate in lower:
            policy_case = candidate
            break

    family = "other"
    if "measured-time-to-correct" in lower or "ttc-" in lower:
        family = "time-to-correct"
    if "prefix-sweep" in lower:
        family = "prefix-sweep"
    if "repair-connector-live-ablation" in lower:
        family = "repair-ablation"
    if "validated_publish_batch" in lower:
        family = "validated-publish"
    if "native_prefix" in lower:
        family = "native-prefix"
    if "smoke" in lower:
        family = "smoke"

    workload = "general"
    if "swebench" in lower or "swe-bench" in lower:
        workload = "swebench"
    elif "openhands" in lower:
        workload = "openhands"
    elif "tau2" in lower:
        workload = "tau2"

    repeat = None
    repeat_match = re.search(r"ttc-r(\d+)", lower)
    if repeat_match:
        repeat = int(repeat_match.group(1))

    pair_key = re.sub(r"-repair-connector-live-ablation-(repair_on|reject_only)$", "", name)
    pair_key = re.sub(r"-(repair_on|reject_only)$", "", pair_key)

    labels = [family, workload]
    if policy_case != "other":
        labels.append(policy_case)
    if repair_mode:
        labels.append(repair_mode)
    if repeat is not None:
        labels.append(f"r{repeat}")

    return {
        "family": family,
        "workload": workload,
        "policy_case": policy_case,
        "repair_mode": repair_mode,
        "repeat": repeat,
        "pair_key": pair_key,
        "labels": labels,
    }


def latency_from_summary(summary: dict[str, Any], replay: dict[str, Any]) -> dict[str, Any]:
    gateway = summary.get("gateway_latency_ms") if isinstance(summary, dict) else None
    replay_latency = replay.get("latency_ms") if isinstance(replay, dict) else None
    source = gateway if isinstance(gateway, dict) else replay_latency
    if not isinstance(source, dict):
        source = {}
    return {
        "count": int(source.get("count") or 0),
        "mean": round_number(source.get("mean")),
        "p50": round_number(source.get("p50")),
        "p95": round_number(source.get("p95")),
        "p99": round_number(source.get("p99")),
        "min": round_number(source.get("min")),
        "max": round_number(source.get("max")),
        "total": round_number(source.get("total")),
        "source": "gateway" if isinstance(gateway, dict) else ("replay" if isinstance(replay_latency, dict) else None),
    }


def child_rollup(summary: dict[str, Any]) -> dict[str, Any]:
    children = summary.get("children")
    if not isinstance(children, list) or not children:
        return {}

    request_count = 0
    means = []
    p95s = []
    total_latency = 0.0
    hit_tokens = 0.0
    miss_tokens = 0.0
    token_total = 0.0
    event_counts: Counter[str] = Counter()
    vllm_counts: Counter[str] = Counter()
    modes: dict[str, Any] = {}

    for child in children:
        if not isinstance(child, dict):
            continue
        lat = child.get("gateway_latency_ms")
        if isinstance(lat, dict):
            request_count += int(lat.get("count") or 0)
            if as_number(lat.get("mean")) is not None:
                means.append(float(lat["mean"]))
            if as_number(lat.get("p95")) is not None:
                p95s.append(float(lat["p95"]))
            total_latency += float(lat.get("total") or 0)
        prefix = child.get("vllm_prefix_summary")
        if isinstance(prefix, dict):
            hit_tokens += float(prefix.get("cache_hit_tokens") or 0)
            miss_tokens += float(prefix.get("cache_miss_tokens") or 0)
            token_total += float(prefix.get("num_tokens") or 0)
        if isinstance(child.get("event_counts"), dict):
            event_counts.update(child["event_counts"])
        if isinstance(child.get("vllm_event_counts"), dict):
            vllm_counts.update(child["vllm_event_counts"])
        mode = child.get("prefix_mode")
        if isinstance(mode, str):
            modes[mode] = {
                "mean_latency_ms": round_number(lat.get("mean") if isinstance(lat, dict) else None),
                "p95_latency_ms": round_number(lat.get("p95") if isinstance(lat, dict) else None),
                "hit_ratio": normalize_ratio(prefix.get("cache_hit_ratio") if isinstance(prefix, dict) else None),
                "request_count": int((lat or {}).get("count") or 0) if isinstance(lat, dict) else 0,
            }

    return {
        "request_count": request_count,
        "mean_latency": round(sum(means) / len(means), 2) if means else None,
        "p95_latency": round(sum(p95s) / len(p95s), 2) if p95s else None,
        "total_latency": round(total_latency, 2) if total_latency else None,
        "hit_tokens": int(hit_tokens),
        "miss_tokens": int(miss_tokens),
        "token_total": int(token_total),
        "hit_ratio": round(hit_tokens / token_total, 4) if token_total else None,
        "event_counts": dict(event_counts),
        "vllm_event_counts": dict(vllm_counts),
        "modes": modes,
    }


def build_run_summary(run_dir: Path) -> dict[str, Any]:
    name = run_dir.name
    summary = safe_json(run_dir / "summary.json", {}) or {}
    replay = safe_json(run_dir / "replay.json", {}) or {}
    manifest = safe_json(run_dir / "manifest.json", {}) or {}
    done = safe_json(run_dir / "done.json", {}) or {}
    candidate_summary = safe_json(run_dir / "candidate_index_summary.json", {}) or {}
    candidate_index = safe_json(run_dir / "candidate_index.json", {}) or {}

    tags = classify_run(name)
    rollup = child_rollup(summary) if isinstance(summary, dict) else {}
    latency = latency_from_summary(summary, replay)

    event_counts = summary.get("event_counts") if isinstance(summary.get("event_counts"), dict) else {}
    vllm_counts = summary.get("vllm_event_counts") if isinstance(summary.get("vllm_event_counts"), dict) else {}
    if rollup:
        event_counts = rollup.get("event_counts") or event_counts
        vllm_counts = rollup.get("vllm_event_counts") or vllm_counts

    request_count = (
        latency.get("count")
        or event_counts.get("REQUEST_DONE")
        or replay.get("sent")
        or replay.get("records")
        or rollup.get("request_count")
        or 0
    )

    prefix = summary.get("vllm_prefix_summary") if isinstance(summary.get("vllm_prefix_summary"), dict) else {}
    hit_tokens = prefix.get("cache_hit_tokens")
    miss_tokens = prefix.get("cache_miss_tokens")
    token_total = prefix.get("num_tokens")
    hit_ratio = prefix.get("cache_hit_ratio")
    if rollup:
        hit_tokens = rollup.get("hit_tokens")
        miss_tokens = rollup.get("miss_tokens")
        token_total = rollup.get("token_total")
        hit_ratio = rollup.get("hit_ratio")
    if hit_ratio is None and (vllm_counts.get("PREFIX_HIT") or vllm_counts.get("PREFIX_MISS")):
        hits = float(vllm_counts.get("PREFIX_HIT") or 0)
        misses = float(vllm_counts.get("PREFIX_MISS") or 0)
        hit_ratio = hits / (hits + misses) if hits + misses else None

    if rollup and latency.get("count") == 0:
        latency.update(
            {
                "count": rollup.get("request_count", 0),
                "mean": rollup.get("mean_latency"),
                "p95": rollup.get("p95_latency"),
                "total": rollup.get("total_latency"),
                "source": "summary_children",
            }
        )

    files = {
        "summary": file_meta(run_dir / "summary.json"),
        "events": file_meta(run_dir / "events.jsonl"),
        "vllm_events": file_meta(run_dir / "vllm_events.jsonl"),
        "replay": file_meta(run_dir / "replay.json"),
        "candidate_index": file_meta(run_dir / "candidate_index.json"),
        "completed": file_meta(run_dir / "completed.jsonl"),
        "manifest": file_meta(run_dir / "manifest.json"),
    }

    return {
        "id": name,
        "started_at": parse_started_at(name, manifest if isinstance(manifest, dict) else None),
        "tags": tags,
        "labels": tags["labels"],
        "model": first_model(run_dir, manifest if isinstance(manifest, dict) else None),
        "trace": manifest.get("trace") or replay.get("trace") if isinstance(manifest, dict) and isinstance(replay, dict) else None,
        "files": files,
        "metrics": {
            "request_count": int(request_count or 0),
            "latency_ms": latency,
            "event_counts": event_counts,
            "vllm_event_counts": vllm_counts,
            "prefix": {
                "hit_ratio": normalize_ratio(hit_ratio),
                "hit_tokens": int(hit_tokens or 0),
                "miss_tokens": int(miss_tokens or 0),
                "num_tokens": int(token_total or 0),
            },
            "replay": {
                "kind": replay.get("kind"),
                "policy": replay.get("policy"),
                "sent": replay.get("sent"),
                "records": replay.get("records"),
                "errors": replay.get("errors"),
                "status_counts": replay.get("status_counts") or latency.get("status_counts"),
                "committed_records": replay.get("committed_records"),
                "isolated_non_committed_records": replay.get("isolated_non_committed_records"),
            },
            "candidate": {
                "accepted": candidate_summary.get("accepted"),
                "candidate_count": candidate_index.get("candidate_count"),
                "accepted_candidate_count": candidate_index.get("accepted_candidate_count"),
                "semantic_compatible": candidate_index.get("semantic_compatible"),
                "physical_class": candidate_index.get("physical_class"),
            },
            "prefix_modes": rollup.get("modes", {}),
        },
        "done": done if isinstance(done, dict) else {},
    }


def aggregate_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    total_requests = sum(r["metrics"]["request_count"] for r in runs)
    runs_with_events = sum(1 for r in runs if r["files"]["events"]["exists"])
    runs_with_vllm = sum(1 for r in runs if r["files"]["vllm_events"]["exists"])
    families = Counter(r["tags"]["family"] for r in runs)
    workloads = Counter(r["tags"]["workload"] for r in runs)
    repair_modes = Counter(r["tags"].get("repair_mode") or "none" for r in runs)
    prefix_hits = sum(r["metrics"]["prefix"]["hit_tokens"] for r in runs)
    prefix_tokens = sum(r["metrics"]["prefix"]["num_tokens"] for r in runs)
    errors = sum(int(r["metrics"]["replay"].get("errors") or 0) for r in runs)
    latencies = [r["metrics"]["latency_ms"]["mean"] for r in runs if r["metrics"]["latency_ms"].get("mean") is not None]

    return {
        "run_count": len(runs),
        "total_requests": total_requests,
        "runs_with_events": runs_with_events,
        "runs_with_vllm_events": runs_with_vllm,
        "mean_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "prefix_hit_ratio": round(prefix_hits / prefix_tokens, 4) if prefix_tokens else None,
        "replay_errors": errors,
        "families": dict(families),
        "workloads": dict(workloads),
        "repair_modes": dict(repair_modes),
    }


def build_compare(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for run in runs:
        mode = run["tags"].get("repair_mode")
        if mode not in {"repair_on", "reject_only"}:
            continue
        key = run["tags"]["pair_key"]
        group = grouped.setdefault(
            key,
            {
                "pair_key": key,
                "policy_case": run["tags"].get("policy_case"),
                "repeat": run["tags"].get("repeat"),
                "runs": {},
            },
        )
        group["runs"][mode] = run

    pairs = []
    for group in grouped.values():
        repair = group["runs"].get("repair_on")
        reject = group["runs"].get("reject_only")
        if not repair or not reject:
            continue
        repair_lat = repair["metrics"]["latency_ms"].get("mean")
        reject_lat = reject["metrics"]["latency_ms"].get("mean")
        repair_hit = repair["metrics"]["prefix"].get("hit_ratio")
        reject_hit = reject["metrics"]["prefix"].get("hit_ratio")
        latency_delta = None
        latency_pct = None
        if repair_lat is not None and reject_lat not in (None, 0):
            latency_delta = round(repair_lat - reject_lat, 2)
            latency_pct = round((repair_lat - reject_lat) / reject_lat, 4)
        hit_delta = None
        if repair_hit is not None and reject_hit is not None:
            hit_delta = round(repair_hit - reject_hit, 4)
        pairs.append(
            {
                "pair_key": group["pair_key"],
                "policy_case": group["policy_case"],
                "repeat": group["repeat"],
                "repair_on": {
                    "id": repair["id"],
                    "mean_latency_ms": repair_lat,
                    "p95_latency_ms": repair["metrics"]["latency_ms"].get("p95"),
                    "hit_ratio": repair_hit,
                    "requests": repair["metrics"]["request_count"],
                    "errors": repair["metrics"]["replay"].get("errors"),
                },
                "reject_only": {
                    "id": reject["id"],
                    "mean_latency_ms": reject_lat,
                    "p95_latency_ms": reject["metrics"]["latency_ms"].get("p95"),
                    "hit_ratio": reject_hit,
                    "requests": reject["metrics"]["request_count"],
                    "errors": reject["metrics"]["replay"].get("errors"),
                },
                "delta": {
                    "mean_latency_ms": latency_delta,
                    "mean_latency_pct": latency_pct,
                    "hit_ratio": hit_delta,
                },
            }
        )
    pairs.sort(key=lambda x: (x["policy_case"] or "", x["repeat"] or 0, x["pair_key"]))
    return pairs


def scan_runs(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _SCAN_CACHE["payload"] is not None and now < _SCAN_CACHE["expires_at"]:
        return _SCAN_CACHE["payload"]

    runs = []
    if RUNS_DIR.exists():
        for run_dir in sorted([p for p in RUNS_DIR.iterdir() if p.is_dir()], key=lambda p: p.name, reverse=True):
            runs.append(build_run_summary(run_dir))
    runs.sort(key=lambda run: (run.get("started_at") or "0000", run["id"]), reverse=True)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": {
            "app_dir": str(APP_DIR),
            "runs_dir": str(RUNS_DIR),
            "schema_dir": str(SCHEMA_DIR),
            "runs_dir_exists": RUNS_DIR.exists(),
            "schema_dir_exists": SCHEMA_DIR.exists(),
            "write_scope": str(APP_DIR),
            "note": "This dashboard serves files from trace_gui and reads sibling project artifacts without modifying them.",
        },
        "stats": aggregate_runs(runs),
        "runs": runs,
        "compare": build_compare(runs),
    }
    _SCAN_CACHE["payload"] = payload
    _SCAN_CACHE["expires_at"] = now + SCAN_CACHE_TTL_S
    return payload


def extract_uuid(value: str) -> str | None:
    match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value,
        re.IGNORECASE,
    )
    return match.group(0) if match else None


def event_preview(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return {
        "event_type": event.get("event_type"),
        "ts": event.get("ts"),
        "request_id": event.get("request_id"),
        "program_id": event.get("program_id"),
        "turn_id": event.get("turn_id"),
        "source": event.get("source"),
        "data": data,
    }


def build_request_timeline(events: list[dict[str, Any]], vllm_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requests: dict[str, dict[str, Any]] = {}
    for event in events:
        req_id = event.get("request_id")
        if not isinstance(req_id, str):
            continue
        rec = requests.setdefault(
            req_id,
            {
                "request_id": req_id,
                "program_id": event.get("program_id"),
                "turn_id": event.get("turn_id"),
                "start_ts": None,
                "end_ts": None,
                "elapsed_ms": None,
                "status_code": None,
                "tool_name": None,
                "agent_role": None,
                "model": None,
                "events": [],
                "vllm_counts": {},
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
                "reject_reasons": [],
            },
        )
        event_type = event.get("event_type")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        rec["events"].append(event_type)
        if event_type == "REQUEST_ARRIVE":
            rec["start_ts"] = event.get("ts")
            rec["program_id"] = event.get("program_id")
            rec["turn_id"] = event.get("turn_id")
            rec["tool_name"] = data.get("tool_name")
            rec["agent_role"] = data.get("agent_role")
            rec["model"] = data.get("model")
        elif event_type == "REQUEST_DONE":
            rec["end_ts"] = event.get("ts")
            rec["status_code"] = data.get("status_code")
            rec["elapsed_ms"] = round_number(data.get("elapsed_ms"))

    vllm_by_uuid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in vllm_events:
        req_id = event.get("request_id")
        if isinstance(req_id, str):
            uuid = extract_uuid(req_id)
            if uuid:
                vllm_by_uuid[uuid].append(event)

    for req_id, rec in requests.items():
        if rec["elapsed_ms"] is None and rec["start_ts"] is not None and rec["end_ts"] is not None:
            rec["elapsed_ms"] = round_number((float(rec["end_ts"]) - float(rec["start_ts"])) * 1000)
        grouped = vllm_by_uuid.get(req_id, [])
        counts = Counter(e.get("event_type") for e in grouped)
        rec["vllm_counts"] = {str(k): v for k, v in counts.items() if k}
        for event in grouped:
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if event.get("event_type") in {"PREFIX_HIT", "PREFIX_MISS"}:
                rec["cache_hit_tokens"] += int(data.get("cache_hit_tokens") or 0)
                rec["cache_miss_tokens"] += int(data.get("cache_miss_tokens") or 0)
            if event.get("event_type") == "COHERENT_KV_REJECT" and data.get("reason"):
                rec["reject_reasons"].append(data["reason"])

    rows = list(requests.values())
    rows.sort(key=lambda r: (r["start_ts"] is None, r["start_ts"] or 0, r["request_id"]))
    return rows[:MAX_TABLE_ROWS]


def build_detail(run_id: str) -> tuple[int, dict[str, Any]]:
    if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid run id"}
    run_dir = (RUNS_DIR / run_id).resolve()
    try:
        run_dir.relative_to(RUNS_DIR.resolve())
    except ValueError:
        return HTTPStatus.BAD_REQUEST, {"error": "invalid run path"}
    if not run_dir.is_dir():
        return HTTPStatus.NOT_FOUND, {"error": "run not found"}

    summary = build_run_summary(run_dir)
    raw_summary = safe_json(run_dir / "summary.json", {}) or {}
    replay = safe_json(run_dir / "replay.json", {}) or {}
    manifest = safe_json(run_dir / "manifest.json", {}) or {}
    done = safe_json(run_dir / "done.json", {}) or {}
    candidate_index = safe_json(run_dir / "candidate_index.json", {}) or {}
    events, events_truncated = read_jsonl(run_dir / "events.jsonl", MAX_DETAIL_EVENTS)
    vllm_events, vllm_truncated = read_jsonl(run_dir / "vllm_events.jsonl", MAX_DETAIL_VLLM_EVENTS)
    completed, completed_truncated = read_jsonl(run_dir / "completed.jsonl", MAX_DETAIL_EVENTS)

    event_counts = Counter(e.get("event_type") for e in events)
    source_counts = Counter(e.get("source") for e in events)
    turn_counts = Counter(e.get("turn_id") for e in events if e.get("turn_id") is not None)
    tool_counts = Counter()
    role_counts = Counter()
    model_counts = Counter()
    for event in events:
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if data.get("tool_name"):
            tool_counts[data["tool_name"]] += 1
        if data.get("agent_role"):
            role_counts[data["agent_role"]] += 1
        if data.get("model"):
            model_counts[data["model"]] += 1

    vllm_counts = Counter(e.get("event_type") for e in vllm_events)
    reject_reasons = Counter()
    prefix_points = []
    block_allocs = []
    first_ts = None
    for event in vllm_events:
        ts = as_number(event.get("ts"))
        if ts is not None and first_ts is None:
            first_ts = ts
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event.get("event_type") == "COHERENT_KV_REJECT" and data.get("reason"):
            reject_reasons[data["reason"]] += 1
        if event.get("event_type") in {"PREFIX_HIT", "PREFIX_MISS"}:
            prefix_points.append(
                {
                    "event_type": event.get("event_type"),
                    "t": round(ts - first_ts, 3) if ts is not None and first_ts is not None else None,
                    "request_id": event.get("request_id"),
                    "hit_tokens": int(data.get("cache_hit_tokens") or 0),
                    "miss_tokens": int(data.get("cache_miss_tokens") or 0),
                    "num_tokens": int(data.get("num_tokens") or 0),
                }
            )
        if event.get("event_type") == "ALLOCATE_SLOTS":
            block_allocs.append(
                {
                    "request_id": event.get("request_id"),
                    "num_tokens": data.get("num_tokens"),
                    "num_new_tokens_arg": data.get("num_new_tokens_arg"),
                    "num_computed_tokens": data.get("num_computed_tokens"),
                    "num_external_computed_tokens": data.get("num_external_computed_tokens"),
                }
            )

    response_fingerprints = replay.get("response_fingerprints") if isinstance(replay, dict) else None
    if not isinstance(response_fingerprints, list):
        response_fingerprints = []

    candidates = candidate_index.get("candidates") if isinstance(candidate_index, dict) else None
    if not isinstance(candidates, list):
        candidates = []

    payload = {
        "summary": summary,
        "raw": {
            "summary_keys": sorted(raw_summary.keys()) if isinstance(raw_summary, dict) else [],
            "replay": replay,
            "manifest": manifest,
            "done": done,
        },
        "events": {
            "count": count_jsonl(run_dir / "events.jsonl"),
            "loaded": len(events),
            "truncated": events_truncated,
            "event_counts": {str(k): v for k, v in event_counts.items() if k},
            "source_counts": {str(k): v for k, v in source_counts.items() if k},
            "turn_counts": {str(k): v for k, v in turn_counts.items() if k},
            "tool_counts": dict(tool_counts),
            "role_counts": dict(role_counts),
            "model_counts": dict(model_counts),
            "timeline": build_request_timeline(events, vllm_events),
            "sample": [event_preview(e) for e in events[:80]],
        },
        "vllm": {
            "count": count_jsonl(run_dir / "vllm_events.jsonl"),
            "loaded": len(vllm_events),
            "truncated": vllm_truncated,
            "event_counts": {str(k): v for k, v in vllm_counts.items() if k},
            "reject_reasons": dict(reject_reasons),
            "prefix_points": prefix_points[:MAX_TABLE_ROWS],
            "block_allocations": block_allocs[:MAX_TABLE_ROWS],
            "sample": [event_preview(e) for e in vllm_events[:80]],
        },
        "completed": {
            "loaded": len(completed),
            "truncated": completed_truncated,
            "rows": completed[:MAX_TABLE_ROWS],
        },
        "candidates": {
            "candidate_count": candidate_index.get("candidate_count") if isinstance(candidate_index, dict) else None,
            "accepted_candidate_count": candidate_index.get("accepted_candidate_count") if isinstance(candidate_index, dict) else None,
            "sample": candidates[:50],
        },
        "response_fingerprints": response_fingerprints[:MAX_TABLE_ROWS],
    }
    return HTTPStatus.OK, payload


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TraceGui/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {self.address_string()} {fmt % args}")

    def send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, rel_path: str) -> None:
        target = (STATIC_DIR / rel_path).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/":
            self.send_static("index.html")
            return
        if path.startswith("/static/"):
            self.send_static(path.removeprefix("/static/"))
            return
        if path == "/api/health":
            self.send_json(
                {
                    "ok": True,
                    "app_dir": str(APP_DIR),
                    "runs_dir": str(RUNS_DIR),
                    "runs_dir_exists": RUNS_DIR.exists(),
                    "schema_dir": str(SCHEMA_DIR),
                    "schema_dir_exists": SCHEMA_DIR.exists(),
                    "write_scope": str(APP_DIR),
                }
            )
            return
        if path == "/api/runs":
            force = query.get("refresh", ["0"])[0] == "1"
            self.send_json(scan_runs(force=force))
            return
        if path.startswith("/api/runs/"):
            run_id = unquote(path.removeprefix("/api/runs/"))
            status, payload = build_detail(run_id)
            self.send_json(payload, status=status)
            return
        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="CoherentKV trace GUI")
    parser.add_argument("--host", default=os.environ.get("TRACE_GUI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("TRACE_GUI_PORT", "8765")))
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Trace GUI serving http://{args.host}:{args.port}")
    print(f"App dir: {APP_DIR}")
    print(f"Read-only source: {RUNS_DIR}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
