#!/usr/bin/env python3
"""How is the Dutch deck getting on?

    python3 tools/progress.py              # the nl- deck (see deckio.deck_id)
    python3 tools/progress.py --user ID    # a specific nl- deck
    python3 tools/progress.py --list       # the nl- decks that exist

Reads the live Firestore document over the REST API using the web API key
out of index.html — the security rule is open, so no credentials are needed.
Prints a summary and writes a page you can open in Windows.

Ordered by what is worth acting on: whether I am turning up at all, then
which words keep going wrong, then where I have got to in the deck. Spaced
repetition fails through absence far more often than through bad scheduling.
"""
import argparse, json, re, sys, urllib.request, urllib.error
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deckio import ROOT, out_dir, esc, FS_BASE as BASE, UID_PREFIX, web_key, deck_id

DAY = 86400_000
SESSION_GAP = 30 * 60_000        # a half-hour gap starts a new sitting
YOUNG, MATURE = 6, 21            # interval days: learning / young / mature


def die(msg): sys.exit(f"progress: {msg}")


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        die(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as e:
        die(f"network error: {e.reason}")


def dec(v):
    """Firestore REST wraps every scalar in a type tag; unwrap it."""
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return float(v["doubleValue"])
    if "stringValue" in v: return v["stringValue"]
    if "booleanValue" in v: return v["booleanValue"]
    if "nullValue" in v: return None
    if "mapValue" in v:
        return {k: dec(x) for k, x in (v["mapValue"].get("fields") or {}).items()}
    if "arrayValue" in v:
        return [dec(x) for x in (v["arrayValue"].get("values") or [])]
    return None


def load_remote(key, user):
    uid = deck_id(user)
    doc = get(f"{BASE}/{uid}?key={key}")
    fields = {k: dec(v) for k, v in doc.get("fields", {}).items()}
    # {noteId: {at, why}} — see tools/withdraw.py
    withdrawn = fields.get("withdrawn") or {}
    return (uid, fields.get("cards") or {}, fields.get("log") or [],
            fields.get("done") or {}, withdrawn)


def retire_ivl():
    """RETIRE_IVL out of index.html, for the same reason as intake_rate below:
    it is a number chosen by simulation and likely to be moved again, and a
    copy here would report cards as retired that the app still shows."""
    m = re.search(r"const RETIRE_IVL\s*=\s*(\d+)",
                  (ROOT / "index.html").read_text(encoding="utf-8"))
    if not m:
        die("could not read RETIRE_IVL out of index.html")
    return int(m.group(1))


RETIRE_IVL = None                # set from index.html on first use


def unlock_thresholds():
    """UNLOCK_REPS out of index.html — same reason as retire_ivl above."""
    m = re.search(r"const UNLOCK_REPS\s*=\s*\{([^}]*)\}",
                  (ROOT / "index.html").read_text(encoding="utf-8"))
    if not m:
        die("could not read UNLOCK_REPS out of index.html")
    return {k: int(v) for k, v in re.findall(r"(\w+)\s*:\s*(\d+)", m.group(1))}


def intake_rate():
    """NEW_WORDS_PER_DAY out of index.html, so this report cannot quietly
    disagree with the app about how fast the deck is consumed."""
    m = re.search(r"const NEW_WORDS_PER_DAY\s*=\s*(\d+)",
                  (ROOT / "index.html").read_text(encoding="utf-8"))
    return int(m.group(1)) if m else 3


def midnight(ms):
    d = datetime.fromtimestamp(ms / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


def sessions(log):
    """Split the review log into sittings on a half-hour gap."""
    out, cur = [], []
    for e in sorted(log, key=lambda x: x["ts"]):
        if cur and e["ts"] - cur[-1]["ts"] > SESSION_GAP:
            out.append(cur); cur = []
        cur.append(e)
    if cur:
        out.append(cur)
    return out


def analyse(cards, log, notes, done, withdrawn=None):
    global RETIRE_IVL
    RETIRE_IVL = retire_ivl()
    withdrawn = withdrawn or {}
    word = {n["id"]: n["word"] for n in notes}
    gloss = {n["id"]: n.get("gloss", "") for n in notes}
    note_of = lambda k: k.rsplit("__", 1)[0]
    type_of = lambda k: k.rsplit("__", 1)[1] if "__" in k else "?"

    by_day = defaultdict(lambda: {"good": 0, "again": 0})
    for e in log:
        by_day[midnight(e["ts"])][e["grade"]] += 1
    days = sorted(by_day)

    # Days he chose "just reviews". Worth watching: the option exists so a
    # tired evening still happens, but if it becomes the default the deck
    # quietly stops growing while the streak and the accuracy look fine.
    light = {int(d) for d, modes in done.items() if "vocab-light" in modes}

    today = midnight(int(datetime.now().timestamp() * 1000))
    streak = 0
    if days and days[-1] in (today, today - DAY):
        streak, prev = 1, days[-1]
        for d in reversed(days[:-1]):
            if prev - d != DAY: break
            streak += 1; prev = d

    vocab = list(log)
    # A withdrawn word's old answers must not follow it back into rotation.
    # The log is never edited — what he answered, he answered — so the cut is
    # made here at read time: entries from before a withdrawal describe a run
    # that has been abandoned, and counting them would report a word as
    # failing on the day it is reintroduced.
    vocab = [e for e in vocab
             if e["ts"] >= withdrawn.get(note_of(e["card"]), {}).get("at", 0)]
    lapses = Counter(note_of(e["card"]) for e in vocab if e["grade"] == "again")
    seen = Counter(note_of(e["card"]) for e in vocab)
    hard = sorted(((nid, lapses[nid], seen[nid]) for nid in lapses),
                  key=lambda r: (-r[1], -(r[1] / max(r[2], 1))))[:12]

    buckets = {"learning": 0, "young": 0, "mature": 0, "retired": 0}
    for st in cards.values():
        ivl = st.get("ivl", 0)
        if ivl >= RETIRE_IVL:
            buckets["retired"] += 1
            continue
        buckets["learning" if ivl < YOUNG else "young" if ivl < MATURE else "mature"] += 1

    # Cards one clean answer away from leaving. Worth seeing before they go,
    # because retirement is a claim rather than an observation: the app stops
    # asking, so nothing after this point will ever tell us it was wrong.
    nearly = sorted(
        ((k, st.get("ivl", 0)) for k, st in cards.items()
         if RETIRE_IVL > st.get("ivl", 0) >= RETIRE_IVL / 2.5),
        key=lambda r: -r[1])
    gone = sorted({note_of(k) for k, st in cards.items()
                   if st.get("ivl", 0) >= RETIRE_IVL})

    started_notes = {note_of(k) for k in cards}
    # How much new vocabulary is left. Conjugation drills are not new words —
    # they draw on the sibling allowance — so they are excluded, exactly as
    # the app excludes them from the daily word budget.
    rate = intake_rate()
    unseen = [n for n in notes
              if n.get("kind") != "form" and n["id"] not in started_notes]
    runway = len(unseen) / rate if rate else 0
    # A gated card is locked while its note's recognition card has not been
    # answered correctly enough times running. Per type, and counted in reps
    # exactly as index.html does — this used to approximate the gate with one
    # interval for both types, which is how the listening bug stayed invisible.
    unlock_reps = unlock_thresholds()
    locked, waiting = 0, []
    for n in notes:
        base = cards.get(f"{n['id']}__recognition")
        reps = base.get("reps", 0) if base else -1
        for ty in n.get("cards", []):
            if ty == "recognition" or f"{n['id']}__{ty}" in cards:
                continue
            if reps < unlock_reps.get(ty, 0):
                locked += 1
            elif base:
                # Unlocked, never introduced: it is behind the daily sibling
                # allowance rather than behind his own progress.
                waiting.append(f"{n['id']}__{ty}")

    # One row per card he has actually met. Unseen cards have no record, so
    # listing all 183 would bury the 22 that mean anything.
    hist = defaultdict(list)
    for e in sorted(vocab, key=lambda x: x["ts"]):
        hist[e["card"]].append(e)
    records = []
    for key, st in cards.items():
        h = hist.get(key, [])
        again = sum(1 for e in h if e["grade"] == "again")
        records.append({
            "key": key, "note": note_of(key), "type": type_of(key),
            "word": word.get(note_of(key), note_of(key)),
            "gloss": gloss.get(note_of(key), ""),
            "ivl": st.get("ivl", 0), "ease": st.get("ease", 0),
            "reps": st.get("reps", 0), "due": st.get("due"),
            "again": again, "seen": len(h),
            "history": [(e["ts"], e["grade"]) for e in h],
            "last": h[-1]["ts"] if h else None,
        })
    # Most trouble first: lapses, then the least-established cards.
    records.sort(key=lambda r: (-r["again"], r["ivl"], r["word"]))

    sess = sessions(log)
    lengths = [(s[-1]["ts"] - s[0]["ts"]) / 60000 for s in sess if len(s) > 1]
    hours = Counter(datetime.fromtimestamp(s[0]["ts"] / 1000).hour for s in sess)

    return dict(
        word=word, gloss=gloss, by_day=by_day, days=days, streak=streak, hard=hard,
        withdrawn=withdrawn, gone=gone, waiting=waiting,
        buckets=buckets,
        nearly=nearly, started=len(started_notes), total_notes=len(notes),
        locked=locked, cards_started=len(cards), sessions=sess,
        median_minutes=sorted(lengths)[len(lengths) // 2] if lengths else 0,
        usual_hour=hours.most_common(1)[0][0] if hours else None,
        last_seen=days[-1] if days else None, today=today,
        type_of=type_of, reviews=len(log), vocab_reviews=len(vocab), light=light,
        unseen=len(unseen), runway=runway, rate=rate,
        records=records, total_cards=sum(len(n.get("cards", [])) for n in notes),
    )


def fmt_day(ms): return datetime.fromtimestamp(ms / 1000).strftime("%a %d %b")


def strip_of(history):
    return "".join("v" if g == "good" else "x" for _, g in history)


def report(uid, a):
    p = print
    p(f"\n  deck {uid}\n")
    if not a["days"]:
        p("  nothing reviewed yet")
        return
    gap = (a["today"] - a["last_seen"]) // DAY
    when = "today" if gap == 0 else "yesterday" if gap == 1 else f"{gap} days ago"
    p(f"  last seen     {fmt_day(a['last_seen'])} ({when})")
    p(f"  streak        {a['streak']} day(s), active on {len(a['days'])} day(s)")
    p(f"  reviews       {a['reviews']} total, "
      f"median sitting {a['median_minutes']:.0f} min"
      + (f", usually around {a['usual_hour']:02d}:00" if a["usual_hour"] is not None else ""))
    p(f"  deck          {a['started']}/{a['total_notes']} words started, "
      f"{a['cards_started']} cards, {a['locked']} still locked")
    if a.get("waiting"):
        w = a["waiting"]
        kinds = Counter(k.rsplit("__", 1)[1] for k in w)
        p(f"                {len(w)} more are unlocked and waiting behind the "
          f"sibling allowance")
        p("                (" + ", ".join(f"{n} {t}" for t, n in kinds.most_common())
          + ") — this queue only drains if the allowance exceeds 2x the daily words")
    # Running out of new words is invisible from the review counts — they stay
    # healthy while the deck quietly stops growing — so it gets its own line.
    if a["runway"] < 4:
        p(f"  new words     {a['unseen']} left"
          + (f" — at {a['rate']}/day that is under a day. " if a["unseen"] else " — ")
          + "THE DECK NEEDS NEW WORDS.")
    else:
        p(f"  new words     {a['unseen']} left, about {a['runway']:.0f} days at {a['rate']}/day")
    b = a["buckets"]
    p(f"  maturity      {b['learning']} learning, {b['young']} young, {b['mature']} mature"
      + (f", {b['retired']} retired" if b["retired"] else ""))
    p("\n  recent days")
    for d in a["days"][-10:]:
        v = a["by_day"][d]
        tot = v["good"] + v["again"]
        rate = round(v["good"] / tot * 100) if tot else 0
        p(f"    {fmt_day(d)}  {tot:>3} reviews  {rate:>3}% first time  "
          + "█" * min(tot, 40) + ("  reviews only" if d in a["light"] else ""))
    recent = [d for d in a["days"][-14:]]
    n_light = sum(1 for d in recent if d in a["light"])
    if n_light:
        p(f"\n  reviews-only on {n_light} of my last {len(recent)} active day(s)"
          + ("  — no new words went in on those" if n_light < len(recent)
             else "  — ALL of them; nothing new has gone in"))
    if a.get("withdrawn"):
        p("\n  taken out of rotation")
        for nid, d in sorted(a["withdrawn"].items()):
            back = "back in rotation" if nid in {r["note"] for r in a["records"]} \
                   else "in the bank"
            p(f"    {a['word'].get(nid, nid):<14} {fmt_day(d.get('at', 0)):<13}"
              f"{back:<18}{d.get('why','')[:44]}")
    if a["buckets"]["retired"] or a.get("nearly"):
        b_ret = a["buckets"]["retired"]
        p(f"\n  retired at {RETIRE_IVL}+ days — never shown again")
        if b_ret:
            g = a["gone"]
            p(f"    {b_ret} card(s) across {len(g)} word(s): "
              + ", ".join(a["word"].get(n, n) for n in g[:10])
              + (" …" if len(g) > 10 else ""))
        else:
            p("    none yet")
        if a.get("nearly"):
            p("    one or two answers away:")
            for k, ivl in a["nearly"][:8]:
                nid = k.rsplit("__", 1)[0]
                p(f"      {a['word'].get(nid, nid):<14} "
                  f"{k.rsplit('__', 1)[1]:<12} {ivl:>4} d")
    if a["hard"]:
        p("\n  giving me trouble")
        for nid, miss, tot in a["hard"]:
            p(f"    {a['word'].get(nid, nid):<14} {a['gloss'].get(nid, ''):<18} "
              f"missed {miss} of {tot}")
    p("\n  every card I have met, worst first"
      "   (v = got it, x = missed, oldest on the left)")
    p(f"    {'word':<13} {'type':<12} {'ivl':>4} {'ease':>5}  {'due':<12} history")
    for r in a["records"]:
        strip = strip_of(r["history"])
        p(f"    {r['word']:<13} {r['type']:<12} {r['ivl']:>4} {r['ease']:>5.2f}  "
          f"{due_in(r['due'], a['today']):<12} {strip}")
    p("")


def due_in(due, today):
    if due is None: return "—"
    d = round((midnight(due) - today) / DAY)
    return "today" if d == 0 else "tomorrow" if d == 1 else \
           f"in {d} days" if d > 0 else f"{-d} day(s) late"


def hist_html(history):
    return "".join(
        f'<b class="{"g" if g == "good" else "a"}" title="'
        f'{datetime.fromtimestamp(ts/1000).strftime("%a %d %b %H:%M")}">'
        f'{"✓" if g == "good" else "✗"}</b>' for ts, g in history)


def html(uid, a, dest):
    dest.mkdir(parents=True, exist_ok=True)
    peak = max((v["good"] + v["again"] for v in a["by_day"].values()), default=1)
    rows = ""
    for d in a["days"][-30:]:
        v = a["by_day"][d]
        tot = v["good"] + v["again"]
        gw = v["good"] / peak * 100
        aw = v["again"] / peak * 100
        rows += (f'<tr><td class=d>{esc(fmt_day(d))}</td>'
                 f'<td class=bar><i style="width:{gw:.1f}%"></i>'
                 f'<u style="width:{aw:.1f}%"></u></td>'
                 f'<td class=n>{tot}</td>'
                 f'<td class=n>{round(v["good"]/tot*100) if tot else 0}%</td>'
                 f'<td class=g>{"reviews only" if d in a["light"] else ""}</td></tr>')
    rows_cards = ""
    for r in a["records"]:
        strip = hist_html(r["history"])
        late = r["due"] is not None and midnight(r["due"]) < a["today"]
        rows_cards += (
            f'<tr class="{"warn" if r["again"] >= 3 else ""}">'
            f'<td class=w>{esc(r["word"])}<em>{esc(r["gloss"])}</em></td>'
            f'<td class=ty>{esc(r["type"])}</td>'
            f'<td class=n>{r["ivl"]}</td>'
            f'<td class="n {"low" if r["ease"] <= 1.5 else ""}">{r["ease"]:.2f}</td>'
            f'<td class="n {"late" if late else ""}">{esc(due_in(r["due"], a["today"]))}</td>'
            f'<td class=hist>{strip}</td></tr>')
    hard = "".join(
        f'<tr><td class=w>{esc(a["word"].get(n, n))}</td>'
        f'<td class=g>{esc(a["gloss"].get(n, ""))}</td>'
        f'<td class=n>{m} of {t}</td></tr>' for n, m, t in a["hard"]) or \
        '<tr><td colspan=3 class=g>nothing yet</td></tr>'
    gap = (a["today"] - a["last_seen"]) // DAY if a["last_seen"] else None
    when = "—" if gap is None else "today" if gap == 0 else \
           "yesterday" if gap == 1 else f"{gap} days ago"
    b = a["buckets"]
    page = f"""<!doctype html><meta charset=utf-8><title>Progress — {esc(uid)}</title>
<meta name=color-scheme content="dark"><style>
:root{{--bg:#15171a;--card:#1d2024;--ink:#e6e8e6;--dim:#9aa0a6;--line:#2e3339;
--ok:#7cc088;--bad:#e8796b;--badbg:#2a1d1c}}
body{{font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;
background:var(--bg);color:var(--ink);line-height:1.5}}
h1{{font-size:1.15rem;margin-bottom:.2rem}}
h2{{font-family:ui-monospace,monospace;font-size:.7rem;letter-spacing:.14em;
text-transform:uppercase;color:var(--dim);margin:2rem 0 .6rem;font-weight:400}}
.sub{{font-size:.8rem;color:var(--dim);font-family:ui-monospace,monospace}}
.box{{background:var(--card);border:1px solid var(--line);padding:1rem 1.2rem}}
.kv{{display:grid;grid-template-columns:auto 1fr;gap:.4rem 1.2rem}}
.kv dt{{font-size:.85rem;color:var(--dim)}}
.kv dd{{margin:0;font-family:ui-monospace,monospace;font-size:.9rem;text-align:right}}
table{{width:100%;border-collapse:collapse;font-size:.85rem}}
td{{padding:.28rem .4rem;border-bottom:1px solid var(--line)}}
td.d{{width:7.5rem;color:var(--dim);font-size:.78rem}}
td.n{{text-align:right;font-family:ui-monospace,monospace;width:4.5rem}}
td.w{{font-size:1rem}} td.g{{color:var(--dim);font-size:.8rem}}
td.bar i,td.bar u{{display:inline-block;height:.7rem;vertical-align:middle}}
td.bar i{{background:var(--ok)}} td.bar u{{background:var(--bad)}}
table.cards th{{text-align:left;font-family:ui-monospace,monospace;font-size:.6rem;
letter-spacing:.1em;text-transform:uppercase;color:var(--dim);font-weight:400;
padding:.2rem .4rem;border-bottom:1px solid var(--line)}}
table.cards th:nth-child(n+3){{text-align:right}}
table.cards th:last-child{{text-align:left}}
tr.warn td{{background:var(--badbg)}}
td.w em{{display:block;font-style:normal;color:var(--dim);font-size:.72rem}}
td.ty{{font-family:ui-monospace,monospace;font-size:.68rem;color:var(--dim)}}
td.n.low{{color:var(--bad);font-weight:700}} dd.low{{color:var(--bad);font-weight:700}} td.n.late{{color:var(--bad)}}
td.n.diag{{color:var(--dim)}} td.n.hot{{color:var(--bad);font-weight:700}}
td.hist{{font-family:ui-monospace,monospace;letter-spacing:.08em;white-space:nowrap}}
td.hist b{{font-weight:400;cursor:default}}
td.hist b.g{{color:var(--ok)}} td.hist b.a{{color:var(--bad)}}
</style>
<h1>{esc(uid)}</h1>
<div class=sub>last seen {esc(when)} · {a['streak']}-day streak · {a['reviews']} reviews all told</div>
<h2>Where I am</h2>
<div class=box><dl class=kv>
<dt>Words started</dt><dd>{a['started']} of {a['total_notes']}</dd>
<dt>Cards in rotation</dt><dd>{a['cards_started']}</dd>
<dt>Still locked</dt><dd>{a['locked']}</dd>
<dt>New words left</dt><dd{' class=low' if a['runway'] < 4 else ''}>{a['unseen']}</dd>
<dt>Learning / young / mature</dt><dd>{b['learning']} / {b['young']} / {b['mature']}</dd>
<dt>Retired ({RETIRE_IVL}+ days)</dt><dd>{b['retired']}</dd>
<dt>Median sitting</dt><dd>{a['median_minutes']:.0f} min</dd>
</dl></div>
<h2>Turning up</h2>
<div class=box><table>{rows}</table></div>
<h2>Giving me trouble</h2>
<div class=box><table>{hard}</table></div>
<h2>Every card I have met — {len(a['records'])} of {a['total_cards']}</h2>
<div class=box><table class=cards>
<tr><th>word</th><th>type</th><th>ivl</th><th>ease</th><th>due</th>
<th>history — oldest first, hover for the date</th></tr>
{rows_cards}</table></div>
"""
    (dest / "progress.html").write_text(page, encoding="utf-8")
    return dest / "progress.html"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default="")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    key = web_key()

    if args.list:
        # Only this app's decks. The collection also holds the Polish app's
        # students, whose progress is none of this tool's business.
        docs = [d for d in get(f"{BASE}?key={key}").get("documents", [])
                if d["name"].rsplit("/", 1)[-1].startswith(UID_PREFIX)]
        print(f"{len(docs)} deck(s):")
        for d in docs:
            f = {k: dec(v) for k, v in d.get("fields", {}).items()}
            print(f"  {d['name'].split('/')[-1]:<40} "
                  f"{len(f.get('cards') or {}):>4} cards  {len(f.get('log') or []):>5} reviews")
        return 0

    uid, cards, log, done, withdrawn = load_remote(key, args.user)
    notes = json.loads((ROOT / "deck/notes.json").read_text(encoding="utf-8"))["notes"]
    a = analyse(cards, log, notes, done, withdrawn)
    report(uid, a)
    if a["days"]:
        print(f"  wrote {html(uid, a, out_dir('progress'))}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
