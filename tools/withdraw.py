#!/usr/bin/env python3
"""Take a word out of rotation and put it back in the bank.

    python3 tools/withdraw.py --notes vaak,soms --why "interference"
    python3 tools/withdraw.py --notes vaak,soms --why "..." --go
    python3 tools/withdraw.py --list
    python3 tools/withdraw.py --restore vaak --go

Which document it touches is decided by deckio.deck_id(), and only ever an
nl- one: this Firestore collection also holds the Polish app's students.

The history below is from the Polish deck this tool was copied from.

Sometimes a word is not hard, it is *competing*. Six frequency adverbs were
carded and prioritised together on 2026-08-29 and arrived within days;
`często` fell to the ease floor on 1 of 8 while unrelated words from the same
week sat at 2.5. Nothing about `często` is difficult. It had five rivals.

Withdrawing deletes that note's card states, so the word returns to the bank
as unseen and is introduced again later by the ordinary shuffle. It does not
touch the review log: what he answered, he answered, and the streak, the day
counts and the accuracy must not move because of a scheduling decision.

## Why this is a tool and not a one-off edit

Because the alternative is a hand-written Firestore call that nobody can find
again, whose effects nobody can see, and which leaves `progress.py` reporting
a word as "giving him trouble" months after it was pulled. A withdrawal is a
teaching decision with a reason and a date; it belongs in the record.

So it writes `withdrawn: {"<noteId>": {at, why}}` on the student's document.
`progress.py` reads it, lists what is out, and ignores log entries older than
a withdrawal when it works out which cards are failing — otherwise a word's
old misses would follow it back into rotation and it would look broken on
arrival.

It also clears `priority` on the note. A prioritised word is pulled out of
the bank *first*, so withdrawing one without clearing the flag hands it back
the next morning — which is the opposite of a withdrawal and is exactly what
happened to `często` and `zwykle` on 2026-09-08.

Dry run by default. Deleting card state is not reversible from here: the
history stays in the log, but the intervals and ease are gone.
"""
import argparse, json, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deckio import ROOT, FS_BASE, web_key, deck_id

NOTES = ROOT / "deck" / "notes.json"
BASE = FS_BASE + "/"


def die(msg):
    sys.exit(f"withdraw: {msg}")


def get_doc(uid, key):
    try:
        with urllib.request.urlopen(f"{BASE}{uid}?key={key}", timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        die(f"could not read {uid}: HTTP {e.code} {e.read()[:200]}")


def num(v):
    for k in ("integerValue", "doubleValue"):
        if k in v:
            return int(v[k]) if k == "integerValue" else float(v[k])
    return None


def patch(uid, key, fields, delete_paths, body_paths):
    """One PATCH: `body_paths` are written, `delete_paths` are named in the
    mask but absent from the body, which is how Firestore deletes a field."""
    mask = "&".join("updateMask.fieldPaths=" + urllib.parse.quote(p)
                    for p in delete_paths + body_paths)
    url = f"{BASE}{uid}?key={key}&{mask}"
    req = urllib.request.Request(
        url, data=json.dumps({"fields": fields}).encode(), method="PATCH",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        die(f"write failed: HTTP {e.code} {e.read().decode('utf-8','replace')[:300]}")


def clear_priority(ids, go):
    """A withdrawn word must not be first out of the bank."""
    deck = json.loads(NOTES.read_text(encoding="utf-8"))
    hit = [n["word"] for n in deck["notes"]
           if n["id"] in ids and n.get("priority")]
    if not hit:
        return []
    if go:
        for n in deck["notes"]:
            if n["id"] in ids:
                n.pop("priority", None)
        NOTES.write_text(json.dumps(deck, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    return hit


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", default="", help="an nl- deck id; default: deckio.deck_id()")
    ap.add_argument("--notes", default="", help="comma-separated note ids")
    ap.add_argument("--why", default="", help="recorded alongside the withdrawal")
    ap.add_argument("--restore", default="", help="clear the record for these ids")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--go", action="store_true", help="actually write")
    args = ap.parse_args()

    key = web_key()
    args.user = deck_id(args.user)
    doc = get_doc(args.user, key)
    f = doc.get("fields", {})
    cards = f.get("cards", {}).get("mapValue", {}).get("fields", {})
    out = f.get("withdrawn", {}).get("mapValue", {}).get("fields", {})
    notes = {n["id"]: n for n in
             json.loads(NOTES.read_text(encoding="utf-8"))["notes"]}

    if args.list or not (args.notes or args.restore):
        if not out:
            print("nothing withdrawn")
            return 0
        print(f"withdrawn from {args.user}:\n")
        for nid in sorted(out):
            d = out[nid]["mapValue"]["fields"]
            at = num(d.get("at", {})) or 0
            why = d.get("why", {}).get("stringValue", "")
            back = any(k.rsplit("__", 1)[0] == nid for k in cards)
            print(f"  {notes.get(nid,{}).get('word', nid):<14} "
                  f"{time.strftime('%d %b %Y', time.localtime(at/1000))}"
                  f"  {'BACK IN ROTATION' if back else 'in the bank'}"
                  f"   {why}")
        return 0

    if args.restore:
        ids = [s.strip() for s in args.restore.split(",") if s.strip()]
        unknown = [i for i in ids if i not in out]
        if unknown:
            die(f"not withdrawn: {', '.join(unknown)}")
        print(f"clearing the withdrawal record for {', '.join(ids)}")
        print("  (the words are already back in the bank; this only removes")
        print("   the note that says why they were pulled)")
        if not args.go:
            print("\ndry run. Re-run with --go.")
            return 0
        keep = {k: v for k, v in out.items() if k not in ids}
        patch(args.user, key, {"withdrawn": {"mapValue": {"fields": keep}},
                               "updated": {"integerValue": str(int(time.time()*1000))}},
              [], ["withdrawn", "updated"])
        print("done")
        return 0

    ids = [s.strip() for s in args.notes.split(",") if s.strip()]
    unknown = [i for i in ids if i not in notes]
    if unknown:
        die(f"unknown note id(s): {', '.join(unknown)}")
    if not args.why:
        die("--why is required: a withdrawal without a reason is one nobody "
            "can review later")

    doomed = sorted(k for k in cards if k.rsplit("__", 1)[0] in ids)
    if not doomed:
        die(f"none of those has any card state — nothing to withdraw")

    print(f"withdrawing from {args.user}:\n")
    for nid in ids:
        mine = [k for k in doomed if k.rsplit("__", 1)[0] == nid]
        w = notes[nid]["word"]
        if not mine:
            print(f"  {w:<14} not in rotation, skipped")
            continue
        print(f"  {w:<14} {len(mine)} card(s):")
        for k in mine:
            d = cards[k]["mapValue"]["fields"]
            print(f"      {k.rsplit('__',1)[1]:<12} ivl {num(d.get('ivl',{})) or 0:>3}"
                  f"  ease {num(d.get('ease',{})) or 0:.2f}")
    prio = clear_priority(set(ids), False)
    if prio:
        print(f"\n  priority flag cleared: {', '.join(prio)}")
        print("  (a prioritised word comes out of the bank first — leaving the")
        print("   flag would hand it straight back tomorrow)")
    print(f"\n  reason recorded: {args.why!r}")
    print("  the review log is untouched — the streak and accuracy do not move")

    if not args.go:
        print("\ndry run. Re-run with --go.")
        return 0

    clear_priority(set(ids), True)
    now = int(time.time() * 1000)
    rec = dict(out)
    for nid in ids:
        rec[nid] = {"mapValue": {"fields": {
            "at": {"integerValue": str(now)},
            "why": {"stringValue": args.why}}}}
    patch(args.user, key,
          {"withdrawn": {"mapValue": {"fields": rec}},
           "updated": {"integerValue": str(now)}},
          [f"cards.{k}" for k in doomed], ["withdrawn", "updated"])

    after = get_doc(args.user, key).get("fields", {}) \
                .get("cards", {}).get("mapValue", {}).get("fields", {})
    left = [k for k in doomed if k in after]
    if left:
        die(f"these were not removed: {', '.join(left)}")
    print(f"\ndone — {len(doomed)} card state(s) deleted, "
          f"{len(ids)} word(s) back in the bank")
    return 0


if __name__ == "__main__":
    sys.exit(main())
