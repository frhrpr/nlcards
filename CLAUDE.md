# Dutch vocab flashcards

Spaced-repetition vocab trainer for one learner — the user, learning Dutch
for themselves. Repo: github.com/frhrpr/nlcards, served at
https://frhrpr.github.io/nlcards/

**Copied from the Polish trainer on 2026-09-11** — `~/projects/flashcards`,
github.com/frhrpr/flashcards, built for one of the user's students. That
repo's `CLAUDE.md` is the full record of *why* the engine is the way it is:
measurements, bugs, simulations. This file keeps the decisions and a line of
reasoning each; go there for the evidence. Where a comment in `index.html` or
`tools/` cites a date, a Polish word or "he", it is describing that deck.

**Do not change the Polish repo from here.** It is in daily use by a real
student. A fix worth having in both is ported by hand, deliberately, one
direction at a time.

Ear training (minimal pairs) was left out on purpose. The Polish repo has the
whole implementation if it is ever wanted — Dutch vowel contrasts
(`u`/`uu`/`ui`, `ij`/`ei`/`eu`, `g`/`ch` vs `k`) would be the candidates.

**Cards are made together, in session.** The user is the learner, so unlike
the Polish deck there is no teacher approving sentences for a student: the
user supplies or agrees words, Claude writes the notes, runs the tools and
pushes. The user cannot vouch for Dutch the way they vouched for Polish, so
say when a sentence is idiomatic-but-unusual, and prefer the plain everyday
phrasing.

## Layout

```
index.html          the whole app — one file, no build step, served by Pages
deck/notes.json     card content, fetched by the app at load
deck/vocab.csv      every word met, whether or not it has a card
media/audio/*.mp3   generated once and committed, never fetched per review
media/img/*.webp    one image per note, 800px
media/manifest.json provenance for every media file — source, licence, checks
media/ATTRIBUTION.md generated from the manifest; do not edit
tools/validate.py   run after every deck change; exits non-zero if unshippable
tools/smoke.mjs     renders every card face in node; run after any app change
tools/audio.py      Commons recordings (Nl-<word>.ogg) for words, TTS for sentences
tools/images.py     generate / fetch / assign images, and --check them
tools/review.py     builds the approval page; records approvals
tools/progress.py   how it's going; reads Firestore over REST
tools/withdraw.py   takes a word out of rotation, with a reason and a date
tools/deckio.py     shared loading, saving, attribution, deck_id() (not runnable)
```

Deliberately **no build pipeline**. Keep `index.html` one static file.

### notes.json

```jsonc
{ "id": "huis",             // [a-z0-9_]+, == note_id in vocab.csv; één → een
  "word": "huis",           // BARE — never "het huis"; see the article rule
  "article": "het",         // nouns only: "de" | "het" | null (deliberate)
  "gloss": "house", "pos": "noun", "ipa": "ɦœys",
  "note": null,             // English, short, only to separate confusables
  "image": "media/img/huis.webp", "image_alt": "a house",
  "audio": "media/audio/huis.mp3",
  "sentence": {
    "nl": "Het huis is groot.", "en": "The house is big.",
    "gap": "___ is groot.",         // gap + answer must rebuild nl exactly
    "answer": "het huis",           // lower-case; a sentence-initial blank is capitalised for you
    "answer_lemma": "huis",
    "lemmas": ["huis", "zijn", "groot"],   // every word used; all must be in vocab.csv
    "audio": "media/audio/huis__sentence.mp3" },
  "cards": ["recognition", "production", "listening"],
  "reviewed": false }       // true once a human has looked and listened
```

Media keys are **absent until the file exists**. A path that is set but
missing is an error; a missing key is only a to-do.

`vocab.csv` columns: `word, note_id, pos, flashcard, status, source, added,
notes`. `status` is `queued` → `known` → `carded`. `word` is bare (no
article), because it is what sentence `lemmas` are matched against.

## Data storage

- **Same Firebase project and collection as the Polish app**
  (`flashcards-f5b40`, collection `progress`), one document for this deck.
  Chosen because it cost nothing: the open rule
  `match /progress/{docId} { allow read, write: if true; }` already covers
  any new document id, and the config is the same web key. GitHub-as-storage
  was considered and rejected — a write token in every browser, a commit per
  grade, SHA conflicts across devices.
- **The deck id is `nl-<uuid>`**, minted by the app with that prefix, and
  lives in `.env` as `NL_UID` (gitignored — the link is the login, and the
  repo is public). The bookmark is
  `https://frhrpr.github.io/nlcards/?u=<NL_UID>`.
- **Every tool resolves its document through `deckio.deck_id()`**, which
  refuses any id without the `nl-` prefix. The same collection holds the
  Polish student's progress; a tool with a `--user` default (the Polish
  `withdraw.py` defaulted to `evert`) would be one slip from writing to it.
  `progress.py --list` shows only `nl-` documents.
- **localStorage key is `nl-vocab-uid`**, not the Polish `vocab-uid`. Both
  apps are served from `frhrpr.github.io`, one origin, one localStorage —
  the shared key would make each app adopt the other's identity.
- Document shape is the Polish one minus ear training: `cards`, `log`,
  `done`, `extra`, `withdrawn`, `logBytes`, `updated`. Writes are deltas
  (`updateDoc` on one field path + `arrayUnion` on the log).

## Dutch-specific decisions

- **De/het is vocabulary.** It cannot be derived from the word, so every
  noun carries `article`, the app shows `het huis` wherever the word is
  shown, and a noun's production card says "With de or het." `word` stays
  bare because it is the join key to `vocab.csv`, the lemma lists and the
  Commons filename. `validate.py` errors on a noun with no `article` key and
  on a `word` that starts with one; `null` is allowed but warned, for the
  rare noun that genuinely takes none.
- **Gap the article with the noun.** `Het ___ is groot.` gives the answer
  away; `___ is groot.` with answer `het huis` does not. `validate.py` warns
  when the word before a noun's gap is de/het/deze/die/dit/dat. An inflected
  adjective (`een grote tafel` / `een groot huis`) leaks it too and cannot be
  caught mechanically — watch for it.
- **Word audio is bare**, from Commons `Nl-<word>.ogg` where it exists. The
  article is on screen; recordings with it barely exist.
- **TTS words that look English.** The Polish deck hit this once (`lody`);
  Dutch will hit it constantly — `hand`, `kind`, `bed`, `arm`, `winkel`.
  All free-tier ElevenLabs voices are English built-ins. Prefer Commons for
  words, and listen to every synthesised word clip before approving.
- **`kind: "form"` notes are kept** for strong verbs, whose past forms
  (`liep`, `gelopen`) are vocabulary in their own right. Not for regular
  paradigms, and not for plurals — this is not a grammar trainer.
- **Separable verbs** (`opbellen` → `ik bel je op`): the lemma is the
  infinitive. Pick a card sentence that keeps it whole
  (`Ik moet je morgen opbellen.`) so the gap is one blank and one answer; a
  split sentence would need two blanks with different fillers, which
  `validate.py` cannot reconstruct. Mention the split in `note` if useful.

## Engine decisions carried over — don't relitigate without reason

Full reasoning in the Polish `CLAUDE.md`; one line each here.

- **SM-2-lite, two grades.** Ease 2.5, floor 1.3; intervals 1, 6, `ivl*ease`.
  Again costs 0.2 ease and splices the card back into today's queue.
- **Ease recovers only from the third consecutive correct answer** (+0.15).
- **Due dates jittered 5% (floor 1 day), intervals never.** `ivl` is read by
  retirement, the unlock gate and the maturity buckets.
- **State keyed `${noteId}__${cardType}`**; note ids are Firestore field
  paths, hence `[a-z0-9_]+`.
- **Siblings buried**; one new card per note per day.
- **Staged unlock counted in reps, not interval** — production at 2 correct
  recognition answers in a row, listening at 3.
- **Intake: 10 cards a day with 3 reserved for new words**
  (`NEW_CARDS_PER_DAY`, `NEW_WORDS_PER_DAY`). The lever for less time is the
  word count, never the sibling headroom. First session gets 10 flat.
- **Cards retire at 180 days** (`RETIRE_IVL`), derived, never stored.
- **Order shuffled, seeded on day + user id** — stable across a reload.
- **`priority: true`** pulls a note out of the bank first; nothing else.
- **`reviewed` must be exactly `true`** or the card is never introduced.
- **"Just reviews" session** records `vocab-light`, so skipping new cards is
  visible in `progress.py` rather than silently stalling the deck.
- **Extra study**: new cards save, practice reviews do not.
- **Test ids (`?u=test…`) never write**; `&peek` never writes. Every write
  goes through the one `write` binding.
- **Withdrawing a word** (`withdraw.py`) deletes its card state, keeps the
  log, records why. Do not card a semantic set in one batch — two at a time,
  extremes first (`altijd`/`nooit` before `vaak`/`soms`/`meestal`).
- **A note drops its production card when the English has two Dutch
  answers.** Check existing glosses for collisions before adding a word.
- **Grammar words don't get cards** (`flashcard: no` in `vocab.csv`) but stay
  in the sentence allowlist.
- **A `note` must never name its own word** — it shows on the production
  front. `smoke.mjs` checks.

## Making cards — the workflow

1. Agree words. Record each in `vocab.csv` (`queued`, bare word, `source`,
   `added` date). Borrowing an unmet everyday word into a sentence is fine —
   record it as `queued` and it turns up in `validate.py`'s `next` list.
2. Write the notes: gloss, pos, article, IPA, a plain everyday sentence built
   from known words, gap, lemmas. `python3 tools/validate.py`.
3. Media: `python3 tools/audio.py` (dry run), then `--go`; images via
   `tools/images.py` (see its docstring; Gemini, ~4 cents each). The image
   rules in the Polish `CLAUDE.md` hold: follow the sentence's scene when it is
   one plain action, no words in pictures but numerals are fine, models cannot
   count, do not keep regenerating a bad image.
4. `python3 tools/review.py` → look and listen → `--approve`.
5. `python3 tools/validate.py && node tools/smoke.mjs`, commit, push.

## Working notes

- **Run `node tools/smoke.mjs` after any change to `index.html`.**
  `node --check` parses but does not resolve names. Smoke slices regions out
  of `index.html` between literal markers; if a marker moves it fails loudly.
  It runs against the real deck *plus a built-in fixture deck* (`fx_` ids),
  because the behavioural tests need notes with every shape and the real deck
  started empty.
- **Fail loudly, dry-run anything that spends money**, verify rather than
  assume — all as in the Polish repo.
- **`audio.py --tts-words` without `--only` rebuilds every word** and clears
  `reviewed` on all of them. Scope it.
- **Generated pages are dark by default** (palette in `tools/review.py`);
  declare `color-scheme`; images get a near-white mount.
- Pushing and non-allowlisted network calls need `dangerouslyDisableSandbox`
  (the sandbox denies `~/.ssh`). Firestore REST works inside the sandbox.
- `.env` holds `ELEVENLABS_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`
  (copied from the Polish repo) and `NL_UID`. Never commit it.
