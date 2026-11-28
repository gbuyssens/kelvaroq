"""Compare 30-interval CPU Present timing windows by their exact frame endpoints."""

import argparse
import codecs
import json
import re
from pathlib import Path

MARKER = "VELKIN_FRAME_TIMINGS "
QUEUE_MARKER = "frame presented (since the last line: draws and dispatches, and those run on the host)"
QUEUE_FIELDS = ("draws", "drawn", "dispatches", "dispatched")
METRICS = ("p50", "p95", "p99")
STREAM = re.compile(r"(?:^|[._-])(?:stdout|stderr|out|err)(?:\.(?:log|txt))?$", re.I)
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def log_files(path):
    if not path.is_dir():
        return [path]
    files = sorted(p for p in path.iterdir() if p.is_file() and STREAM.search(p.name))
    if not files:
        raise ValueError(f"no stdout/stderr logs directly under {path}")
    return files


def parse_line(line, *, skip_frame_gaps=False):
    if MARKER not in line:
        return None
    fields = {}
    for token in line.split(MARKER, 1)[1].split():
        key, sep, value = token.partition("=")
        if not sep or key in fields:
            raise ValueError("invalid or duplicate timing field")
        fields[key] = value
    endpoints = re.fullmatch(r"(\d+)\.\.(\d+)", fields.pop("frames", ""))
    if endpoints is None or not all(
        v.isascii() and v.isdigit() for v in fields.values()
    ):
        raise ValueError("invalid frame range or numeric field")
    first, last = map(int, endpoints.groups())
    fields = {k: int(v) for k, v in fields.items()}
    if skip_frame_gaps and fields.get("samples") == 30 and last - first > 30:
        return None
    if fields.get("samples") != 30 or last - first != 30:
        raise ValueError("expected 30 consecutive frame intervals")
    try:
        quantiles = tuple(fields[f"cpu_present_{p}_us"] for p in METRICS)
    except KeyError as e:
        raise ValueError(f"missing {e.args[0]}") from e
    if not quantiles[0] <= quantiles[1] <= quantiles[2]:
        raise ValueError("timing quantiles are out of order")
    return (first, last), fields


def parse_queue_sample(line):
    if QUEUE_MARKER not in line:
        return None
    values = dict(re.findall(r"\b(\w+)=(\d+)\b", line.split(QUEUE_MARKER, 1)[1]))
    if not all(key in values for key in ("frame", *QUEUE_FIELDS)):
        raise ValueError("incomplete queue workload record")
    frame = int(values["frame"])
    if frame == 0 or frame & (frame - 1):
        raise ValueError("expected a power-of-two queue counter checkpoint")
    return (frame // 2, frame), {k: int(values[k]) for k in QUEUE_FIELDS}


def read_run(path, *, queue_samples=None, require_windows=True, skip_frame_gaps=False):
    windows = {}
    for file in log_files(path):
        with file.open("rb") as stream:
            prefix = stream.read(3)
        encoding = (
            "utf-16"
            if prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
            else "utf-8-sig"
        )
        previous_end = None
        with file.open(encoding=encoding, errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                try:
                    if "\x1b" in line:
                        line = ANSI.sub("", line)
                    if queue_samples is not None:
                        sample = parse_queue_sample(line)
                        if sample is not None:
                            key, counts = sample
                            if key in queue_samples and queue_samples[key] != counts:
                                raise ValueError(
                                    "conflicting counts for the same queue interval"
                                )
                            queue_samples[key] = counts
                    parsed = parse_line(line, skip_frame_gaps=skip_frame_gaps)
                    if parsed is None:
                        continue
                    key, fields = parsed
                    if previous_end is not None and key[0] < previous_end:
                        raise ValueError(
                            "overlapping or repeated windows in one stream; use one session"
                        )
                    if key in windows and windows[key] != fields:
                        raise ValueError(
                            "conflicting records for the same window across streams"
                        )
                    previous_end = key[1]
                    windows[key] = fields
                except ValueError as error:
                    raise ValueError(f"{file.name} line {number}: {error}") from error
    if require_windows and not windows:
        raise ValueError(f"no complete native timing windows in {path}")
    return windows


def frame_report(control, candidate=None, first=None, last=None):
    runs = {"control": control}
    if candidate is not None:
        runs["candidate"] = candidate
    start = first if first is not None else max(min(run)[0] for run in runs.values())
    end = last if last is not None else min(max(run)[1] for run in runs.values())
    if end <= start or (end - start) % 30:
        raise ValueError(
            f"no contiguous aligned 30-interval segment at frames {start}..{end}"
        )
    keys = sorted(k for k in control if k[0] >= start and k[1] <= end)
    n = (end - start) // 30
    if len(keys) != n or any(
        k != (start + 30 * i, start + 30 * (i + 1)) for i, k in enumerate(keys)
    ):
        raise ValueError(
            f"missing or overlapping control windows for frames {start}..{end}"
        )
    if candidate is not None:
        other = sorted(k for k in candidate if k[0] >= start and k[1] <= end)
        if keys != other:
            raise ValueError(
                f"candidate windows do not match control frames {start}..{end}"
            )
    rows = []
    for key in keys:
        row = {"from_frame": key[0], "to_frame": key[1]}
        for name, run in runs.items():
            row[name + "_ms"] = {
                p: run[key][f"cpu_present_{p}_us"] / 1000 for p in METRICS
            }
        if candidate is not None:
            row["candidate_minus_control_ms"] = {
                p: (
                    candidate[key][f"cpu_present_{p}_us"]
                    - control[key][f"cpu_present_{p}_us"]
                )
                / 1000
                for p in METRICS
            }
        rows.append(row)
    return {
        "measurement": "CPU interval between successful DXGI Present returns",
        "alignment": "frame numbers only; scene identity requires separate evidence",
        "from_frame": start,
        "to_frame": end,
        "window_count": n,
        "samples_per_window": 30,
        "excluded_window_count": {name: len(run) - n for name, run in runs.items()},
        "windows": rows,
    }


def compare_workload(samples, start, end):
    keys = sorted(
        {k for run in samples.values() for k in run if k[0] >= start and k[1] <= end}
    )
    rows = []
    for key in keys:
        row = {"after_frame": key[0], "through_frame": key[1]}
        row.update({name: run.get(key) for name, run in samples.items()})
        if "candidate" in samples:
            row["matching_counts"] = (
                row["control"] == row["candidate"]
                if row["control"] is not None and row["candidate"] is not None
                else None
            )
        rows.append(row)
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "control",
        type=Path,
        help="log file or session directory containing stdout/stderr",
    )
    parser.add_argument(
        "candidate", nargs="?", type=Path, help="omit to inspect the control alone"
    )
    parser.add_argument(
        "--first-frame", type=int, help="exact start of the first window"
    )
    parser.add_argument("--last-frame", type=int, help="exact end of the final window")
    args = parser.parse_args()
    try:
        labels = {"control": args.control}
        if args.candidate:
            labels["candidate"] = args.candidate
        counters = {name: {} for name in labels}
        runs = {
            name: read_run(path, queue_samples=counters[name])
            for name, path in labels.items()
        }
        report = frame_report(**runs, first=args.first_frame, last=args.last_frame)
        report["queue_workload_checkpoints"] = compare_workload(
            counters, report["from_frame"], report["to_frame"]
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(report, indent=2))
