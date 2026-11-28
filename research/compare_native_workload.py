"""Align native host journals by first bindings of mesh/pixel pipeline pairs."""

import argparse
import codecs
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

if __package__:
    from . import compare_native_frames as timing
else:
    import compare_native_frames as timing


BIND = re.compile(
    r'first bind pipeline=(0x[0-9a-fA-F]+) frame=(\d+) stage="(mesh|pixel)" '
    r"size=\d+ hash=([0-9a-fA-F]{16})\b"
)
HOST = re.compile(
    r"\b([\d.]+) fps, ([\d.]+) ms/frame .*?, ([\d.]+) draws/frame, "
    r"dropped (\d+) without a host pipeline"
)
PAIR = re.compile(r"^[0-9a-f]{16}/[0-9a-f]{16}$")
TOTAL = re.compile(
    r"VELKIN_QUEUE_TOTAL frame=(\d+) draws=(\d+) drawn=(\d+) dispatches=(\d+) dispatched=(\d+)$"
)
SUCCESS = re.compile(r"VELKIN_PRESENT frame=(\d+)(?: interval_us=(\d+))?$")
BOUND = re.compile(
    r"VELKIN_MESH_BIND frame=(\d+) guest_token=\d+ mesh=([0-9a-f]{16}) pixel=([0-9a-f]{16})$"
)


@dataclass
class Run:
    windows: dict
    checkpoints: dict
    pairs: dict
    host: dict
    cumulative: dict = field(default_factory=dict)
    successes: dict = field(default_factory=dict)
    anchor_clock: str = "presentx"


def record_frame(series, frame, value, same_stream):
    if frame in same_stream or (frame in series and series[frame] != value):
        raise ValueError(f"repeated or conflicting workload record for frame {frame}")
    series[frame] = value
    same_stream.add(frame)


def read_journal(path):
    checkpoints = {}
    windows = timing.read_run(
        path, queue_samples=checkpoints, require_windows=False, skip_frame_gaps=True
    )
    cumulative, successes, source_pairs = {}, {}, {}
    stages = defaultdict(lambda: defaultdict(set))
    host = defaultdict(list)
    seen = set()
    for log in timing.log_files(path):
        with log.open("rb") as source:
            prefix = source.read(2)
        encoding = (
            "utf-16"
            if prefix in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
            else "utf-8-sig"
        )
        # Host samples have no frame field. Bracket them between two consecutive
        # timing records from this same stream, never infer a frame from a timestamp.
        last = None
        pending = []
        seen_totals, seen_successes = set(), set()
        with log.open(encoding=encoding, errors="replace") as source:
            for line in source:
                line = timing.ANSI.sub("", line) if "\x1b" in line else line
                if match := TOTAL.search(line.rstrip()):
                    frame, *values = map(int, match.groups())
                    record_frame(
                        cumulative,
                        frame,
                        dict(zip(timing.QUEUE_FIELDS, values)),
                        seen_totals,
                    )
                elif match := SUCCESS.search(line.rstrip()):
                    frame, micros = match.groups()
                    record_frame(
                        successes,
                        int(frame),
                        int(micros) if micros is not None else None,
                        seen_successes,
                    )
                elif match := BOUND.search(line.rstrip()):
                    frame, mesh, pixel = match.groups()
                    key = f"{mesh}/{pixel}"
                    source_pairs[key] = min(
                        int(frame), source_pairs.get(key, int(frame))
                    )
                elif any(
                    k in line
                    for k in (
                        "VELKIN_QUEUE_TOTAL ",
                        "VELKIN_PRESENT ",
                        "VELKIN_MESH_BIND ",
                    )
                ):
                    raise ValueError(f"malformed native workload record in {log.name}")
                elif item := timing.parse_line(line, skip_frame_gaps=True):
                    (first, end), _ = item
                    if last == first:
                        for key, record in pending:
                            if (first, key) not in seen:
                                seen.add((first, key))
                                host[(first, end)].append(record)
                    last = end
                    pending.clear()
                elif match := BIND.search(line):
                    address, frame, stage, digest = match.groups()
                    stages[(int(frame), address.lower())][stage].add(digest.lower())
                elif match := HOST.search(line):
                    _, ms, draws, dropped = match.groups()
                    pending.append(
                        (
                            line.rstrip(),
                            {
                                "reported_ms_per_frame": float(ms),
                                "draws_per_frame": float(draws),
                                "dropped_without_host_pipeline": int(dropped),
                            },
                        )
                    )
    pairs = {}
    for (frame, _), shader in sorted(stages.items()):
        if len(shader["mesh"]) == len(shader["pixel"]) == 1:
            key = next(iter(shader["mesh"])) + "/" + next(iter(shader["pixel"]))
            pairs.setdefault(key, frame)
    raw = bool(cumulative or successes or source_pairs)
    if not windows and not raw:
        raise ValueError("no native timing or workload records")
    prior = None
    for frame, values in sorted(cumulative.items()):
        if prior is not None and any(values[k] < prior[k] for k in values):
            raise ValueError(f"decreasing cumulative counters at frame {frame}")
        prior = values
    return Run(
        windows,
        checkpoints,
        source_pairs if raw else pairs,
        host,
        cumulative,
        successes,
        "presentx" if raw else "guest_frame_token_legacy_estimate",
    )


def discover(control, candidate, quiet):
    if quiet < 1:
        raise ValueError("quiet interval must be positive")
    groups = []
    previous = None
    for key, frame in sorted(
        control.pairs.items(), key=lambda item: (item[1], item[0])
    ):
        if previous is None or frame - previous >= quiet:
            groups.append([])
        groups[-1].append(key)
        previous = frame
    return [
        next((k for k in group if k in candidate.pairs), group[0]) for group in groups
    ]


def timing_window(run, key):
    start, end = key
    return {
        "from_frame": start,
        "to_frame": end,
        "cpu_present_ms": {
            q: run.windows[key][f"cpu_present_{q}_us"] / 1000 for q in timing.METRICS
        },
        "host_samples_logged_between_timing_records": run.host.get(key, []),
    }


def anchor_evidence(run, pair):
    frame = run.pairs.get(pair)
    if frame is None:
        return None
    before = next((k for k in run.windows if k[0] < frame <= k[1]), None)
    after = min((k for k in run.windows if frame <= k[0] < frame + 30), default=None)
    return {
        "frame": frame,
        "frame_source": run.anchor_clock,
        "frames_until_end_of_timing_window": before[1] - frame if before else None,
        "containing_window": timing_window(run, before) if before else None,
        "first_complete_window_after_anchor": timing_window(run, after)
        if after
        else None,
    }


def exact_present_span(run, start, end):
    if run.anchor_clock != "presentx" or start not in run.successes:
        return None
    # Every element corresponds to one successful return at k and its immediate
    # predecessor k-1. Failures, skipped frames and truncated logs break coverage.
    if end - start > len(run.successes):
        return None
    samples = [run.successes.get(k) for k in range(start + 1, end + 1)]
    if not samples or any(t is None for t in samples):
        return None
    samples.sort()
    count = len(samples)
    return {
        "samples": count,
        "nearest_rank_us": {
            f"p{p}": samples[(p * count + 99) // 100 - 1] for p in (50, 95, 99)
        },
    }


def interval_evidence(run, start, end):
    # An observed interval has a strict left endpoint. Counters resetting on a
    # native checkpoint can only recover totals when both bounds tile exactly.
    checks = sorted(k for k in run.checkpoints if start <= k[0] and k[1] <= end)
    counts = None
    source = None
    if (
        checks
        and checks[0][0] == start
        and checks[-1][1] == end
        and all(a[1] == b[0] for a, b in zip(checks, checks[1:]))
    ):
        counts = {
            field: sum(run.checkpoints[k][field] for k in checks)
            for field in timing.QUEUE_FIELDS
        }
        source = "power_of_two_queue_checkpoints"
    if start in run.cumulative and end in run.cumulative:
        counts = {
            key: run.cumulative[end][key] - run.cumulative[start][key]
            for key in timing.QUEUE_FIELDS
        }
        source = "cumulative_presentx_endpoints"
    if run.anchor_clock != "presentx":
        counts, source = None, None
    signatures = {p for p, f in run.pairs.items() if start < f <= end}
    return {
        "frame_interval": [start, end],
        "bound_clock": run.anchor_clock,
        "interval_count": end - start,
        "first_bound_mesh_pixel_pair_count": len(signatures),
        "queue_totals_if_exactly_covered": counts,
        "queue_totals_source": source,
        "cpu_present_exact": exact_present_span(run, start, end),
        "complete_timing_windows": [
            timing_window(run, k)
            for k in sorted(run.windows)
            if start <= k[0] and k[1] <= end
        ],
    }, signatures


def compare(control, candidate, requested=None, quiet=120, tail=30):
    if tail < 0:
        raise ValueError("tail interval count must be nonnegative")
    keys = requested or discover(control, candidate, quiet)
    if not keys:
        raise ValueError("no reference mesh/pixel first bindings")
    if any(not PAIR.fullmatch(k) for k in keys) or len(set(keys)) != len(keys):
        raise ValueError("provide distinct ordered 16-hex mesh/pixel pairs")
    runs = {"control": control, "candidate": candidate}
    anchors = [
        {
            "mesh_pixel": k,
            **{name: anchor_evidence(run, k) for name, run in runs.items()},
        }
        for k in keys
    ]
    order = {}
    for name, run in runs.items():
        observed = [run.pairs.get(k) for k in keys]
        order[name] = all(f is not None for f in observed) and all(
            a < b for a, b in zip(observed, observed[1:])
        )
    report = {
        "selection": "explicit"
        if requested
        else f"first shared pair per reference cluster, gap >= {quiet} frames",
        "anchors": anchors,
        "all_anchors_present_in_order": order,
        "anchored_intervals": [],
        "stop_bound": {},
        "measurement": "CPU intervals between successful DXGI Present returns. Exact spans use consecutive individual samples and nearest-rank quantiles.",
        "limits": "Exact spans bracket the last PresentX observed at each mesh/pixel binding. Legacy binding labels are guest wait tokens. Host summaries are bracketed by log order. Scene identity requires separate evidence.",
    }
    for name, run in runs.items():
        target = run.pairs[keys[-1]] + tail if order[name] else None
        end = max([*(k[1] for k in run.windows), *run.successes], default=None)
        covered = False
        if target is not None and run.successes:
            origin = run.pairs[keys[-1]]
            covered = (
                origin in run.successes
                if tail == 0
                else exact_present_span(run, origin, target) is not None
            )
        elif target is not None and end is not None:
            cursor = run.pairs[keys[-1]]
            for a, b in sorted(run.windows):
                if a <= cursor < b:
                    cursor = b
            covered = cursor >= target and (tail > 0 or end >= target)
        report["stop_bound"][name] = {
            "target_present_frame": target,
            "last_timing_frame": end,
            "frame_source": run.anchor_clock,
            "reached": covered,
            "verified_presentx_bound": covered and run.anchor_clock == "presentx",
        }
    if not all(order.values()):
        return report
    for first, last in zip(keys, keys[1:]):
        sides = {}
        signatures = {}
        for name, run in runs.items():
            sides[name], signatures[name] = interval_evidence(
                run, run.pairs[first], run.pairs[last]
            )
        a, b = (sides[name]["queue_totals_if_exactly_covered"] for name in runs)
        same_signatures = signatures["control"] == signatures["candidate"]
        same_counts = a == b if a is not None and b is not None else None
        same_length = (
            sides["control"]["interval_count"] == sides["candidate"]["interval_count"]
        )
        if control.anchor_clock != "presentx" or candidate.anchor_clock != "presentx":
            same_length = None
        if not same_signatures or same_counts is False or same_length is False:
            result = "observed workloads differ"
        elif same_counts is None:
            result = "inconclusive: exact draw and dispatch totals unavailable"
        else:
            result = "measured counters and first-bound pairs match; scene identity unverified"
        report["anchored_intervals"].append(
            {
                "from_anchor": first,
                "through_anchor": last,
                **sides,
                "common_first_bound_pair_count": len(
                    signatures["control"] & signatures["candidate"]
                ),
                "matching_first_bound_pair_sets": same_signatures,
                "matching_present_interval_count": same_length,
                "matching_queue_totals": same_counts,
                "workload_check": result,
            }
        )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("control", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument(
        "--anchor",
        action="append",
        help="repeat in reference order: mesh_hash/pixel_hash",
    )
    parser.add_argument(
        "--quiet-frames",
        type=int,
        default=120,
        help="reference gap separating discovery clusters",
    )
    parser.add_argument(
        "--tail-intervals",
        type=int,
        default=30,
        help="successful Present intervals required after final anchor",
    )
    args = parser.parse_args()
    try:
        result = compare(
            read_journal(args.control),
            read_journal(args.candidate),
            args.anchor,
            args.quiet_frames,
            args.tail_intervals,
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))
