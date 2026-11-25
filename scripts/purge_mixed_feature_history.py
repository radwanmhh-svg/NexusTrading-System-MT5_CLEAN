"""
One-time cleanup: purge schema-mismatched records from
``nexus:features:history:{symbol}``.

Background (2026-11-22): the training-history key must hold ONE feature
schema — the live M5 features (41 numeric features) appended by nexus-ml's
``_accumulate_training_history``. The old backfill ``_merge_h1_features``
mixed in H1 records with a different 24-feature schema, which made
``_parse_record`` pick the first record's keys as the training columns and
produced "RL feature mismatch: got 41 features, fitted on 24".

This script keeps only records whose numeric feature-key set matches the
dominant (live) schema and drops the rest. Dry-run by default:

    .venv\\Scripts\\python.exe scripts\\purge_mixed_feature_history.py           # report only
    .venv\\Scripts\\python.exe scripts\\purge_mixed_feature_history.py --apply   # actually delete
"""
from __future__ import annotations

import collections
import json
import os
import sys

import redis

SYMBOLS = [s.strip().upper() for s in os.getenv("STREAM_SYMBOLS", "XAUUSD,BTCUSD").split(",")]


def numeric_keys(record: dict) -> frozenset:
    feats = record.get("features", {})
    if not isinstance(feats, dict):
        return frozenset()
    return frozenset(k for k, v in feats.items() if isinstance(v, (int, float)))


def main() -> None:
    apply = "--apply" in sys.argv
    r = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"), decode_responses=True)

    for sym in SYMBOLS:
        key = f"nexus:features:history:{sym}"
        n = r.llen(key)
        if n == 0:
            print(f"{key}: empty")
            continue

        raw = r.lrange(key, 0, -1)
        schema_counter: collections.Counter = collections.Counter()
        parsed: list[tuple[str, dict, frozenset] | None] = []
        for blob in raw:
            try:
                rec = json.loads(blob)
                parsed.append((blob, rec, numeric_keys(rec)))
                schema_counter[parsed[-1][2]] += 1
            except (json.JSONDecodeError, TypeError):
                parsed.append(None)

        if not schema_counter:
            print(f"{key}: no parseable records")
            continue

        dominant, dominant_count = schema_counter.most_common(1)[0]
        print(f"{key}: total={n} schemas={len(schema_counter)}")
        for schema, cnt in schema_counter.most_common(5):
            label = "DOMINANT" if schema == dominant else "         "
            print(f"  {label} {cnt:6d} records, {len(schema)} numeric features: "
                  f"{sorted(schema)[:5]}...")

        # Safety: refuse to delete if the dominant schema holds less than 60%
        # of the data — that would mean the live schema is NOT the majority
        # and we should investigate instead of nuking records.
        if dominant_count < 0.6 * len(parsed):
            print(f"  SKIP {key}: dominant schema only {dominant_count}/{len(parsed)} "
                  f"(<60%) — manual review needed")
            continue

        keep, drop = [], []
        for item in parsed:
            if item is not None and item[2] == dominant:
                keep.append(item[0])
            else:
                drop.append(item)

        print(f"  keep={len(keep)} drop={len(drop)}")
        if not apply:
            print("  (dry-run — rerun with --apply to delete)")
            continue

        pipe = r.pipeline()
        pipe.delete(key)
        if keep:
            pipe.rpush(key, *keep)
        pipe.execute()
        print(f"  APPLIED: {key} rewritten with {len(keep)} live-schema records")

    print("done" + (" (applied)" if apply else " (dry-run)"))


if __name__ == "__main__":
    main()
