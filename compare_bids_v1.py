#!/usr/bin/env python3
"""
compare_bids.py — Compare many OpenRTB bid-request JSON files at once.

What it does
------------
1. Reads every *.json / *.txt file in a folder (recursively).
2. Flattens each request to dot-paths (so nothing is missed).
3. Writes a per-file CSV of the fields that matter most for RTB debugging
   (bidfloor, banner size, api, os, app id/bundle, schain, etc.).
4. Optionally splits files into groups (e.g. "bids" vs "204") and reports
   which fields PERFECTLY SEPARATE the groups — those are your prime suspects
   for why one group bids and the other doesn't.

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

Output
------
    bid_comparison.csv      one row per file, curated fields
    bid_flat_all.csv        one row per file, EVERY flattened field (wide)
    + a console report of separating / high-variance fields
"""

import argparse
import csv
import glob
import json
import os
from collections import defaultdict


# ---- fields worth surfacing for RTB debugging (dot-path -> column name) ----
# imp.* paths refer to the FIRST impression (imp[0]); most app traffic is single-imp.
CURATED = {
    "id": "request_id",
    "at": "auction_type",
    "tmax": "tmax",
    "imp0.media": "imp_media",          # banner / video / native (derived)
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
    "schain_last_asi": "schain_last",   # derived
}


def flatten(obj, prefix=""):
    """Flatten a nested dict/list into {dot.path: scalar}. Lists of scalars
    are joined; lists of dicts are indexed (only [0] kept for brevity here
    via the caller's curated map, but full index kept in the wide export)."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        # scalar list -> join; otherwise index each element
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


def curated_row(req):
    """Pull the curated fields out of one request dict."""
    flat = flatten(req)
    imp0 = (req.get("imp") or [{}])[0]
    imp0_flat = flatten(imp0, "imp0")
    imp0_flat["imp0.media"] = imp_media(imp0)
    flat.update(imp0_flat)
    flat["schain_last_asi"] = schain_last_asi(req)

    row = {}
    for path, col in CURATED.items():
        row[col] = flat.get(path, "")
    return row, flat


def load_files(folder):
    paths = []
    for ext in ("*.json", "*.txt"):
        paths += glob.glob(os.path.join(folder, "**", ext), recursive=True)
    records = []
    for p in sorted(set(paths)):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                req = json.load(fh)
        except Exception as e:
            print(f"  [skip] {os.path.basename(p)}: {e}")
            continue
        records.append((p, req))
    return records


def assign_group(path, group_rules, group_by_folder):
    name = os.path.basename(path)
    if group_by_folder:
        return os.path.basename(os.path.dirname(path)) or "(root)"
    for substr, label in group_rules:
        if substr in name:
            return label
    return "(ungrouped)"


def main():
    ap = argparse.ArgumentParser(description="Compare many OpenRTB bid requests.")
    ap.add_argument("folder", help="Folder containing .json/.txt bid requests")
    ap.add_argument("--groups", default="",
                    help='Comma list of substring=label, e.g. "Bid_request=bids,dsp=nobid"')
    ap.add_argument("--group-by-folder", action="store_true",
                    help="Group files by their containing subfolder instead")
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
    print(f"Loaded {len(records)} files from {args.folder}\n")

    curated_rows, flat_rows, groups = [], [], {}
    for path, req in records:
        crow, flat = curated_row(req)
        g = assign_group(path, group_rules, args.group_by_folder)
        crow = {"file": os.path.basename(path), "group": g, **crow}
        flat = {"file": os.path.basename(path), "group": g, **flat}
        curated_rows.append(crow)
        flat_rows.append(flat)
        groups[os.path.basename(path)] = g

    # ---- write curated CSV ----
    os.makedirs(args.outdir, exist_ok=True)
    cur_cols = ["file", "group"] + list(CURATED.values())
    cur_path = os.path.join(args.outdir, "bid_comparison.csv")
    with open(cur_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cur_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(curated_rows)

    # ---- write wide flattened CSV (every field) ----
    all_keys = set()
    for r in flat_rows:
        all_keys.update(r.keys())
    wide_cols = ["file", "group"] + sorted(k for k in all_keys if k not in ("file", "group"))
    wide_path = os.path.join(args.outdir, "bid_flat_all.csv")
    with open(wide_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=wide_cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(flat_rows)

    print(f"Wrote {cur_path}")
    print(f"Wrote {wide_path}\n")

    # ---- numeric summary for bidfloor per group (very handy) ----
    floors = defaultdict(list)
    for r in curated_rows:
        try:
            floors[r["group"]].append(float(r["bidfloor"]))
        except (TypeError, ValueError):
            pass
    if floors:
        print("Bid floor (USD CPM) by group:")
        for g, vals in floors.items():
            if vals:
                vals.sort()
                print(f"  {g:<12} n={len(vals):<4} min={vals[0]:.4f} "
                      f"median={vals[len(vals)//2]:.4f} max={vals[-1]:.4f}")
        print()

    # ---- separating-field report (only meaningful with >=2 groups) ----
    distinct_groups = sorted(set(groups.values()))
    if len(distinct_groups) >= 2:
        # for each curated field, collect the set of values seen per group
        per_field = defaultdict(lambda: defaultdict(set))
        for r in curated_rows:
            for col in CURATED.values():
                per_field[col][r["group"]].add(str(r.get(col, "")))

        print("Fields that PERFECTLY separate the groups (no shared values) —")
        print("these are the strongest suspects:\n")
        found = False
        for col, by_group in per_field.items():
            value_sets = [by_group.get(g, set()) for g in distinct_groups]
            # separating = every pair of groups has zero overlap
            overlap = set.intersection(*value_sets) if all(value_sets) else set()
            if not overlap and all(value_sets):
                found = True
                print(f"  • {col}")
                for g in distinct_groups:
                    sample = sorted(by_group[g])[:4]
                    print(f"       {g:<10} {sample}")
        if not found:
            print("  (none — no single curated field cleanly splits the groups)")
        print()

        # also show fields that merely vary, for context
        print("Other fields that vary across files (not group-separating):")
        for col, by_group in per_field.items():
            allvals = set().union(*by_group.values())
            if len(allvals) > 1:
                print(f"  - {col}: {len(allvals)} distinct values")
    else:
        print("Only one group present. Pass --groups to compare two sets, "
              "e.g. --groups \"Bid_request=bids,dsp=nobid\"")


if __name__ == "__main__":
    main()
