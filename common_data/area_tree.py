"""
Visualise the area containment tree derived from the admin GeoJSON.

  python area_tree.py --geojson cyprus_admin.geojson
  python area_tree.py --geojson cyprus_admin.geojson --level-counts
  python area_tree.py --geojson cyprus_admin.geojson --orphans     # only show problems
  python area_tree.py --geojson cyprus_admin.geojson --grep Λακατ  # subtree(s) matching

Parenthood is computed the same way the resolver does it: a place's parent is
the SMALLEST higher-level polygon whose ring contains the place's centroid.
Prints an indented tree so you can see whether the relationships make sense.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path


# ---- geometry helpers (same approach as resolve.py) ----------------------

def rings(geom):
    if not geom:
        return []
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "Polygon" and c:
        return [c[0]]
    if t == "MultiPolygon" and c:
        return [poly[0] for poly in c if poly]
    return []


def centroid(rs):
    pts = [p for r in rs for p in r if len(p) >= 2]
    if not pts:
        return None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def bbox(rs):
    pts = [p for r in rs for p in r if len(p) >= 2]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def in_ring(x, y, ring):
    inside, n, j = False, len(ring), len(ring) - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi:
                inside = not inside
        j = i
    return inside


# ---- load places ---------------------------------------------------------

class Place:
    __slots__ = ("idx", "name_el", "name_en", "level", "rs", "cx", "cy",
                 "bb", "parent", "children")

    def __init__(self, idx, name_el, name_en, level, rs):
        self.idx = idx
        self.name_el = name_el
        self.name_en = name_en
        self.level = level
        self.rs = rs
        c = centroid(rs)
        self.cx, self.cy = (c if c else (None, None))
        self.bb = bbox(rs)
        self.parent = None
        self.children = []


def load(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    places = []
    for i, f in enumerate(data.get("features", [])):
        p = f.get("properties") or {}
        name_el = p.get("name") or p.get("name:el") or ""
        name_en = p.get("name:en") or p.get("int_name") or name_el or "?"
        try:
            level = int(p.get("admin_level"))
        except (TypeError, ValueError):
            level = 99
        rs = rings(f.get("geometry"))
        if not name_el and not name_en:
            continue
        places.append(Place(i, name_el, name_en, level, rs))
    return places


def assign_parents(places):
    """Parent = smallest-area higher-level polygon containing this centroid."""
    def area_of(bb):
        if not bb:
            return float("inf")
        return (bb[2] - bb[0]) * (bb[3] - bb[1])

    polys = [p for p in places if p.rs and p.bb]
    for p in places:
        if p.cx is None:
            continue
        best = None
        for q in polys:
            if q is p or q.level >= p.level:
                continue
            minx, miny, maxx, maxy = q.bb
            if not (minx <= p.cx <= maxx and miny <= p.cy <= maxy):
                continue
            if not any(in_ring(p.cx, p.cy, r) for r in q.rs):
                continue
            if best is None or area_of(q.bb) < area_of(best.bb):
                best = q
        p.parent = best
        if best:
            best.children.append(p)


# ---- output --------------------------------------------------------------

def label(p):
    en = f" / {p.name_en}" if p.name_en and p.name_en != p.name_el else ""
    geo = "" if p.rs else "  [no geometry]"
    return f"[L{p.level}] {p.name_el}{en}{geo}"


def print_tree(p, depth, out):
    out.append("    " * depth + label(p))
    for c in sorted(p.children, key=lambda c: (c.level, c.name_el)):
        print_tree(c, depth + 1, out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--geojson", type=Path, required=True)
    ap.add_argument("--level-counts", action="store_true",
                    help="just print how many features exist per admin_level")
    ap.add_argument("--orphans", action="store_true",
                    help="only list places with no parent / no geometry")
    ap.add_argument("--grep", help="only print subtrees whose name matches")
    args = ap.parse_args()

    places = load(args.geojson)

    if args.level_counts:
        by_level = defaultdict(int)
        for p in places:
            by_level[p.level] += 1
        print("features per admin_level:")
        for lvl in sorted(by_level):
            print(f"  L{lvl}: {by_level[lvl]}")
        return

    assign_parents(places)

    # health summary — the numbers that tell you if the tree makes sense
    no_geom = [p for p in places if not p.rs]
    orphans = [p for p in places if p.parent is None and p.level > 1 and p.rs]
    print(f"{len(places)} places | {len(no_geom)} without geometry | "
          f"{len(orphans)} orphaned (no parent found)\n", file=sys.stderr)

    if args.orphans:
        print("== places with no parent (level > 1) ==")
        for p in sorted(orphans, key=lambda p: (p.level, p.name_el)):
            print("  " + label(p))
        print("\n== places with no geometry (can't be placed) ==")
        for p in sorted(no_geom, key=lambda p: (p.level, p.name_el)):
            print("  " + label(p))
        return

    if args.grep:
        needle = args.grep.lower()
        matches = [p for p in places
                   if needle in p.name_el.lower() or needle in p.name_en.lower()]
        for p in matches:
            out = []
            # walk up to show ancestry, then print its subtree
            chain = []
            q = p
            while q:
                chain.append(q)
                q = q.parent
            print(" ← ".join(label(c) for c in chain))
            sub = []
            print_tree(p, 0, sub)
            print("\n".join("    " + line for line in sub))
            print()
        return

    # full forest: roots are places with no parent
    roots = [p for p in places if p.parent is None]
    out = []
    for r in sorted(roots, key=lambda p: (p.level, p.name_el)):
        print_tree(r, 0, out)
    print("\n".join(out))


if __name__ == "__main__":
    main()
