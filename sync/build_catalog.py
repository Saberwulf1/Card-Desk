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

Change check: if PREV_SITE is set (the live site's base URL), the previous
manifest and price files are downloaded and compared. The manifest records
  changed         True/False: did any price differ from the previous update
  changedCount    how many products' prices differ
  updateAt        when this price update ran (ms)
  pricesChangedAt when prices last actually changed (ms)
A build on a code push whose prices match the live site keeps the previous
update's status, so pushing code doesn't look like a fresh price update.
"""
import gzip, json, os, re, sys, time, unicodedata, concurrent.futures as cf
import urllib.request

BASE = "https://tcgcsv.com/tcgplayer"
GAMES = {  # key: (categoryId, label)
    "pokemon": (3, "Pokémon"),
    "pokemonjp": (85, "Pokémon Japan"),  # TCGplayer "Pokemon Japan"; id checked by name at build time
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


def dump_gz(path, obj):  # plain JSON (name kept from an older gzip version)
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


def fetch_prev(base):
    """Previous manifest + price buckets from the live site, or (None, None)."""
    if not base:
        return None, None
    base = base.rstrip("/") + "/catalog/"
    bust = f"?t={int(time.time())}"
    try:
        man = get(base + "manifest.json" + bust, tries=2)
        prices = {}
        for b in range(man.get("priceBuckets") or NBUCKET):
            for pid, v in get(f"{base}prices/{b}.json{bust}", tries=3).items():
                prices[int(pid)] = v
        return man, prices
    except Exception as e:
        print(f"  previous prices not available ({e}); can't tell what changed", file=sys.stderr)
        return None, None


def price_check(manifest, prev_man, prev_prices):
    now, event = manifest["builtAt"], os.environ.get("GITHUB_EVENT_NAME", "")
    cur = {pid: v for b in PRICES.values() for pid, v in b.items()}
    if prev_prices is None:
        manifest.update(changed=None, changedCount=None, updateAt=now, pricesChangedAt=now)
        return
    n = sum(1 for pid in cur.keys() | prev_prices.keys() if cur.get(pid) != prev_prices.get(pid))
    if n == 0 and event == "push" and prev_man:
        # code push, same prices: keep the last real update's status
        for k in ("changed", "changedCount", "updateAt", "pricesChangedAt"):
            manifest[k] = prev_man.get(k)
        manifest["updateAt"] = manifest["updateAt"] or prev_man.get("builtAt")
        manifest["pricesChangedAt"] = manifest["pricesChangedAt"] or prev_man.get("builtAt")
        manifest["checkedOnPush"] = True
        return
    manifest.update(changed=n > 0, changedCount=n, updateAt=now,
                    pricesChangedAt=now if n else (prev_man or {}).get("pricesChangedAt") or (prev_man or {}).get("builtAt"))


def check_categories():
    """Confirm each categoryId by name on TCGCSV (fixes the id if TCGplayer renumbers)."""
    want = {"pokemonjp": "pokemon japan"}
    try:
        cats = {c["name"].strip().lower(): c["categoryId"] for c in get(f"{BASE}/categories")["results"]}
    except Exception as e:
        print(f"  couldn't list categories ({e}); using built-in ids", file=sys.stderr)
        return
    for key, name in want.items():
        if name in cats and cats[name] != GAMES[key][0]:
            print(f"  {key}: category id {GAMES[key][0]} -> {cats[name]}", file=sys.stderr)
            GAMES[key] = (cats[name], GAMES[key][1])


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
    check_categories()
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
    prev_man, prev_prices = fetch_prev(os.environ.get("PREV_SITE", ""))
    price_check(manifest, prev_man, prev_prices)
    print(f"Price check: changed={manifest.get('changed')} count={manifest.get('changedCount')}", file=sys.stderr)
    manifest["seconds"] = round(time.time() - started)
    with open(os.path.join(OUT, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, separators=(",", ":"), ensure_ascii=False)
    print(json.dumps({k: v for k, v in manifest.items() if k != "games"}))


if __name__ == "__main__":
    main()
