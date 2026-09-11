#!/usr/bin/env python3
"""Find candidate images, let a human choose, and wire the choice into the deck.

    python3 tools/images.py                    # status + what's in the inbox
    python3 tools/images.py --fetch            # download candidates, build the picker
    python3 tools/images.py --pick kot=3,dom=1 --go
    python3 tools/images.py --assign photo.jpg=kot --go     # your own file
    python3 tools/images.py --sheet            # contact sheet, to check the pairing

Candidates come from Openverse (CC-licensed, aggregates Flickr and Commons)
and each word's Wikipedia lead image. Neither needs an API key. Wikipedia is
precise when it hits and eccentric when it misses — "house" returns the
Katsura Imperial Villa — so it is offered as one option among several rather
than trusted.

Nothing is chosen automatically. The picker page shows the word, its example
sentence and the candidates side by side; you pick a number, or drop your own
file in the inbox if none of them are right.

Images are converted to WebP, capped in width and stripped of metadata.
Assigning one clears `reviewed` — a new picture has not been looked at.
"""
import argparse, base64, hashlib, json, shutil, subprocess, sys, time
import urllib.parse, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deckio import ROOT, load, save, out_dir, esc, load_key

IMG_DIR = ROOT / "media/img"
OPENVERSE = "https://api.openverse.org/v1/images/"
GEMINI = ("https://generativelanguage.googleapis.com/v1beta/models/"
          "{model}:generateContent?key={key}")
# Flash tier: a flashcard image needs to be legible at 800px on a phone, not
# gallery-grade. ~1290 output tokens per image, so roughly 4 euro cents.
GEN_MODEL = "gemini-2.5-flash-image"
# Vision model for the sanity check. Cheap enough (~1300 tokens per image,
# a small fraction of a cent) that every new image gets checked automatically.
CHECK_MODEL = "gemini-flash-latest"
WIKI_SUMMARY = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
UA = "nlcards-vocab/0.1 (https://github.com/frhrpr/nlcards; personal study tool)"

MAX_WIDTH, QUALITY = 800, 82
THUMB_WIDTH = 420          # candidates only need to be big enough to judge
MIN_SOURCE_WIDTH = 400
CANDIDATES = 5
PROCESS_VERSION = 1
EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}


def die(msg): sys.exit(f"images: {msg}")


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        w, h = r.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except ValueError:
        return None, None


def convert(src, dest, width=MAX_WIDTH):
    """Whatever came off the web -> a consistent, small, metadata-free WebP."""
    w, h = probe(src)
    if not w:
        raise RuntimeError("not a readable image (ffprobe found no video stream)")
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src),
         "-vf", f"scale='min({width},iw)':-2",
         "-c:v", "libwebp", "-quality", str(QUALITY), "-compression_level", "5",
         "-map_metadata", "-1", "-frames:v", "1", "-f", "image2", str(dest)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg could not convert it: {r.stderr.strip()[:140]}")
    return (w, h), probe(dest)


def query_for(n):
    """English gloss searches far better than Dutch. First sense only."""
    g = n.get("gloss", "").split(",")[0].strip()
    return g[3:] if g.startswith("to ") else g


def openverse(q, want):
    url = OPENVERSE + "?" + urllib.parse.urlencode({
        "q": q, "page_size": want, "license_type": "all-cc,commercial", "mature": "false"})
    try:
        data = json.loads(fetch(url))
    except Exception as e:
        print(f"      openverse failed: {e}")
        return []
    out = []
    for r in data.get("results", []):
        if not r.get("url"):
            continue
        out.append({"src": r["url"], "source": "openverse",
                    "title": r.get("title", ""), "author": r.get("creator") or "unknown",
                    "licence": (r.get("license") or "").upper(),
                    "source_url": r.get("foreign_landing_url", "")})
    return out


def wikipedia(q):
    for lang in ("en",):
        try:
            d = json.loads(fetch(WIKI_SUMMARY.format(
                lang=lang, title=urllib.parse.quote(q.replace(" ", "_")))))
        except Exception:
            continue
        src = (d.get("originalimage") or {}).get("source")
        if src:
            return [{"src": src, "source": "wikipedia", "title": d.get("title", q),
                     "author": "Wikipedia contributors", "licence": "see file page",
                     "source_url": (d.get("content_urls", {}).get("desktop", {})
                                    .get("page", ""))}]
    return []


def generate(key, prompt):
    """One image from one prompt. Returns PNG/JPEG bytes."""
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode()
    req = urllib.request.Request(GEMINI.format(model=GEN_MODEL, key=key), data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail)["error"]["message"]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail[:200]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from None
    parts = (d.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    imgs = [p for p in parts if "inlineData" in p]
    if not imgs:
        # The model sometimes refuses and answers in prose instead.
        text = next((p.get("text", "") for p in parts if p.get("text")), "")
        raise RuntimeError(f"no image returned: {text[:160] or 'empty response'}")
    return base64.b64decode(imgs[0]["inlineData"]["data"])


CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "shows_word": {"type": "boolean"},
        "unambiguous": {"type": "boolean"},
        "has_text": {"type": "boolean"},
        "problem": {"type": "string"},
    },
    "required": ["shows_word", "unambiguous", "has_text", "problem"],
}


def check_image(key, path, n):
    """Ask a vision model what is wrong with this image for this word.

    Specific yes/no questions beat "is this right?" — a model asked an open
    question tends to agree with whatever it is shown.
    """
    s = n.get("sentence") or {}
    # Only a picture that was built FROM the sentence should be judged against
    # it. For the rest the picture illustrates the word alone, and complaining
    # that the cat is not drinking milk is noise that buries real problems.
    from_sentence = n.get("image_basis") == "sentence"
    context = (f"The card's example sentence is: {s.get('nl','')} ({s.get('en','')}). "
               f"This picture was made to illustrate that sentence.\n\n"
               if from_sentence else
               f"The picture illustrates the word on its own. The card's example "
               f"sentence appears separately as text and audio, so do NOT judge "
               f"the picture against it.\n\n")
    # A card whose whole point is a numeral will always contain digits, and
    # the rule is that words are banned, not numerals. Without this every
    # number card flags and the flags stop meaning anything.
    numerals_ok = "only the numbers" in (n.get("image_prompt") or "")
    prompt = (
        f"This picture is the illustration on a vocabulary flashcard teaching an "
        f"adult beginner the Dutch word \"{n['word']}\", meaning \"{n.get('gloss','')}\".\n"
        + context +
        f"The card ALSO shows the English meaning, the Dutch word and the "
        f"example sentence. The picture is a memory hook, not a quiz: it does "
        f"not have to convey the meaning on its own, and a picture of an "
        f"action will always also contain the things performing it. Judge it "
        f"as a hook.\n\n"
        f"Answer strictly about what is visible:\n"
        f"- shows_word: is \"{n.get('gloss','')}\" actually depicted or clearly "
        f"implied? Answer true even if other things share the frame.\n"
        f"- unambiguous: would the picture actively MISLEAD — attach a wrong "
        f"meaning to the word? Answer false only for that. A picture that is "
        f"merely insufficient on its own is fine, because the gloss and the "
        f"sentence are there too.\n"
        + (f"- has_text: are there any WORDS, lettering or a watermark anywhere? "
           f"Digits alone are expected on this card and are not text — answer "
           f"false for a picture whose only marking is numerals.\n"
           if numerals_ok else
           f"- has_text: is there any written text, lettering or watermark anywhere?\n")
        +
        f"- problem: if anything is wrong, say so in one short sentence"
        + (", including any way the picture contradicts the sentence"
           if from_sentence else "") + ". Otherwise empty string.")
    body = {"contents": [{"parts": [
                {"inline_data": {"mime_type": "image/webp",
                                 "data": base64.b64encode(path.read_bytes()).decode()}},
                {"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseSchema": CHECK_SCHEMA}}
    req = urllib.request.Request(GEMINI.format(model=CHECK_MODEL, key=key),
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
        raw = d["candidates"][0]["content"]["parts"][0]["text"]
        v = json.loads(raw)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:160]}") from None
    except Exception as e:
        raise RuntimeError(f"unreadable check response: {str(e)[:120]}") from None
    failures = []
    if not v.get("shows_word"):
        failures.append("does not clearly show the word")
    if not v.get("unambiguous"):
        failures.append("ambiguous — something else dominates")
    if v.get("has_text"):
        failures.append("contains text or lettering")
    return {"verdict": "flag" if failures else "ok",
            "why": "; ".join(failures) or "",
            "problem": (v.get("problem") or "").strip(),
            "model": CHECK_MODEL}


def cmd_check(deck, notes, manifest, only, force):
    targets = [n for n in notes if n.get("image") and (ROOT / n["image"]).exists()
               and (not only or n["id"] in only)
               and (force or "check" not in manifest.get(n["image"], {}))]
    if not targets:
        print("nothing to check — every image has been checked "
              "(use --force to check again)")
        return 0
    key = load_key("GEMINI_API_KEY")
    flagged = []
    for i, n in enumerate(targets, 1):
        rel = n["image"]
        print(f"  [{i}/{len(targets)}] {n['id']:<10} ", end="", flush=True)
        try:
            res = check_image(key, ROOT / rel, n)
        except RuntimeError as e:
            print(f"CHECK FAILED — {e}")
            continue
        manifest.setdefault(rel, {})["check"] = res
        if res["verdict"] == "flag":
            flagged.append((n["id"], res))
            print(f"FLAG — {res['why']}")
            if res["problem"]:
                print(f"{'':<21}{res['problem']}")
        else:
            print("ok" + (f" — note: {res['problem']}" if res["problem"] else ""))
        time.sleep(0.3)
    save(deck, notes, manifest)
    if flagged:
        print(f"\n{len(flagged)} image(s) flagged. These are advisory, not errors — "
              f"look at them on the review page before approving.")
    return 0


def cmd_generate(deck, notes, manifest, dest, only, go):
    targets = [n for n in notes if "image" not in n and n.get("image_prompt")
               and (not only or n["id"] in only)]
    skipped = [n["id"] for n in notes
               if "image" not in n and not n.get("image_prompt")]
    if skipped:
        print(f"  no image_prompt, skipping: {', '.join(skipped)}")
    if not targets:
        print("nothing to generate")
        return 0
    for n in targets:
        print(f"  {n['id']:<10} {n['image_prompt'][:88]}")
    print(f"\n{len(targets)} image(s) via {GEN_MODEL}, roughly "
          f"{len(targets) * 4} euro cents")
    if not go:
        print("\ndry run. Re-run with --go.")
        return 0

    key = load_key("GEMINI_API_KEY")
    raw_dir = dest / "generated"
    raw_dir.mkdir(parents=True, exist_ok=True)
    plan, failed = [], []
    for i, n in enumerate(targets, 1):
        print(f"  [{i}/{len(targets)}] {n['id']} ... ", end="", flush=True)
        try:
            data = generate(key, n["image_prompt"])
        except RuntimeError as e:
            print("FAILED")
            failed.append((n["id"], str(e)))
            continue
        raw = raw_dir / f"{n['id']}.png"
        raw.write_bytes(data)
        print(f"ok ({len(data) // 1024} KB)")
        plan.append((raw, n, {"source": "generated", "model": GEN_MODEL,
                              "prompt": n["image_prompt"]}))
        time.sleep(0.5)
    if failed:
        print(f"\n{len(failed)} generation(s) FAILED:")
        for nid, why in failed:
            print(f"  {nid}\n      {why}")
    if not plan:
        return 1
    rc = adopt(deck, notes, manifest, plan, True)
    if rc == 0:
        print("\n  checking what came back:")
        cmd_check(deck, notes, manifest, {n["id"] for _, n, _ in plan}, force=True)
    return rc or (1 if failed else 0)


def cmd_fetch(notes, dest, only, count):
    cand_root = dest / "candidates"
    index = {}
    targets = [n for n in notes if "image" not in n and (not only or n["id"] in only)]
    if not targets:
        print("every note already has an image")
        return 0
    for n in targets:
        q = query_for(n)
        print(f"  {n['id']:<10} searching {q!r}")
        found = wikipedia(q) + openverse(q, count)
        d = cand_root / n["id"]
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        kept = []
        for c in found:
            if len(kept) >= count:
                break
            i = len(kept) + 1
            raw = d / f"raw{i}"
            try:
                raw.write_bytes(fetch(c["src"], 45))
                _, (nw, nh) = convert(raw, d / f"{i}.webp", THUMB_WIDTH)
            except Exception as e:
                print(f"      candidate {i} skipped: {str(e)[:70]}")
                raw.unlink(missing_ok=True)
                (d / f"{i}.webp").unlink(missing_ok=True)
                continue
            raw.unlink(missing_ok=True)
            kept.append({**c, "n": i, "file": f"{i}.webp", "size": f"{nw}x{nh}"})
            time.sleep(0.3)
        index[n["id"]] = kept
        print(f"      {len(kept)} candidate(s)")
    (cand_root / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    build_picker(notes, dest, index)
    return 0


def build_picker(notes, dest, index):
    blocks = ""
    for n in notes:
        if "image" in n:
            continue
        s = n.get("sentence") or {}
        cands = index.get(n["id"], [])
        thumbs = "".join(
            f'<figure><img src="candidates/{esc(n["id"])}/{esc(c["file"])}" alt="">'
            f'<figcaption><b>{c["n"]}</b> · {esc(c["source"])} · {esc(c["licence"] or "?")}'
            f'<br>{esc((c["title"] or "")[:44])}</figcaption></figure>' for c in cands)
        if not thumbs:
            thumbs = '<div class="none">nothing found — drop your own file in inbox/</div>'
        blocks += f'''<section>
  <h2>{esc(n['word'])} <span class=gl>{esc(n.get('gloss',''))}</span>
      <span class=id>{esc(n['id'])}</span></h2>
  <div class=sent>{esc(s.get('nl',''))}<em>{esc(s.get('en',''))}</em></div>
  <div class=row>{thumbs}</div></section>'''
    html = f'''<!doctype html><meta charset=utf-8><title>Pick images</title>
<meta name=color-scheme content="dark"><style>
:root{{--bg:#15171a;--card:#1d2024;--ink:#e6e8e6;--dim:#9aa0a6;--line:#2e3339;--bad:#e8796b;--badbg:#2a1d1c;--mount:#f4f5f2}}
body{{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;
background:var(--bg);color:var(--ink)}}
h1{{font-size:1.15rem}} p.i{{font-size:.9rem;color:var(--dim);max-width:38rem}}
section{{background:var(--card);border:1px solid var(--line);padding:1rem 1.2rem;margin:1.2rem 0}}
h2{{font-size:1.5rem;font-weight:300;margin:0 0 .3rem}}
.gl{{font-size:.9rem;color:var(--dim)}}
.id{{font-family:ui-monospace,monospace;font-size:.65rem;color:var(--dim);float:right}}
.sent{{font-size:1rem;margin-bottom:.8rem}}
.sent em{{display:block;font-style:normal;font-size:.82rem;color:var(--dim)}}
.row{{display:flex;gap:.7rem;overflow-x:auto;padding-bottom:.3rem}}
figure{{margin:0;flex:0 0 12rem}}
figure img{{width:12rem;height:9rem;object-fit:contain;background:var(--mount);
border:1px solid var(--line)}}
figcaption{{font-family:ui-monospace,monospace;font-size:.6rem;color:var(--dim);
margin-top:.25rem;line-height:1.4}}
figcaption b{{font-size:.9rem;color:var(--ink)}}
.none{{color:var(--bad);font-size:.85rem;padding:2rem 0}}</style>
<h1>Pick an image for each word</h1>
<p class=i>Candidates are CC-licensed, from Openverse and Wikipedia. Tell Claude the
number you want for each word — e.g. <b>kot 3, dom 1</b>. If none are any good,
put your own file in <b>inbox/</b> (any name, any format) and say so. The example
sentence is shown because an image that illustrates it is worth more than one that
only matches the word.</p>{blocks}'''
    (dest / "pick.html").write_text(html, encoding="utf-8")
    print(f"\nwrote {dest / 'pick.html'}")


def adopt(deck, notes, manifest, plan, go):
    """plan: list of (source_path, note, provenance dict)."""
    seen = {}
    for _, n, _ in plan:
        if n["id"] in seen:
            die(f"two images assigned to {n['id']}")
        seen[n["id"]] = True
    for src, n, _ in plan:
        w, h = probe(src)
        warn = "  (LOW RESOLUTION)" if w and w < MIN_SOURCE_WIDTH else ""
        print(f"  {src.name:<38} -> {n['id']:<10} {n['word']}   {w}x{h}{warn}")
    if not go:
        print(f"\n{len(plan)} image(s). Dry run — re-run with --go.")
        return 0

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    failed = []
    for src, n, prov in plan:
        rel = f"media/img/{n['id']}.webp"
        dest_p, tmp = IMG_DIR / f"{n['id']}.webp", IMG_DIR / f"{n['id']}.part"
        try:
            (ow, oh), (nw, nh) = convert(src, tmp)
        except RuntimeError as e:
            tmp.unlink(missing_ok=True)
            failed.append((src.name, str(e)))
            print(f"  FAILED {src.name}: {e}")
            continue
        tmp.replace(dest_p)
        dest_p.chmod(0o644)
        n["image"] = rel
        n.setdefault("image_alt", n["gloss"])
        n["reviewed"] = False
        manifest[rel] = {"fingerprint": hashlib.sha256(src.read_bytes()).hexdigest()[:16],
                         "original_size": f"{ow}x{oh}", "size": f"{nw}x{nh}",
                         "bytes": dest_p.stat().st_size,
                         "process_version": PROCESS_VERSION, **prov}
        print(f"  ok  {rel}  {ow}x{oh} -> {nw}x{nh}, {dest_p.stat().st_size // 1024} KB")
    save(deck, notes, manifest)
    if failed:
        print(f"\n{len(failed)} failed — nothing written for those.")
        return 1
    print("\nRun --sheet and check the pairing, then tools/validate.py.")
    return 0


def cmd_pick(deck, notes, manifest, dest, picks, go):
    idx_path = dest / "candidates" / "index.json"
    if not idx_path.exists():
        die("no candidates yet — run --fetch first")
    index = json.loads(idx_path.read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in notes}
    plan = []
    for pick in picks:
        if "=" not in pick:
            die(f"expected note_id=number, got {pick!r}")
        nid, num = (x.strip() for x in pick.split("=", 1))
        if nid not in by_id:
            die(f"unknown note id: {nid}")
        cands = {str(c["n"]): c for c in index.get(nid, [])}
        if num not in cands:
            die(f"{nid} has no candidate {num} (available: {', '.join(cands) or 'none'})")
        c = cands[num]
        src = dest / "candidates" / nid / c["file"]
        if not src.exists():
            die(f"candidate file vanished: {src}")
        plan.append((src, by_id[nid], {"source": c["source"], "title": c["title"],
                                       "author": c["author"], "licence": c["licence"],
                                       "source_url": c["source_url"]}))
    return adopt(deck, notes, manifest, plan, go)


def cmd_assign(deck, notes, manifest, inbox, pairs, go):
    by_id = {n["id"]: n for n in notes}
    plan = []
    for pair in pairs:
        if "=" not in pair:
            die(f"expected file=note_id, got {pair!r}")
        fname, nid = (x.strip() for x in pair.split("=", 1))
        src = inbox / fname
        if not src.exists():
            die(f"no such file in the inbox: {src}")
        if nid not in by_id:
            die(f"unknown note id: {nid}")
        plan.append((src, by_id[nid], {"source": "supplied", "original": fname,
                                       "author": "supplied by user", "licence": "n/a"}))
    return adopt(deck, notes, manifest, plan, go)


def cmd_sheet(notes, manifest, dest):
    dest.mkdir(parents=True, exist_ok=True)
    shot = dest / "check"
    shot.mkdir(exist_ok=True)
    cards = ""
    for n in notes:
        rel = n.get("image")
        if rel and (ROOT / rel).exists():
            shutil.copy2(ROOT / rel, shot / Path(rel).name)
            img = f'<img src="check/{esc(Path(rel).name)}" alt="">'
            m = manifest.get(rel, {})
            meta = f"{esc(m.get('source', '?'))} · {esc(m.get('size', '?'))}"
        else:
            img, meta = '<div class="none">no image</div>', "—"
        s = n.get("sentence") or {}
        cards += (f'<div class=c>{img}<div class=w>{esc(n["word"])}</div>'
                  f'<div class=g>{esc(n.get("gloss", ""))}</div>'
                  f'<div class=s>{esc(s.get("nl", ""))}<em>{esc(s.get("en", ""))}</em></div>'
                  f'<div class=m>{meta}</div></div>')
    html = f'''<!doctype html><meta charset=utf-8><title>Image check</title>
<meta name=color-scheme content="dark"><style>
:root{{--bg:#15171a;--card:#1d2024;--ink:#e6e8e6;--dim:#9aa0a6;--line:#2e3339;--bad:#e8796b;--badbg:#2a1d1c;--mount:#f4f5f2}}
body{{font-family:system-ui,sans-serif;max-width:54rem;margin:2rem auto;padding:0 1rem;
background:var(--bg);color:var(--ink)}}
h1{{font-size:1.15rem}} p{{font-size:.9rem;color:var(--dim)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(12rem,1fr));gap:1rem}}
.c{{background:var(--card);border:1px solid var(--line);padding:.6rem;text-align:center}}
.c img{{width:100%;height:8rem;object-fit:contain;background:var(--mount)}}
.none{{height:8rem;display:flex;align-items:center;justify-content:center;
background:var(--badbg);color:var(--bad);font-size:.8rem}}
.w{{font-size:1.15rem;font-weight:300;margin-top:.4rem}}
.g{{font-size:.8rem;color:var(--dim)}}
.s{{font-size:.78rem;margin-top:.35rem}}
.s em{{display:block;font-style:normal;color:var(--dim);font-size:.72rem}}
.m{{font-family:ui-monospace,monospace;font-size:.6rem;color:var(--dim);margin-top:.35rem}}
</style><h1>Image check — does each picture match its word and sentence?</h1>
<p>If any pairing is wrong, tell Claude which. This is the only place a
mismatched image gets caught before Evert sees it.</p>
<div class=grid>{cards}</div>'''
    (dest / "check.html").write_text(html, encoding="utf-8")
    print(f"wrote {dest / 'check.html'}")
    return 0


def cmd_status(notes, inbox, dest):
    missing = [n for n in notes if "image" not in n]
    print(f"{len(notes) - len(missing)}/{len(notes)} notes have an image\n")
    for n in missing:
        s = n.get("sentence") or {}
        print(f"  {n['id']:<10} {n['word']:<10} {n.get('gloss',''):<16} {s.get('nl','')}")
        print(f"  {'':<10} {'':<10} {'':<16} {s.get('en','')}")
    idx = dest / "candidates" / "index.json"
    if idx.exists():
        index = json.loads(idx.read_text(encoding="utf-8"))
        print(f"\ncandidates fetched for {len(index)} word(s) — see {dest / 'pick.html'}")
    files = sorted(p for p in inbox.glob("*") if p.is_file() and p.suffix.lower() in EXTS)
    print(f"\ninbox: {inbox}")
    if not files:
        print("  (empty — drop your own images here if the candidates are no good)")
    for p in files:
        w, h = probe(p)
        print(f"  {p.name:<44} {w}x{h}{'  << small' if w and w < MIN_SOURCE_WIDTH else ''}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generate", action="store_true",
                    help="generate images from each note's image_prompt")
    ap.add_argument("--fetch", action="store_true", help="download candidates")
    ap.add_argument("--pick", default="", help="comma-separated note_id=number")
    ap.add_argument("--assign", default="", help="comma-separated file=note_id")
    ap.add_argument("--check", action="store_true",
                    help="ask a vision model what is wrong with each image")
    ap.add_argument("--force", action="store_true", help="re-check already-checked images")
    ap.add_argument("--sheet", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--count", type=int, default=CANDIDATES)
    ap.add_argument("--go", action="store_true")
    args = ap.parse_args()

    deck, notes, manifest = load()
    dest = out_dir("images")
    inbox = dest / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    if args.generate:
        return cmd_generate(deck, notes, manifest, dest, only, args.go)
    if args.fetch:
        return cmd_fetch(notes, dest, only, args.count)
    if args.pick:
        return cmd_pick(deck, notes, manifest, dest,
                        [p for p in (s.strip() for s in args.pick.split(",")) if p], args.go)
    if args.assign:
        return cmd_assign(deck, notes, manifest, inbox,
                          [p for p in (s.strip() for s in args.assign.split(",")) if p], args.go)
    if args.check:
        return cmd_check(deck, notes, manifest, only, args.force)
    if args.sheet:
        return cmd_sheet(notes, manifest, dest)
    return cmd_status(notes, inbox, dest)


if __name__ == "__main__":
    sys.exit(main())
