#!/usr/bin/env python3
"""Card Desk price history, for the stock value chart.

Runs after build_catalog.py. Keeps a per-product near-mint price history from
START (about five months before this was added) to the latest TCGCSV update:
  - one point a week (every 7th day from START) for anything older than DAILY days
  - one point a day for the last DAILY days
Only products/printings worth $1 or more at some point are kept (cheaper cards
barely move a stock total, and the app falls back to their current price).

Past points come from TCGCSV's daily archives
  https://tcgcsv.com/archive/tcgplayer/prices-YYYY-MM-DD.ppmd.7z
The latest point comes from the prices this build just downloaded.
The previous history is read back from the live site (PREV_SITE), so each run
only downloads the archives it doesn't have yet.

Output (OUT = site/catalog):
  history/index.json   {"start", "dates": [YYYY-MM-DD...], "buckets", "products", "builtAt", "missing": [...]}
  history/{n}.json     {productId: {printing: [cents or null per date]}}, n = productId % BUCKETS

The history never blocks a deploy: on any error the previous history is kept
(or, on the very first run, the app simply shows the chart without past prices).
"""
import datetime as dt, glob, json, os, shutil, sys, tempfile, time, urllib.error, urllib.request

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "catalog")
START = dt.date(2026, 6, 4)
DAILY = 14
BUCKETS = 128
MIN_CENTS = 100
MAX_FETCH = 60          # archives per run (first run: ~35)
TIME_BUDGET = 20 * 60   # seconds spent downloading archives per run
ARCHIVE = "https://tcgcsv.com/archive/tcgplayer/prices-{}.ppmd.7z"
UA = {"User-Agent": "CardDesk-nightly/1.0 (personal inventory app; once daily)"}
HDIR = os.path.join(OUT, "history")


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get_json(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if i == tries - 1:
                raise
        except Exception:
            if i == tries - 1:
                raise
        time.sleep(2 * (i + 1))


def wanted_dates(latest):
    """Dates the history should hold, oldest first (latest included)."""
    out, d = set(), START
    while d <= latest:
        if (latest - d).days < DAILY or (d - START).days % 7 == 0:
            out.add(d)
        d += dt.timedelta(days=1)
    out.add(latest)
    return sorted(out)


def load_prev(base):
    """{date: {pid: {sub: cents}}} from the live site's history, plus its missing list."""
    if not base:
        return {}, []
    base = base.rstrip("/") + "/catalog/history/"
    bust = f"?t={int(time.time())}"
    try:
        idx = get_json(base + "index.json" + bust, tries=2)
    except Exception as e:
        log(f"  no previous history on the site ({e}); starting fresh")
        return {}, []
    dates = idx.get("dates") or []
    by_date = {d: {} for d in dates}
    try:
        for b in range(idx.get("buckets") or BUCKETS):
            for pid, subs in get_json(f"{base}{b}.json{bust}").items():
                for sub, arr in subs.items():
                    for d, v in zip(dates, arr):
                        if v is not None:
                            by_date[d].setdefault(int(pid), {})[sub] = v
    except Exception as e:
        log(f"  previous history incomplete ({e}); starting fresh")
        return {}, []
    log(f"  previous history: {len(dates)} dates, {idx.get('products')} products")
    return by_date, idx.get("missing") or []


def read_archive(day, cats):
    """{pid: {sub: cents}} for one archived day, only our categories. None if not archived."""
    import py7zr
    tmp = tempfile.mkdtemp(prefix="cd-hist-")
    try:
        path = os.path.join(tmp, "p.7z")
        req = urllib.request.Request(ARCHIVE.format(day.isoformat()), headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as fh:
                shutil.copyfileobj(r, fh)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        with py7zr.SevenZipFile(path) as z:
            names = [n for n in z.getnames() if n.count("/") == 3 and n.split("/")[1] in cats]
        with py7zr.SevenZipFile(path) as z:
            z.extract(path=tmp, targets=names)
        out = {}
        for f in glob.glob(os.path.join(tmp, "*", "*", "*", "prices")):
            if f.split(os.sep)[-3] not in cats:
                continue
            try:
                rows = json.load(open(f, encoding="utf-8")).get("results") or []
            except Exception:
                continue
            for x in rows:
                mk = x.get("marketPrice") or x.get("midPrice") or x.get("lowPrice")
                if mk is not None:
                    out.setdefault(x["productId"], {})[x.get("subTypeName") or "Normal"] = int(round(mk * 100))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def current_prices():
    out = {}
    for f in glob.glob(os.path.join(OUT, "prices", "*.json")):
        for pid, subs in json.load(open(f, encoding="utf-8")).items():
            out[int(pid)] = {s: int(round(v * 100)) for s, v in subs.items() if v is not None}
    return out


def write(by_date, missing):
    dates = sorted(by_date)
    keep = {}
    for i, d in enumerate(dates):
        for pid, subs in by_date[d].items():
            for sub, v in subs.items():
                keep.setdefault(pid, {}).setdefault(sub, [None] * len(dates))[i] = v
    os.makedirs(HDIR, exist_ok=True)
    for fn in os.listdir(HDIR):
        os.remove(os.path.join(HDIR, fn))
    buckets, n = {}, 0
    for pid, subs in keep.items():
        subs = {s: a for s, a in subs.items() if max((v for v in a if v is not None), default=0) >= MIN_CENTS}
        if subs:
            buckets.setdefault(pid % BUCKETS, {})[pid] = subs
            n += 1
    for b in range(BUCKETS):
        with open(os.path.join(HDIR, f"{b}.json"), "w", encoding="utf-8") as fh:
            json.dump(buckets.get(b, {}), fh, separators=(",", ":"))
    idx = {"start": START.isoformat(), "dates": dates, "buckets": BUCKETS, "products": n,
           "builtAt": int(time.time() * 1000), "missing": sorted(set(missing))}
    with open(os.path.join(HDIR, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(idx, fh, separators=(",", ":"))
    size = sum(os.path.getsize(os.path.join(HDIR, f)) for f in os.listdir(HDIR))
    log(f"  history: {len(dates)} dates ({dates[0]} … {dates[-1]}), {n} products, {size/1e6:.1f} MB")


def main():
    started = time.time()
    man = json.load(open(os.path.join(OUT, "manifest.json"), encoding="utf-8"))
    cats = {str(g["cat"]) for g in man["games"].values() if g.get("cat") and not g.get("error")}
    upd = (man.get("tcgcsvUpdated") or "")[:10]
    try:
        latest = dt.date.fromisoformat(upd)
    except ValueError:
        latest = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    log(f"Price history up to {latest}…")
    prev, missing = load_prev(os.environ.get("PREV_SITE", ""))
    want = wanted_dates(latest)
    by_date = {d: v for d, v in prev.items() if dt.date.fromisoformat(d) in want}
    by_date[latest.isoformat()] = current_prices()
    # don't retry days TCGCSV never archived, once they're a few days old
    todo = [d for d in reversed(want) if d.isoformat() not in by_date and d.isoformat() not in missing]
    fetched = 0
    for d in todo:
        if fetched >= MAX_FETCH or time.time() - started > TIME_BUDGET:
            log(f"  stopping early; {len(todo) - fetched} days left for the next run")
            break
        t = time.time()
        try:
            day = read_archive(d, cats)
        except Exception as e:
            log(f"  {d}: failed ({e}); will retry next run")
            continue
        fetched += 1
        if day is None:
            log(f"  {d}: not archived")
            if (latest - d).days > 3:
                missing.append(d.isoformat())
            continue
        by_date[d.isoformat()] = day
        log(f"  {d}: {len(day)} products ({time.time() - t:.0f}s)")
    missing = [m for m in missing if dt.date.fromisoformat(m) >= START]
    write(by_date, missing)
    log(f"  done in {time.time() - started:.0f}s")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # keep the site deploying; the app copes without history
        log(f"Price history not updated: {e}; keeping the live copy")
        shutil.rmtree(HDIR, ignore_errors=True)
        base = os.environ.get("PREV_SITE", "").rstrip("/")
        if base:
            try:
                os.makedirs(HDIR)
                idx = get_json(f"{base}/catalog/history/index.json")
                files = {"index.json": idx}
                for b in range(idx.get("buckets") or BUCKETS):
                    files[f"{b}.json"] = get_json(f"{base}/catalog/history/{b}.json")
                for fn, obj in files.items():
                    with open(os.path.join(HDIR, fn), "w", encoding="utf-8") as fh:
                        json.dump(obj, fh, separators=(",", ":"))
            except Exception as e2:
                log(f"  live copy not available either ({e2}); no history this time")
                shutil.rmtree(HDIR, ignore_errors=True)
