#!/usr/bin/env python3
"""Card Desk nightly catalog build from TCGCSV (free nightly mirror of TCGplayer).

Downloads groups/products/prices for each game once, writes compact sharded
catalog files to OUT (default ./catalog) and a manifest.json. The app loads
only the shard(s) it needs by relative path. The app never calls TCGCSV.

Every word of a product name is indexed, so "charizard" finds "Dark Charizard".
Shards are plain JSON ({key}.json).

Shard row format (JSON array per product):
  [productId, name, number, rarity, groupId, {subType: marketPrice}]
Sets file per game: {groupId: [name, abbreviation, releaseYear]}
Price lookup (all games): prices/{productId % 32}.json = {productId: {subType: market}}
  used by the app to refresh inventory cards that carry a productId.
"""
import gzip, json, os, re, sys, time, unicodedata, concurrent.futures as cf
import urllib.request

BASE = "https://tcgcsv.com/tcgplayer"
GAMES = {  # key: (categoryId, label)
    "pokemon": (3, "Pokémon"),
    "yugioh": (2, "Yu-Gi-Oh!"),
    "onepiece": (68, "One Piece"),
    "gundam": (86, "Gundam"),
    "riftbound": (89, "Riftbound"),
}
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "catalog")
MAX_SHARD = 1_200_000  # raw bytes; bigger first-letter shards split by two letters
STOP = {"ex", "gx", "the", "of", "and", "with", "for", "de", "la", "le"}
PRICES, NBUCKET = {}, 32
UA = {"User-Agent": "CardDesk-nightly/1.0 (personal inventory app; once daily)"}


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def words(name):
    out = set()
    for w in re.split(r"[^a-z0-9]+", unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()):
        if len(w) >= 2 and w[0].isalpha() and w not in STOP:
            out.add(w)
    return out


def dump_gz(path, obj):  # plain JSON: the artifact host does not serve .gz files
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False)


def ext(p, field):
    for e in p.get("extendedData") or []:
        if e.get("name") == field:
            return e.get("value") or ""
    return ""


def fetch_group(cat, gid):
    prods = get(f"{BASE}/{cat}/{gid}/products").get("results") or []
    prices = get(f"{BASE}/{cat}/{gid}/prices").get("results") or []
    pm = {}
    for x in prices:
        mk = x.get("marketPrice") or x.get("midPrice") or x.get("lowPrice")
        if mk is not None:
            pm.setdefault(x["productId"], {})[x.get("subTypeName") or "Normal"] = round(mk, 2)
    rows = []
    for p in prods:
        rows.append([p["productId"], p.get("name") or "", ext(p, "Number"), ext(p, "Rarity"), gid, pm.get(p["productId"], {})])
    return rows


def build_game(key, cat):
    groups = get(f"{BASE}/{cat}/groups")["results"]
    sets, rows, errs = {}, [], 0
    for g in groups:
        pub = (g.get("publishedOn") or "")[:4]
        sets[g["groupId"]] = [g.get("name") or "", g.get("abbreviation") or "", int(pub) if pub.isdigit() else None]
    with cf.ThreadPoolExecutor(6) as ex:
        futs = {ex.submit(fetch_group, cat, g["groupId"]): g["groupId"] for g in groups}
        for f in cf.as_completed(futs):
            try:
                rows.extend(f.result())
            except Exception as e:
                errs += 1
                print(f"  {key} group {futs[f]} failed: {e}", file=sys.stderr)
    gdir = os.path.join(OUT, key)
    os.makedirs(gdir, exist_ok=True)
    for fn in os.listdir(gdir):
        os.remove(os.path.join(gdir, fn))
    dump_gz(os.path.join(gdir, "sets.json"), sets)

    rw = [(r, words(r[1])) for r in rows]
    by1 = {}
    for r, ws in rw:
        for k in {w[0] for w in ws} or {"_"}:
            by1.setdefault(k, []).append((r, ws))
    shards, raw = [], 0
    for k, items in sorted(by1.items()):
        size = sum(len(json.dumps(r, separators=(",", ":"), ensure_ascii=False)) for r, _ in items)
        if size <= MAX_SHARD or k == "_":
            parts = {k: [r for r, _ in items]}
        else:
            parts = {}
            for r, ws in items:
                for k2 in {w[:2] for w in ws if w[0] == k}:
                    parts.setdefault(k2, []).append(r)
        for sk, srs in parts.items():
            srs.sort(key=lambda r: (norm(r[1]), r[4]))
            dump_gz(os.path.join(gdir, sk + ".json"), srs)
            shards.append(sk)
    for r in rows:
        if r[5]:
            PRICES.setdefault(r[0] % NBUCKET, {})[r[0]] = r[5]
    return {"cat": cat, "products": len(rows), "groups": len(groups), "failedGroups": errs, "shards": sorted(shards)}


def main():
    started = time.time()
    os.makedirs(OUT, exist_ok=True)
    manifest = {"source": "tcgcsv.com", "games": {}}
    try:
        with urllib.request.urlopen(urllib.request.Request("https://tcgcsv.com/last-updated.txt", headers=UA), timeout=30) as r:
            manifest["tcgcsvUpdated"] = r.read().decode().strip()
    except Exception:
        pass
    ok = True
    for key, (cat, label) in GAMES.items():
        print(f"{label}…", file=sys.stderr)
        try:
            info = build_game(key, cat)
            info["label"] = label
            manifest["games"][key] = info
            print(f"  {info['products']} products, {len(info['shards'])} shards, {info['failedGroups']} failed groups", file=sys.stderr)
            if info["failedGroups"]:
                ok = False
        except Exception as e:
            ok = False
            manifest["games"][key] = {"cat": cat, "label": label, "error": str(e), "shards": []}
            print(f"  FAILED: {e}", file=sys.stderr)
    pdir = os.path.join(OUT, "prices")
    os.makedirs(pdir, exist_ok=True)
    for fn in os.listdir(pdir):
        os.remove(os.path.join(pdir, fn))
    for b in range(NBUCKET):
        dump_gz(os.path.join(pdir, f"{b}.json"), PRICES.get(b, {}))
    manifest["priceBuckets"] = NBUCKET
    manifest["builtAt"] = int(time.time() * 1000)
    manifest["ok"] = ok
    manifest["seconds"] = round(time.time() - started)
    with open(os.path.join(OUT, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, separators=(",", ":"), ensure_ascii=False)
    # file list for publish_files (patch mode): every catalog file, served under catalog/
    root = os.path.abspath(OUT)
    files = [{"as": "catalog/" + os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/"), "path": os.path.join(d, f)}
             for d, _, fs in os.walk(root) for f in sorted(fs) if f.endswith(".json") and f != "publish_files.json"]
    with open(os.path.join(root, "publish_files.json"), "w") as fh:
        json.dump(sorted(files, key=lambda x: x["as"]), fh)
    print(json.dumps({k: v for k, v in manifest.items() if k != "games"}))
    print(f"{len(files)} files listed in {os.path.join(root, 'publish_files.json')}")


if __name__ == "__main__":
    main()
