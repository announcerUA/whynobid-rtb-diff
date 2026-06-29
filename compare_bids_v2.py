#!/usr/bin/env python3
"""
compare_bids.py — Compare many OpenRTB bid-request files at once.

What it does
------------
1. Reads every *.json / *.txt / *.jsonl / *.ndjson file in a folder
   (recursively). A single file may contain one JSON object, a JSON array
   of objects, or one object per line (NDJSON) — all are handled.
2. Flattens each request to dot-paths (so nothing is missed).
3. Writes a per-request CSV of the fields that matter most for RTB
   debugging (bidfloor, banner size, api, os, app id/bundle, schain, ...).
4. Optionally splits requests into groups (e.g. "bids" vs "nobid") and
   RANKS every curated field by how strongly it separates the groups, so
   you immediately see the prime suspects for why one group bids and the
   other doesn't — even when no single field separates them perfectly.

Usage
-----
    # just tabulate everything in a folder
    python compare_bids.py /path/to/folder

    # tag files into groups by a substring in their filename, then compare.
    # format: --groups "substring=label,substring=label"
    python compare_bids.py /path/to/folder \
        --groups "Bid_request=bids,dsp_bid_request=nobid"

    # group by subfolder name instead of filename
    python compare_bids.py /path/to/folder --group-by-folder

    # mask IFA / IP / geo / user ids in the wide export
    python compare_bids.py /path/to/folder --redact

Options
-------
    --groups STR          comma list of substring=label rules
    --group-by-folder     group by containing subfolder instead of filename
    --redact              redact PII (ifa, ip, geo lat/lon, user ids) in wide CSV
    --top N               how many ranked fields to print (default 30)
    --outdir DIR          where to write CSVs (default ".")

Output
------
    bid_comparison.csv      one row per request, curated fields
    bid_flat_all.csv        one row per request, EVERY flattened field (wide)
    + a console report: numeric summaries and a field-separation ranking
"""

import argparse
import csv
import glob
import json
import math
import os
from collections import Counter, defaultdict


# Sentinel for "this field was absent". Using a unique object (not "" or
# None) guarantees an ABSENT field is never confused with a present-but-empty
# value during analysis. It is rendered as a blank cell in the CSVs.
MISSING = object()

# Leaf field names treated as personally identifying. When --redact is set,
# their values are masked in the wide CSV. `PII_LEAF` matches the final
# dot-path segment exactly; `PII_SUFFIX` matches the end of the full path.
PII_LEAF = {
    "ifa", "idfa", "aaid",
    "dpidsha1", "dpidmd5", "didsha1", "didmd5",
    "macsha1", "macmd5",
    "ip", "ipv6", "buyeruid",
}
PII_SUFFIX = ("geo.lat", "geo.lon", "user.id")


# ---- fields worth surfacing for RTB debugging (dot-path -> column name) ----
# imp0.* paths refer to the FIRST impression (imp[0]); imp_count flags multi-imp.
CURATED = {
    "id": "request_id",
    "at": "auction_type",
    "tmax": "tmax",
    "imp_count": "imp_count",            # derived (len of imp array)
    "imp0.media": "imp_media",           # banner / video / native (derived)
    "imp0.bidfloor": "bidfloor",
    "imp0.bidfloorcur": "floor_cur",
    "imp0.instl": "instl",
    "imp0.secure": "secure",
    "imp0.banner.w": "ban_w",
    "imp0.banner.h": "ban_h",
    "imp0.banner.api": "api",
    "imp0.banner.battr": "battr",
    "imp0.banner.pos": "pos",
    "imp0.displaymanager": "displaymanager",
    "imp0.displaymanagerver": "dm_ver",
    "imp0.tagid": "tagid",
    "app.id": "app_id",
    "app.bundle": "app_bundle",
    "app.name": "app_name",
    "app.publisher.id": "pub_id",
    "device.os": "os",
    "device.osv": "osv",
    "device.make": "make",
    "device.model": "model",
    "device.devicetype": "devicetype",
    "device.connectiontype": "conn",
    "device.lmt": "lmt",
    "device.dnt": "dnt",
    "regs.coppa": "coppa",
    "regs.ext.gdpr": "gdpr",
    "schain_last_asi": "schain_last",    # derived
}


# --------------------------------------------------------------------------- #
# Flattening / derived fields
# --------------------------------------------------------------------------- #
def flatten(obj, prefix=""):
    """Flatten a nested dict/list into {dot.path: scalar}. Lists of scalars
    are joined with ';'; lists of dicts are indexed (prefix[i])."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        if all(not isinstance(x, (dict, list)) for x in obj):
            out[prefix] = ";".join(str(x) for x in obj)
        else:
            for i, v in enumerate(obj):
                out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def imp_media(imp):
    for m in ("banner", "video", "native", "audio"):
        if m in imp:
            return m
    return ""


def schain_last_asi(req):
    src = req.get("source", {}) or {}
    sch = (src.get("ext", {}) or {}).get("schain") or src.get("schain") or {}
    nodes = sch.get("nodes", []) if isinstance(sch, dict) else []
    return nodes[-1].get("asi", "") if nodes else ""


def redact(flat):
    """Mask PII values in a flattened dict (in place) and return it."""
    for k in list(flat.keys()):
        leaf = k.split(".")[-1]
        if leaf in PII_LEAF or any(k.endswith(suf) for suf in PII_SUFFIX):
            flat[k] = "<redacted>"
    return flat


def build_rows(req, redact_pii=False):
    """Return (curated_row, wide_row) for one request dict.

    `wide` is the full flatten (every field), used for the wide CSV.
    `curated` is read from a lookup table that adds imp0-prefixed paths and
    a few derived fields, so curated columns stay stable regardless of how
    many impressions a request carries.
    """
    wide = flatten(req)
    wide["schain_last_asi"] = schain_last_asi(req)

    imps = req.get("imp") or []
    imp0 = imps[0] if imps else {}

    lookup = dict(wide)
    lookup.update(flatten(imp0, "imp0"))
    lookup["imp0.media"] = imp_media(imp0)
    lookup["imp_count"] = len(imps)

    curated = {col: lookup.get(path, MISSING) for path, col in CURATED.items()}

    if redact_pii:
        wide = redact(wide)
    return curated, wide


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def parse_requests(text):
    """Parse one file's text into a list of request dicts.

    Accepts a single JSON object, a JSON array of objects, or NDJSON
    (one JSON object per line)."""
    text = text.strip()
    if not text:
        return []
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return [o for o in obj if isinstance(o, dict)]
        if isinstance(obj, dict):
            return [obj]
        return []
    except json.JSONDecodeError:
        pass
    # fall back to NDJSON
    reqs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(o, dict):
            reqs.append(o)
    return reqs


def load_files(folder):
    """Return a list of (display_name, path, request_dict)."""
    paths = []
    for ext in ("*.json", "*.txt", "*.jsonl", "*.ndjson"):
        paths += glob.glob(os.path.join(folder, "**", ext), recursive=True)

    records = []
    for p in sorted(set(paths)):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            print(f"  [skip] {os.path.basename(p)}: {e}")
            continue
        reqs = parse_requests(text)
        if not reqs:
            print(f"  [skip] {os.path.basename(p)}: no JSON object found")
            continue
        base = os.path.basename(p)
        if len(reqs) == 1:
            records.append((base, p, reqs[0]))
        else:
            for i, r in enumerate(reqs):
                records.append((f"{base}#{i}", p, r))
    return records


def assign_group(path, group_rules, group_by_folder):
    if group_by_folder:
        return os.path.basename(os.path.dirname(path)) or "(root)"
    name = os.path.basename(path)
    for substr, label in group_rules:
        if substr in name:
            return label
    return "(ungrouped)"


# --------------------------------------------------------------------------- #
# Analysis helpers
# --------------------------------------------------------------------------- #
def value_key(v):
    """Normalize a curated value into a hashable bucket key. Absent stays the
    MISSING sentinel; everything else is stringified so 0 and '0' collapse."""
    return v if v is MISSING else str(v)


def value_label(key):
    if key is MISSING:
        return "«absent»"
    return key if key != "" else "«empty»"


def entropy(counts):
    counts = [c for c in counts if c]
    total = sum(counts)
    if total <= 0:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in counts)


def median(vals):
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return 0.0
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def numeric_summary(rows, columns):
    """Yield (col, {group: [floats]}) for curated columns that are mostly
    numeric. A column qualifies if >=60% of its present values parse as float."""
    for col in columns:
        per = defaultdict(list)
        n_total = n_parsed = 0
        for r in rows:
            v = r.get(col)
            if v is MISSING or v is None or v == "":
                continue
            n_total += 1
            try:
                per[r["group"]].append(float(v))
                n_parsed += 1
            except (TypeError, ValueError):
                pass
        if n_total and n_parsed >= max(1, int(0.6 * n_total)) and any(per.values()):
            yield col, per


def separation_ranking(rows, columns):
    """Rank curated columns by normalized information gain about the group.

    Returns (results, H_group) where results is a list of
    (norm_score, perfectly_separates, col, value->Counter(group)) sorted by
    score desc. norm_score is in [0, 1]; 1.0 means the field's value alone
    determines the group. `perfectly_separates` is the corrected condition:
    every observed value occurs in exactly one group (i.e. the groups are
    pairwise disjoint on this field) — this fixes the old all-groups
    intersection check, which wrongly flagged fields when A∩B∩C was empty
    but individual pairs still overlapped.
    """
    n = len(rows)
    group_totals = Counter(r["group"] for r in rows)
    h_group = entropy(group_totals.values())

    results = []
    for col in columns:
        vg = defaultdict(Counter)  # value_key -> Counter(group -> count)
        for r in rows:
            vg[value_key(r.get(col))][r["group"]] += 1
        if len(vg) <= 1:
            continue  # constant field carries no separating information

        h_cond = 0.0
        for gc in vg.values():
            n_v = sum(gc.values())
            h_cond += (n_v / n) * entropy(gc.values())

        norm = (h_group - h_cond) / h_group if h_group > 0 else 0.0
        perfect = all(len(gc) == 1 for gc in vg.values())
        results.append((norm, perfect, col, vg))

    results.sort(key=lambda t: (-t[0], t[2]))
    return results, h_group


def group_value_dist(vg):
    """Invert value->group->count into group->Counter(value)."""
    out = defaultdict(Counter)
    for val, gc in vg.items():
        for g, c in gc.items():
            out[g][val] += c
    return out


# --------------------------------------------------------------------------- #
# CSV writing
# --------------------------------------------------------------------------- #
def cell(v):
    """Render a curated value for CSV output (absent -> blank)."""
    return "" if v is MISSING else v


def write_csv(path, fieldnames, rows, render=False):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        if render:
            rows = [{k: cell(v) for k, v in r.items()} for r in rows]
        w.writerows(rows)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Compare many OpenRTB bid requests.")
    ap.add_argument("folder", help="Folder containing .json/.txt/.jsonl bid requests")
    ap.add_argument("--groups", default="",
                    help='Comma list of substring=label, e.g. "Bid_request=bids,dsp=nobid"')
    ap.add_argument("--group-by-folder", action="store_true",
                    help="Group files by their containing subfolder instead")
    ap.add_argument("--redact", action="store_true",
                    help="Mask PII (ifa, ip, geo lat/lon, user ids) in the wide CSV")
    ap.add_argument("--top", type=int, default=30,
                    help="How many ranked separating fields to print (default 30)")
    ap.add_argument("--outdir", default=".", help="Where to write CSVs")
    args = ap.parse_args()

    group_rules = []
    for tok in filter(None, (t.strip() for t in args.groups.split(","))):
        if "=" in tok:
            sub, lab = tok.split("=", 1)
            group_rules.append((sub.strip(), lab.strip()))

    records = load_files(args.folder)
    if not records:
        print("No parseable files found.")
        return
    print(f"Loaded {len(records)} request(s) from {args.folder}\n")

    curated_rows, flat_rows = [], []
    for name, path, req in records:
        crow, wide = build_rows(req, redact_pii=args.redact)
        g = assign_group(path, group_rules, args.group_by_folder)
        curated_rows.append({"file": name, "group": g, **crow})
        flat_rows.append({"file": name, "group": g, **wide})

    # ---- write curated CSV ----
    os.makedirs(args.outdir, exist_ok=True)
    cur_cols = ["file", "group"] + list(CURATED.values())
    cur_path = os.path.join(args.outdir, "bid_comparison.csv")
    write_csv(cur_path, cur_cols, curated_rows, render=True)

    # ---- write wide flattened CSV (every field) ----
    all_keys = set()
    for r in flat_rows:
        all_keys.update(r.keys())
    wide_cols = ["file", "group"] + sorted(k for k in all_keys if k not in ("file", "group"))
    wide_path = os.path.join(args.outdir, "bid_flat_all.csv")
    write_csv(wide_path, wide_cols, flat_rows)

    print(f"Wrote {cur_path}")
    print(f"Wrote {wide_path}\n")

    groups_order = sorted(set(r["group"] for r in curated_rows))

    # ---- numeric summaries (min / median / max) per group ----
    numeric = list(numeric_summary(curated_rows, list(CURATED.values())))
    if numeric:
        print("Numeric fields — min / median / max by group:\n")
        for col, per in numeric:
            print(f"  {col}:")
            for g in groups_order:
                vals = per.get(g)
                if not vals:
                    continue
                vals_sorted = sorted(vals)
                print(f"        {g:<12} n={len(vals):<4} "
                      f"min={vals_sorted[0]:.4f} median={median(vals):.4f} "
                      f"max={vals_sorted[-1]:.4f}")
        print()

    # ---- field-separation ranking (needs >=2 groups) ----
    if len(groups_order) >= 2:
        ranking, h_group = separation_ranking(curated_rows, list(CURATED.values()))
        print("Field separation ranking — how strongly each curated field "
              "predicts the group")
        print("(1.00 = the field's value alone tells the groups apart; "
              "0.00 = no signal)\n")

        shown = 0
        nonseparating = []
        for norm, perfect, col, vg in ranking:
            if norm <= 1e-9:
                nonseparating.append(col)
                continue
            mark = "  ★ perfectly separates" if perfect else ""
            print(f"  [{norm:0.2f}] {col}{mark}")
            dist = group_value_dist(vg)
            for g in groups_order:
                if g not in dist:
                    continue
                total = sum(dist[g].values())
                parts = ", ".join(f"{value_label(v)} ({c}/{total})"
                                  for v, c in dist[g].most_common(3))
                extra = "" if len(dist[g]) <= 3 else f", +{len(dist[g]) - 3} more"
                print(f"        {g:<12} {parts}{extra}")
            shown += 1
            if shown >= args.top:
                break

        if shown == 0:
            print("  (no curated field shows any separation between the groups)")
        elif nonseparating:
            print(f"\n  Fields that vary but do NOT separate the groups: "
                  f"{', '.join(nonseparating)}")
    else:
        print('Only one group present. Pass --groups to compare two sets, '
              'e.g. --groups "Bid_request=bids,dsp=nobid"')


if __name__ == "__main__":
    main()