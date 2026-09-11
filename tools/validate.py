#!/usr/bin/env python3
"""Check deck/notes.json before it ships.

Errors (exit 1) are things that would break the app or teach something wrong.
Warnings (exit 0) are things worth a human look. Run it after every edit:

    python3 tools/validate.py
"""
import csv, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES, VOCAB = ROOT / "deck/notes.json", ROOT / "deck/vocab.csv"
MANIFEST = ROOT / "media/manifest.json"

CARD_TYPES = {"recognition", "production", "listening", "form"}
POS = {"noun", "verb", "adjective", "adverb", "preposition", "conjunction",
       "particle", "pronoun", "interjection", "numeral"}
ID_RE = re.compile(r"[a-z0-9_]+$")

# Sentence vocabulary IS checked, but not by guessing. The Polish version of
# this tool once tried to derive lemmas from surface forms and cried wolf on
# every correct sentence; Dutch has the same problem in smaller doses
# (liep→lopen, ging→gaan, and separable verbs split across the clause:
# "ik bel je op" is opbellen). So each sentence
# carries the lemmas it uses, written down when the sentence is written,
# because whoever chooses the words already knows them. That turns an
# unsolvable parsing problem into bookkeeping, and gives two things:
# every word he reads is one we have recorded, and the words he has already
# met in a sentence are exactly the queue for the next notes to make.

errors, warnings = [], []
def err(nid, msg): errors.append(f"{nid}: {msg}")
def warn(nid, msg): warnings.append(f"{nid}: {msg}")


def load():
    try:
        data = json.loads(NOTES.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"deck/notes.json is not valid JSON: {e}")
    if data.get("schema") != 1:
        sys.exit(f"unknown schema version: {data.get('schema')!r}")
    vocab = {}
    with VOCAB.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # Keyed by note_id, so a blank or repeated one silently drops the
            # row and its word stops existing as far as the lemma check is
            # concerned. Two blank ids once hid a word that was plainly there.
            nid = row["note_id"]
            if not nid:
                sys.exit(f"deck/vocab.csv: {row['word']!r} has no note_id")
            if nid in vocab:
                sys.exit(f"deck/vocab.csv: note_id {nid!r} used by both "
                         f"{vocab[nid]['word']!r} and {row['word']!r}")
            vocab[nid] = row
    return data["notes"], vocab


def fill(gap, answer):
    """Put the answer into every blank, capitalising one that opens the
    sentence. Plain str.replace cannot do this: it would write Być into both
    halves of `Być albo nie być`."""
    parts = gap.split("___")
    out = parts[0]
    for seg in parts[1:]:
        a = answer
        if not out:                       # this blank starts the sentence
            a = a[:1].upper() + a[1:]
        out += a + seg
    return out


def check_sentence(nid, s, word, vocab, kind=None, pos=None):
    for field in ("nl", "en", "gap", "answer", "answer_lemma"):
        if not s.get(field):
            err(nid, f"sentence.{field} is missing or empty")
            return
    if "___" not in s["gap"]:
        err(nid, "sentence.gap has no ___ placeholder")
    # More than one blank is allowed, but only when every blank takes the same
    # word — `Być albo nie być` gaps both. Reconstruction is what enforces
    # that: every ___ is filled with the one answer, so two blanks needing
    # different words cannot rebuild the sentence and fail here instead.
    #
    # A blank that opens the sentence takes the sentence's capital. That is
    # the same position-not-lexis argument that removed the sentence-initial
    # warning below: the capital is a property of where the word sits, not of
    # the word, and he writes the same thing either way. Without it the
    # Hamlet line cannot be gapped in its own order, since Być and być differ
    # only by that capital.
    # The gap filled with the answer must reproduce the sentence exactly, or
    # the student is shown one string and graded against another.
    if fill(s["gap"], s["answer"]) != s["nl"]:
        err(nid, "sentence.gap + answer does not reconstruct sentence.nl")
    # A sentence-initial gap used to warn here, on the theory that the answer's
    # capital is ambiguous. It is not worth a warning: the blank hides the
    # capital, so nothing leaks before he answers, and whether the capital is
    # positional or lexical changes nothing he would write. Polish drops
    # pronouns, so "Lubię mojego psa." cannot avoid starting with the verb —
    # the check fired on correct sentences, which is how warnings get ignored.
    #
    # What is worth catching is the same capital in a place position cannot
    # explain: a mid-sentence answer capitalised when the headword is not.
    # That is always a typo in the data.
    if (s["answer"][:1].isupper() and not word[:1].isupper()
            and not s["gap"].strip().startswith("___")):
        warn(nid, f"answer {s['answer']!r} is capitalised mid-sentence, but "
                  f"{word!r} is not a proper noun")
    # A noun's production card asks for de/het, so a gap that sits right after
    # the article (or a determiner that agrees with it) hands the answer over.
    # Gap the article together with the noun instead: gap "___ is groot.",
    # answer "Het huis". An inflected adjective leaks it too (een grote tafel
    # vs een groot huis), but that cannot be caught by looking one word back.
    before = s["gap"].split("___")[0].split()
    if pos == "noun" and before and before[-1].lower() in (
            "de", "het", "deze", "die", "dit", "dat"):
        warn(nid, f"the word before the gap ({before[-1]!r}) gives the article "
                  f"away — gap the article with the noun")
    if s["answer_lemma"] != word:
        warn(nid, f"answer_lemma {s['answer_lemma']!r} is not the note word {word!r}")

    lemmas = s.get("lemmas")
    if not lemmas:
        err(nid, "sentence has no lemmas list — record the words it uses")
        return
    # Cheap guard against a stale list: the answer must be one of them. A
    # conjugation drill is exempt — its answer_lemma is the inflected form it
    # teaches (mamy, jestem), which is deliberately not a vocab.csv word; the
    # parent verb appears in the lemmas list instead.
    if kind != "form" and s["answer_lemma"] not in lemmas:
        err(nid, f"sentence.lemmas does not include {s['answer_lemma']!r} — "
                 f"the list is stale, rewrite it for the current sentence")
    for lem in lemmas:
        if lem not in vocab:
            err(nid, f"sentence uses {lem!r}, which is not in deck/vocab.csv")


def main():
    notes, vocab = load()
    by_word = {row["word"] for row in vocab.values()}
    todo = {"image": [], "audio": [], "sentence audio": [],
            "listening audio": [], "review": []}
    manifest = {}
    if MANIFEST.exists():
        try:
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            errors.append(f"media/manifest.json is not valid JSON: {e}")
    sources = {"commons": 0, "tts": 0, "unrecorded": 0}

    seen = set()
    for n in notes:
        nid = n.get("id", "<no id>")
        if not ID_RE.fullmatch(nid):
            err(nid, "id must match [a-z0-9_]+ (it becomes a Firestore field path)")
        if nid in seen:
            err(nid, "duplicate id")
        seen.add(nid)

        for field in ("word", "gloss", "pos", "ipa", "sentence", "cards"):
            if not n.get(field):
                err(nid, f"{field} is missing or empty")

        if n.get("pos") not in POS:
            err(nid, f"pos must be one of {sorted(POS)}, got {n.get('pos')!r}")

        # de/het is vocabulary, not grammar: it cannot be derived from the
        # word, so every noun carries it and the app shows and asks for it.
        # A noun with no article (a plurale tantum, a name) must say so with
        # an explicit null rather than by leaving the key out, so a forgotten
        # article is an error and a deliberate absence is visible.
        if n.get("pos") == "noun":
            if "article" not in n:
                err(nid, "noun has no article — set \"de\" or \"het\" "
                         "(or null, deliberately, for a noun that takes none)")
            elif n["article"] not in ("de", "het", None):
                err(nid, f"article must be \"de\", \"het\" or null, got {n['article']!r}")
            elif n["article"] is None:
                warn(nid, "noun with article null — shown and asked without de/het")
        elif "article" in n:
            err(nid, f"article is set on a {n.get('pos')}, only nouns take one")
        if n.get("word", "").split(" ")[0] in ("de", "het"):
            err(nid, "word starts with an article — keep word bare and put "
                     "the article in the article field")

        for t in n.get("cards", []):
            if t not in CARD_TYPES:
                err(nid, f"unknown card type {t!r}")
        # A listening card needs word audio, but media arrives after the text,
        # so this is a to-do rather than an error — the app drops the card
        # type until the file exists (see NEEDS in index.html).
        if "listening" in n.get("cards", []) and "audio" not in n:
            todo["listening audio"].append(nid)
        # Only worth saying once a note is otherwise finished; before that the
        # "awaiting image" to-do already covers it.
        if (n.get("reviewed") and "production" in n.get("cards", [])
                and not n.get("image") and not n.get("note")):
            warn(nid, "production card has only the English gloss as a cue (no image)")

        if isinstance(n.get("sentence"), dict):
            check_sentence(nid, n["sentence"], n.get("word", ""), by_word,
                           n.get("kind"), n.get("pos"))

        # Media is absent until generated. A path that IS set must resolve,
        # and every clip must have provenance — without it, tools/audio.py
        # cannot tell a stale clip from a current one and will never rebuild.
        clips = [(key, n[key]) for key in ("image", "audio") if key in n]
        if isinstance(n.get("sentence"), dict) and "audio" in n["sentence"]:
            clips.append(("sentence.audio", n["sentence"]["audio"]))
        for key, rel in clips:
            if not (ROOT / rel).exists():
                err(nid, f"{key} points at a missing file: {rel}")
            elif key.endswith("audio"):
                entry = manifest.get(rel)
                if not entry:
                    err(nid, f"{key} has no entry in media/manifest.json — "
                             f"delete {rel} and re-run tools/audio.py")
                    sources["unrecorded"] += 1
                else:
                    sources[entry.get("source", "unrecorded")] = \
                        sources.get(entry.get("source", "unrecorded"), 0) + 1
                    if entry.get("text") != (n["word"] if key == "audio"
                                             else n["sentence"]["nl"]):
                        err(nid, f"{key} was generated from "
                                 f"{entry.get('text')!r}, which is no longer the text — "
                                 f"re-run tools/audio.py")

        # The app withholds any note whose reviewed flag is not exactly true,
        # so a missing or non-boolean flag silently removes a card from the
        # deck. Refuse it here rather than let it vanish quietly.
        if not isinstance(n.get("reviewed"), bool):
            err(nid, f"reviewed is {n.get('reviewed')!r}, must be true or false")

        if n.get("reviewed") and not (n.get("audio") and
                                      (n.get("sentence") or {}).get("audio")):
            err(nid, "marked reviewed but audio is missing")
        # Not-yet-generated media is normal, so it is counted rather than
        # listed — 30 lines of "no image yet" would bury a real problem.
        for key in ("image", "audio"):
            if key not in n:
                todo[key].append(nid)
        if isinstance(n.get("sentence"), dict) and "audio" not in n["sentence"]:
            todo["sentence audio"].append(nid)
        if "image" in n and not n.get("image_alt"):
            err(nid, "image without image_alt")

        if not n.get("reviewed"):
            todo["review"].append(nid)

        # Inflection drills are notes but not vocabulary: liep is a form of
        # lopen, not a separate word learned, so they stay out of vocab.csv.
        if n.get("kind") == "form":
            if not n.get("parent"):
                err(nid, "form note has no parent verb")
            elif n["parent"] not in {m["id"] for m in notes}:
                err(nid, f"parent {n['parent']!r} is not a note")
            if n.get("cards") != ["form"]:
                err(nid, "a form note should carry exactly the form card")
            continue

        row = vocab.get(nid)
        if row is None:
            err(nid, "not in deck/vocab.csv")
        else:
            if row["word"] != n.get("word"):
                err(nid, f"word {n.get('word')!r} disagrees with vocab.csv {row['word']!r}")
            if row["status"] != "carded":
                err(nid, f"vocab.csv status is {row['status']!r}, expected 'carded'")
            if row["flashcard"] != "yes":
                err(nid, "vocab.csv says flashcard=no but a note exists")

    for nid, row in vocab.items():
        if row["status"] == "carded" and nid not in seen:
            err(nid, "vocab.csv says carded but there is no note")

    # Everything decided-on but not yet carded: the queue for new notes.
    # Words already used in a sentence come first, because he is reading them
    # now — but the queue lists the rest too. It once watched sentences only,
    # and so lost sight of the seven day names that arrive on the tydzień card
    # image; a queue that quietly omits things is the failure it exists to
    # prevent.
    met = {lem for n in notes for lem in (n.get("sentence") or {}).get("lemmas", [])}
    wanted = {r["word"] for r in vocab.values()
              if r["flashcard"] == "yes" and r["status"] != "carded"}
    in_use = sorted(wanted & met)
    elsewhere = sorted(wanted - met)

    print(f"{len(notes)} notes checked\n")
    for w in warnings:
        print(f"  warn   {w}")
    if warnings:
        print()

    human = sources.get("commons", 0)
    synth = sources.get("tts", 0)
    if human or synth:
        print(f"  audio  {human} human recording(s), {synth} synthesised")
    # Priority goes inert once a word is introduced, so a stale flag does no
    # harm — but it should not be invisible either, or nobody remembers to
    # clear it and "prioritised" quietly comes to mean "everything".
    prio = [n["id"] for n in notes if n.get("priority")]
    if prio:
        print(f"  prio   {len(prio)} note(s) marked priority, out of the bank first: "
              f"{', '.join(prio)}")

    if in_use or elsewhere:
        print(f"  next   {len(in_use) + len(elsewhere)} word(s) wanted but not carded")
        if in_use:
            print(f"           in a sentence already: {', '.join(in_use)}")
        if elsewhere:
            print(f"           waiting             : {', '.join(elsewhere)}")
    for label, ids in todo.items():
        if not ids:
            continue
        shown = ", ".join(ids[:6]) + (f" +{len(ids) - 6} more" if len(ids) > 6 else "")
        print(f"  todo   {len(ids)}/{len(notes)} awaiting {label}: {shown}")
    if any(todo.values()):
        print()

    for e in errors:
        print(f"  ERROR  {e}")
    if errors:
        print(f"\n{len(errors)} error(s) — not safe to ship")
        return 1
    print(f"no errors ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
