#!/usr/bin/env python3
"""Build the deck's audio: human recordings for words, TTS for sentences.

    python3 tools/audio.py            # dry run — says what it would do
    python3 tools/audio.py --go       # actually fetch and synthesise
    python3 tools/audio.py --go --only kot,pies
    python3 tools/audio.py --go --tts-words   # skip Commons, synthesise words

Why the split: a multilingual TTS model infers language from the text, so a
sentence gives it plenty of Dutch to lock onto but an isolated "hand" or
"kind" reads as English — and far more Dutch words than Polish ones are
spelled like English words, so this bites harder here. Wikimedia Commons has native-speaker recordings of
exactly those isolated words (and no sentences), so the two sources cover
each other's gap. Where no recording exists we fall back to TTS with the
language pinned, which also fixes it.

Safe to re-run. A clip is rebuilt only when its text, source, voice or model
changes. Nothing enters deck/notes.json until ffprobe confirms real audio,
and any note whose audio changes has `reviewed` cleared — new audio has not
been listened to yet.
"""
import argparse, hashlib, html, json, re, subprocess, sys, tempfile, time
import urllib.parse, urllib.request, urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deckio import load, save, ATTRIB_PATH, MANIFEST_PATH, NOTES_PATH, reorder

ROOT = Path(__file__).resolve().parent.parent
AUDIO_DIR = ROOT / "media/audio"
ENV_PATH = ROOT / ".env"

VOICE_ID = "ErXwobaYiN019PkySvjV"
VOICE_NAME = "Antoni"
# Sentences: highest quality, and enough context to infer Dutch unaided.
SENTENCE_MODEL = "eleven_multilingual_v2"
# Isolated words: language must be pinned, which multilingual_v2 cannot do.
WORD_MODEL = "eleven_turbo_v2_5"
WORD_LANG = "nl"

TTS_API = "https://api.elevenlabs.io/v1/text-to-speech/"
COMMONS_FILE = "https://commons.wikimedia.org/wiki/Special:FilePath/"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
UA = "nlcards-vocab/0.1 (https://github.com/frhrpr/nlcards; personal study tool)"

MIN_BYTES, MIN_SECONDS, PAUSE = 2000, 0.2, 0.6

# Post-processing. Commons recordings come from different contributors on
# different microphones — measured spread was 20 dB, with some clips peaking
# at 0.0 dBFS. Everything is levelled to the same target so the deck does not
# lurch in volume between a word and its sentence.
#
# Trimming happens AFTER levelling, deliberately: a fixed dB threshold applied
# to a clip whose whole content sits at -37 dB would eat the word. The
# threshold is conservative and 60 ms is kept either side, because words
# can open on a very quiet fricative (Dutch v, z and g all can) that a keener
# setting would clip. Anything losing more than TRIM_ALARM of its length is
# reported rather than silently accepted.
NORM_MEAN_DB, NORM_PEAK_DB = -19.0, -1.0
TRIM_DB, TRIM_PAD, TRIM_ALARM = -50, 0.06, 0.30
PROCESS_VERSION = 1     # bump to force every clip to be rebuilt


def die(msg): sys.exit(f"audio: {msg}")


def load_key():
    if not ENV_PATH.exists():
        die(f"no .env — create it with:\n  echo 'ELEVENLABS_API_KEY=your_key' > {ENV_PATH}")
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ELEVENLABS_API_KEY="):
            key = line.split("=", 1)[1].strip().strip("'\"")
            if key:
                return key
    die(".env has no ELEVENLABS_API_KEY value")


def fingerprint(*parts):
    """Covers source and model, so changing either invalidates the clip."""
    return hashlib.sha256("\0".join(map(str, parts)).encode("utf-8")).hexdigest()[:16]


def get(url, timeout=45):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def commons_meta(filename):
    """Licence and author, so ATTRIBUTION.md is generated rather than owed."""
    q = urllib.parse.urlencode({"action": "query", "titles": f"File:{filename}",
                                "prop": "imageinfo", "iiprop": "extmetadata|url",
                                "format": "json"})
    try:
        page = list(json.loads(get(f"{COMMONS_API}?{q}", 30))["query"]["pages"].values())[0]
        info = page["imageinfo"][0]
        m = info.get("extmetadata", {})
        author = re.sub(r"<[^>]+>", "", m.get("Artist", {}).get("value", "")).strip()
        return {"licence": m.get("LicenseShortName", {}).get("value", "unknown"),
                "author": html.unescape(author) or "unknown",
                "page": info.get("descriptionurl", "")}
    except Exception as e:
        return {"licence": "unknown", "author": "unknown", "page": "", "meta_error": str(e)}


def fetch_commons(word, dest_tmp):
    """Returns provenance, or None if Commons has no recording for this word."""
    # Commons' Dutch pronunciation files are named Nl-<word>.ogg. The word
    # is fetched bare — the article is shown on the card, not recorded.
    name = f"Nl-{word}.ogg"
    try:
        data = get(COMMONS_FILE + urllib.parse.quote(name))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"Commons HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Commons network error: {e.reason}") from None
    ogg = dest_tmp.with_suffix(".ogg")
    ogg.write_bytes(data)
    # -f mp3 is required: the temp file ends in .part, so ffmpeg cannot infer
    # the output format from the extension.
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(ogg),
                        "-codec:a", "libmp3lame", "-q:a", "5", "-f", "mp3", str(dest_tmp)],
                       capture_output=True, text=True)
    ogg.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg could not convert {name}: {r.stderr.strip()[:120]}")
    return {"source": "commons", "commons_file": name, **commons_meta(name)}


def synthesise(key, text, model, lang=None, seed=None):
    body = {"text": text, "model_id": model}
    if lang:
        body["language_code"] = lang
    # Same text and voice give near-identical audio every time, so re-requesting
    # a clip with odd prosody achieves nothing. A seed is the only handle on
    # that: a different one is a genuinely different take of the same line.
    # Deliberately NOT part of the fingerprint — putting it there would make
    # every clip stale the moment the default changed.
    if seed is not None:
        body["seed"] = seed
    req = urllib.request.Request(TTS_API + VOICE_ID, data=json.dumps(body).encode(),
                                 headers={"xi-api-key": key, "Content-Type": "application/json",
                                          "Accept": "audio/mpeg"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        try:
            detail = json.loads(detail)["detail"]["message"]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error: {e.reason}") from None


def measure(path):
    """mean and peak dBFS, via ffmpeg's volumedetect."""
    out = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", out)
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", out)
    if not mean or not peak:
        raise RuntimeError("could not measure loudness")
    return float(mean.group(1)), float(peak.group(1))


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def postprocess(src, dest):
    """Level to a common target, then trim leading/trailing silence.

    Returns (gain_db, seconds_before, seconds_after) for reporting.
    """
    mean, peak = measure(src)
    # Whichever constraint binds first: hit the target mean, but never let the
    # peak clip. A quiet clip is raised; a hot one is pulled down.
    gain = min(NORM_MEAN_DB - mean, NORM_PEAK_DB - peak)
    before = duration(src)
    trim = (f"silenceremove=start_periods=1:start_silence={TRIM_PAD}"
            f":start_threshold={TRIM_DB}dB:detection=peak")
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src),
         "-af", f"volume={gain:.2f}dB,{trim},areverse,{trim},areverse",
         "-codec:a", "libmp3lame", "-q:a", "5", "-f", "mp3", str(dest)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg post-processing failed: {r.stderr.strip()[:140]}")
    return gain, before, duration(dest)


def verify(path):
    """An mp3 that is really a JSON error page passes an existence check."""
    size = path.stat().st_size if path.exists() else 0
    if size < MIN_BYTES:
        return f"only {size} bytes — an error response, not audio"
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        return f"ffprobe rejected it: {r.stderr.strip()[:120]}"
    try:
        if float(r.stdout.strip()) < MIN_SECONDS:
            return f"duration {r.stdout.strip()}s — too short to be real"
    except ValueError:
        return f"ffprobe gave no duration: {r.stdout.strip()[:80]}"
    return None


def plan(notes, only, tts_words):
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else {}
    jobs = []
    for n in notes:
        if only and n["id"] not in only:
            continue
        # A note can insist on TTS for its word. Without this the choice lives
        # only in the --tts-words flag, so the next ordinary run recomputes the
        # fingerprint expecting Commons, calls the clip stale and quietly
        # rebuilds it from the recording we rejected. That happened to piękny.
        want = "tts" if (tts_words or n.get("audio_tts")) else "commons"
        targets = [(n["word"], f"{n['id']}.mp3", n, "audio", "word", want)]
        s = n.get("sentence")
        if s and s.get("nl"):
            targets.append((s["nl"], f"{n['id']}__sentence.mp3", s, "audio", "sentence", "tts"))
        for text, name, holder, key, kind, want in targets:
            rel = f"media/audio/{name}"
            model = SENTENCE_MODEL if kind == "sentence" else WORD_MODEL
            fp = fingerprint(kind, want, text,
                             VOICE_ID if want == "tts" else "commons",
                             model if want == "tts" else "-",
                             PROCESS_VERSION, NORM_MEAN_DB, NORM_PEAK_DB, TRIM_DB)
            entry = manifest.get(rel)
            if entry and entry.get("fingerprint") == fp and (ROOT / rel).exists():
                if holder.get(key) != rel:
                    jobs.append(dict(rel=rel, text=text, holder=holder, key=key, kind=kind,
                                     want=want, fp=fp, note=n, relink=True))
                continue
            jobs.append(dict(rel=rel, text=text, holder=holder, key=key, kind=kind,
                             want=want, fp=fp, note=n, relink=False))
    return jobs, manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--go", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--seed", type=int, default=None,
                    help="ask for a different take of the same line; use with --only")
    ap.add_argument("--tts-words", action="store_true",
                    help="skip Commons and synthesise words too")
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    deck, notes, manifest_unused = load()
    if only:
        unknown = only - {n["id"] for n in notes}
        if unknown:
            die(f"unknown note id(s): {', '.join(sorted(unknown))}")

    jobs, manifest = plan(notes, only, args.tts_words)
    todo = [j for j in jobs if not j["relink"]]
    relink = [j for j in jobs if j["relink"]]

    print(f"words: {'TTS' if args.tts_words else 'Commons, falling back to TTS'}"
          f"  |  sentences: {SENTENCE_MODEL}, voice {VOICE_NAME}")
    if not todo and not relink:
        print("nothing to do — every clip is present and current")
        return 0
    for j in todo:
        print(f"  build  {j['rel']:<38} {('commons?' if j['want']=='commons' else 'tts'):<9} "
              f"{j['text'][:40]!r}")
    for j in relink:
        print(f"  relink {j['rel']:<38} (file already correct)")
    tts_chars = sum(len(j["text"]) for j in todo if j["want"] == "tts")
    print(f"\n{len(todo)} clip(s) to build; {tts_chars} TTS characters "
          f"(Commons words cost nothing)")
    if not args.go:
        print("\ndry run. Re-run with --go.")
        return 0

    key = load_key()
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    failed, changed, trimmed = [], set(), []
    counts = {"commons": 0, "tts": 0, "fallback": 0}

    for j in relink:
        j["holder"][j["key"]] = j["rel"]
        print(f"  relinked {j['rel']}")

    for i, j in enumerate(todo, 1):
        print(f"  [{i}/{len(todo)}] {j['rel']} ... ", end="", flush=True)
        raw = Path(tempfile.mkstemp(dir=AUDIO_DIR, suffix=".raw")[1])
        tmp = Path(tempfile.mkstemp(dir=AUDIO_DIR, suffix=".part")[1])
        prov, fell_back = None, False
        try:
            if j["want"] == "commons":
                prov = fetch_commons(j["text"], raw)
                if prov is None:            # no recording exists — pin the language
                    fell_back = True
                    raw.write_bytes(synthesise(key, j["text"], WORD_MODEL, WORD_LANG, args.seed))
                    prov = {"source": "tts", "voice": VOICE_ID, "voice_name": VOICE_NAME,
                            **({"seed": args.seed} if args.seed is not None else {}),
                            "model": WORD_MODEL, "language_code": WORD_LANG,
                            "why": "no Commons recording for this word"}
            else:
                model = SENTENCE_MODEL if j["kind"] == "sentence" else WORD_MODEL
                lang = None if j["kind"] == "sentence" else WORD_LANG
                raw.write_bytes(synthesise(key, j["text"], model, lang, args.seed))
                prov = {"source": "tts", "voice": VOICE_ID, "voice_name": VOICE_NAME,
                            **({"seed": args.seed} if args.seed is not None else {}),
                        "model": model, **({"language_code": lang} if lang else {})}
            gain, before, after = postprocess(raw, tmp)
        except RuntimeError as e:
            raw.unlink(missing_ok=True); tmp.unlink(missing_ok=True)
            print("FAILED")
            failed.append((j["rel"], str(e)))
            continue
        finally:
            raw.unlink(missing_ok=True)
        problem = verify(tmp)
        if problem:
            tmp.unlink(missing_ok=True)
            print("FAILED")
            failed.append((j["rel"], problem))
            continue
        cut = (before - after) / before if before else 0
        if cut > TRIM_ALARM:
            trimmed.append((j["rel"], before, after, cut))
        tmp.replace(ROOT / j["rel"])
        (ROOT / j["rel"]).chmod(0o644)
        manifest[j["rel"]] = {"fingerprint": j["fp"], "text": j["text"],
                              "gain_db": round(gain, 1),
                              "seconds": round(after, 2), **prov}
        j["holder"][j["key"]] = j["rel"]
        changed.add(j["note"]["id"])
        counts["fallback" if fell_back else prov["source"]] += 1
        print(f"ok  [{'TTS fallback' if fell_back else prov['source']}] "
              f"{gain:+.1f} dB, {before:.2f}s→{after:.2f}s")
        if prov["source"] == "tts":
            time.sleep(PAUSE)

    # New audio has not been listened to, so the note is no longer approved.
    for n in notes:
        if n["id"] in changed and n.get("reviewed"):
            n["reviewed"] = False
            print(f"  reviewed cleared for {n['id']} — audio changed")

    save(deck, notes, manifest)

    print(f"\n{counts['commons']} human recording(s), {counts['tts']} synthesised, "
          f"{counts['fallback']} synthesised because Commons had nothing")
    if trimmed:
        print(f"\n{len(trimmed)} clip(s) lost more than {TRIM_ALARM:.0%} to silence "
              f"trimming — listen to these before approving:")
        for rel, b, a, cut in trimmed:
            print(f"  {rel}  {b:.2f}s -> {a:.2f}s  ({cut:.0%} removed)")
    if failed:
        print(f"\n{len(failed)} FAILED — nothing written for these:")
        for rel, why in failed:
            print(f"  {rel}\n      {why}")
        print("\nRe-run to retry just these; everything else is saved.")
        return 1
    print("Run tools/review.py next, then tools/validate.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
