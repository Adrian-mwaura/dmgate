"""Merge answer caches from several machines (docs/DEPLOY.md).

Each GPU server keeps its own `results/llm-cache/<model>.jsonl`, and a results tarball carries the
server's whole cache. Unpacking two servers' tarballs over each other would let one cache replace
the other; merging keeps every answer instead. Entries are keyed by the exact request
(`CachedClient.key`): a key already in the destination keeps its answer, new keys are appended.
Two different answers for one key would mean two machines asked the same question; that is
reported, and the destination's answer stays.

    python -m dmgate.llm.merge_cache <source dir or .jsonl files...> --into results/llm-cache
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def merge(source: Path, destination: Path) -> tuple[int, int, int]:
    """Merge one cache file into another. Returns (added, already there, conflicting)."""
    existing: dict[str, list[str]] = {}
    if destination.is_file():
        for line in destination.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                existing[entry["key"]] = entry["raw"]
    added, same, conflicts, new_lines = 0, 0, 0, []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry["key"] not in existing:
            existing[entry["key"]] = entry["raw"]
            new_lines.append(json.dumps(entry, ensure_ascii=False))
            added += 1
        elif existing[entry["key"]] == entry["raw"]:
            same += 1
        else:
            conflicts += 1
    if new_lines:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(new_lines) + "\n")
    return added, same, conflicts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Merge answer caches by request key.")
    parser.add_argument("sources", nargs="+", type=Path, help="cache directories or .jsonl files")
    parser.add_argument("--into", type=Path, default=Path("results") / "llm-cache")
    args = parser.parse_args(argv)
    files = [f for s in args.sources for f in (sorted(s.glob("*.jsonl")) if s.is_dir() else [s])]
    for f in files:
        added, same, conflicts = merge(f, args.into / f.name)
        note = f", {conflicts} CONFLICTING (kept the existing answers)" if conflicts else ""
        print(f"{f.name}: {added} new, {same} already there{note}")


if __name__ == "__main__":
    main()
