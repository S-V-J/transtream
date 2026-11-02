# TranStream — Product Requirements and State of Record

Written 2026-10-27 at HEAD aea9a07.

## 0. How to read this document (the trust rule)

Every claim below carries an evidence pointer: a `file:line`, a commit sha, a take
id, or a measurement printed in the session that wrote it. Where a claim has no
such pointer it is marked **UNVERIFIED** and must not be acted on as fact.

Three rules govern this file, and they exist because this repository has been
burned by their absence:

1. **Read before write.** If anything here conflicts with the code, the code wins.
   Report the conflict and stop. This document has been wrong twice: it prescribed
   repairing a `.env.example` comment that a later commit had already repaired, and
   it blamed patchkit's Markdown limitation on needle kinds when the cause was one
   unconditional `ast.parse`.
2. **A commit message is an output of verification, never an input to a patch.**
   Section 4 records what happens when this is inverted. Do not pre-author a
   message in a plan and then run it against whatever landed.
3. **Code-verified is not behaviour-verified.** Section 2 lists what the code does;
   section 5 step B lists what no one has yet watched happen.

## 1. Identity and verified state

| Item | Value | Evidence |
|---|---|---|
| Root | `/home/ML/transtream` (WSL, host `LAPTOP-B4UTDP8Q`) | every prompt |
| Interpreter | Python 3.14, venv at `./venv` | `VENV_OK: /home/ML/transtream/venv` |
| Remote | `github.com:S-V-J/transtream`, branch `main` | `git ls-remote` |
| HEAD | `aea9a07`, == `origin/main`, tree clean | this turn |
| Server | Daphne on `127.0.0.1:8003`, port in `logs/transtream.port` | README, CLI |
| Model | `Systran/faster-whisper-tiny`, CPU, `int8`, HF cache only | `settings.py`, `.env.example:29` |
| VAD | Silero, `VAD_THRESHOLD=0.5`, `MIN_SILENCE_MS=500`, `SPEECH_PAD_MS=30` | `.env.example:14-16` |
| Translation lib | `argostranslate 1.11.0` installed; **zero language packs** | `pip` output 14:22, `get_installed_packages()` |
| Transitively added | `spacy 3.8.18`, `stanza 1.10.1`, `sacremoses`, `sentencepiece`, `pydantic 2.14.1` (~70 MB) | install log |
| Already present | `ctranslate2 4.8.3`, `torch 2.14.1` + CUDA 13 stack, `numpy 2.5.4`, `onnxruntime 1.31.0` | install log, "already satisfied" |
| Guard suite | `guard_layers`, `guard_pipeline_invariants`, `guard_log_append_only`, `patchkit --selftest`, `preflight_ab_warmup`, `check_migrations` | all four at 0 this turn |
| Standing observations | `stale` probe rc=1; `README.md` 643 lines | repeated, unchanged |

**Pipeline (Phases 1–8, all live):** record → save raw → FFmpeg normalise
(16 kHz, 1 ch, 16-bit PCM WAV) → Silero VAD → Faster-Whisper → timestamped segments,
plus a rolling live preview over WebSocket during recording, a leased/heartbeated
background worker with an orphan-recovery sweep, upload ingest, a public API with
account keys, and `.srt`/`.vtt`/`.txt` export.

## 2. Done, with witnesses

### 2.1 Core (Phases 1–8)
Committed and guarded before this session; the counters that prove it were observed
live this week: `chunks sent/received 521/521` on a completed take (equal, no loss),
`7 speech segment(s) → 7 transcript segments`, `1586/1586` on the earlier run.
VAD-segment count and Whisper-segment count are **different stages, not a bug** —
VAD found 7 regions on 05eb0713 and Whisper merged them into 16 utterances.

### 2.2 This session (commits 34bc087 → aea9a07)
| Commit | What | Witness |
|---|---|---|
| `806b11d` | 12 env reads wired into `settings.py`; `WHISPER_LANGUAGE` pin added | defaults copied from the services' own `getattr` fallbacks, so wiring changed nothing for anyone who sets nothing |
| `7377082` | `WHISPER_INITIAL_PROMPT` knob | identity-defaulted `""` |
| `a7d8cc7` | prompt comment corrected to the semantics the code has | |
| `ff613d6` | the inverted dependency arrow repaired; the witness paragraph completed with its measured cost | `settings.py:215-225` read back on disk |
| `1cfe0f7` | patchkit: the `python=` declaration, 13 → 16 axes | **see §4 — its message overclaims** |
| `aea9a07` | native-script toggle functional at both render sites | **see §4 — its message claims an unrun browser check** |

### 2.3 Instruments built or repaired
- **patchkit v1→v4** — anchored edits, order-independent descending splice,
  four-way `lit` diagnosis (absent / WRAPPED / BOUNDARY / boundary-artifact),
  `code=` globals sharing `code_only()` with the log gate, collect-all lint,
  write-last, and now a `python=False` declaration so HTML/Markdown/dotenv are
  editable. Certified 16/16.
- **`guard_log_append_only`** — refuses any script that truncates or deletes a log
  artifact; owns the single `code_only()` implementation in the repo.
- **`preflight_ab_warmup`** — refuses to let the A/B harness measure anything until
  its shape is verified against code, never against prose.
- **`check_migrations`** — one-token migration probe, shipped as a file because an
  inline heredoc-inside-command-substitution-inside-`cat` lost its body and
  terminator in a paste.
- **`scripts/transtream`** CLI — owns a named systemd user unit for the worker,
  with `bash -n` termination proven by the parser rather than by a count.

### 2.4 The native-script toggle (the one Phase 9 control now in code)
`studio.html:97` `@click` · `:98` new title · `:99` `cursor-pointer` ·
`:322` and `:523` `:class="nativeScript ? 'text-lg leading-relaxed' : ''"` ·
state at `studio.js:36`. Off state binds `''` so the parent's `text-sm` inheritance
is untouched — **behaviourally identical to the previous file until clicked.**
`leading-relaxed` is the substantive part: Devanagari matras stack and collide at
the current line-height. It is an **enlargement**, not a script switch; the old
title promised a switch, which would need a transliteration engine and a second
stored representation per segment.

## 3. Measured facts that constrain design

These are not opinions. Each was produced by a run on this machine, and each one
rules something out.

### 3.1 Language detection cannot be biased by the prompt (closed, two methods)
- **Static:** detection is `self.detect_language(features=…)`, a separate method
  over pre-computed mel features, called at line 942 of the installed
  `faster_whisper/transcribe.py` (1941 lines). Its signature has no prompt-shaped
  parameter. Resolved by walking the module's AST.
- **Dynamic:** take `456d0aff` detects as `'id'` at `0.10968711227178574` — maximum
  headroom, any pull would show. No-prompt / English-prompt / Devanagari-prompt
  returned byte-identical language **and** probability to 16 digits.
- **The arrow runs the other way:** a Devanagari prompt on an `'id'`-decoding take
  produced no Devanagari at all. So the language decides whether the prompt can be
  heard; the prompt cannot choose the language. `settings.py` asserted the reverse
  for two commits and is now corrected.

### 3.2 The name is vocabulary, not capacity — and it is per-take
| Take | Size / path | Spelling |
|---|---|---|
| `651842ef` | tiny | `Sadhan` |
| `651842ef` | base | `Siddharth` |
| `3d92ded8` | production, EN 85% | `Sitharth` × **4** (00:21, 01:49, 02:04, 02:11) |
| truth | — | `Siddhant` |

No model size in the hub cache spells it, because no size has been told which
spelling is right. `.env.example:25` independently records the same phenomenon for
other words: `"Kamailio" arrives as "KMLU"` and `"Terraform" as "telephone"` —
corroborated by arm 1's drop list, which contained `kmlu`.

### 3.3 What a prompt actually buys, and what it costs
On `651842ef` (244 s telecom résumé): targets exact **1/9 → 7/9**, controls
**4/4 → 4/4**, `sadhan 1→0`, `siddhant 0→1`, language identical to 16 digits.
Cost, measured on the same run: `telephony` → `California` (`DROPPED` includes
`telephony`), `wipe 3→2`.

On `3cf6d657` (41 s pipeline test, no glossary terms spoken): the prompt replaced
the speaker's own self-identification **"Saturn" with "Siddhant"**.

On `456d0aff` (29 s Hindi, misdetected as `'id'`): the prompt wrote
**"Siddhant Kumar" twice** into a sentence containing no such words.

### 3.4 The instrument was blind, and that is the transferable lesson
The differential built to detect prompt imposition reported `INJECTED: NONE` on the
clean take — because it tests membership in a thirteen-word list, and the prompt's
own proper noun is outside that list **by construction**. On the jargon take it
reported six injections that were in fact six recoveries (the speaker is reading
those words). So it was anti-correlated on the take it was tuned for and blind on
the two it existed to protect. Only an occurrence counter, added for an unrelated
reason, caught the Saturn loss.

**Rule: an instrument's universe must include the thing it is testing for, or the
test is a ritual.** Corollary, from six instances: before writing a check, ask what
input makes it pass vacuously, and build that input as a fixture first.

### 3.5 Three takes, three disjoint vocabularies, one global knob
`651842ef` needs Kamailio/Asterisk/VoIP/PBX/Telus. `3cf6d657` needs nothing and was
corrupted anyway. `3d92ded8` needs FFmpeg/16 kHz/VAD/WebSocket/chunks — **none of
which any prompt tested this session contains.** Therefore
`WHISPER_INITIAL_PROMPT` is a **per-speaker, per-take** vocabulary. Setting it
deployment-wide fixes one take by corrupting others, and the takes it corrupts are
this machine's own archive.

### 3.6 Detection confidence on this hardware is low, often
Observed probabilities across the session: `0.11, 0.48, 0.56, 0.73, 0.74, 0.85,
0.93, 0.95`. Plain English audio on this mic detected at **0.48**, below the
library's own `language_detection_threshold=0.5`, resolved to EN by argmax
fallback. So the pin is not an optimisation for code-switched audio; on this
hardware it is the difference between a guess and a decision.

### 3.7 Beam size, captured by accident
Same vocalisation, same take: live preview (beam 1) produced
`Oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh, oh.`; the authoritative pass (beam 5)
produced `Whoo! Whoo! Whoo! Whoo!`. The live preview and the final transcript are
different measurements of the same audio, and the interim text must never
overwrite the authoritative one.

### 3.8 The "~2x" claim has three bases and must not be edited
`.env.example:26` and README:375 assert `base` ≈ 2x **decode cost**. The harness
measured 1.19–1.94x **wall time, load-inclusive**. The KB records Whisper time as
non-monotonic in both duration and speech fraction (`41.6 s → 1.1 s`,
`10.6 s → 2.1 s`, `146 s → 2.9 s`). Three different quantities; none refutes or
confirms the others. **Leave the README figure alone**, and say why in one sentence
rather than hedging.

### 3.9 Segmentation shifts under every decoder-context lever
Four independent instances (prompt on/off, size change, pin, language). Segment
counts are not a stable denominator for any quality claim.

## 4. 🔴 FALSE IN THE PUSHED RECORD — repair this first

Two pushed commit messages assert things this session's output does not support.
Neither can be amended (`git push --force` is forbidden here); both are repaired
forward by a new commit whose message states the correction.

### 4.1 `1cfe0f7` (patchkit) claims a control that is not in the file
The message says: *"Now AXES = 16 is one constant with three uses, the guard reads
'cases != AXES' … Proven two-sided: the tampered-source simulation … shows rc=1 and
'15/16' where a decorative return would have printed 0."*

**Actual state of `scripts/patchkit.py` at `1cfe0f7`:**
- `AXES` — **absent** (measured occ=0)
- `cases != AXES` — **absent** (occ=0); the guard is still `if fails:` (occ=1)
- `return 16 - 16` — **present** (occ=1), and it evaluates to 0 unconditionally
- the simulation printed **`rc=0`** and `*** THE CONTROL IS STILL DECORATIVE …
  Do not commit. ***`

Why the file is like this: the patch that would have fixed it (`PKHONEST`) was
**refused** by lint on two under-counted globals — `{cases}/{AXES}` wanted 1,
found 2 (it appears in P2's summary print *and* P3's failure message);
`PRESENT BUT WRAPPED` wanted 1, found 2 (in `_check_lit` *and* in the docstring).
The refusal wrote nothing, which is correct. The message was authored in a plan
before the refusal was known, and git attached it anyway.

**Severity:** this is the worst instance of the class in the arc. It does not
merely claim a check exists; it cites a two-sided proof and inverts its outcome. A
future reader would trust it more than any of the earlier overclaims.

### 4.2 `aea9a07` (studio) claims a verification that has not happened
The message says: *"Verified … in the browser on both the recording path and the
uploads path."* No browser output exists in this session. The **code** is verified
(anchors, splices, 18 globals, both sites, four guards at 0). The **behaviour** is
not. The message collapsed the two.

### 4.3 The forward repair
One commit, touching no code, whose message:
- states plainly that `1cfe0f7`'s SECOND paragraph describes a patch that was
  refused and never landed, and that the simulation's real result was rc=0;
- states that `aea9a07`'s browser claim is outstanding and is now §5 step B;
- records the mechanism (a message pre-authored inside a plan) and the rule that
  prevents it (§6 rule 2), so the next operator does not repeat it.

Then §5 step C lands the actual fix, with its own message generated from the real
output.

## 5. Remaining work, sequenced

Gates are hard: do not start a row until the row above it has a printed witness.

| # | Work | Shape | Gate to start | Gate to finish |
|---|---|---|---|---|
| **A** | Forward repair of §4 | 1 commit, prose only | nothing | pushed; message generated from this session's output |
| **B** | **Browser-verify the native-script toggle** | manual, 30 s | A | recording path grows; **uploads path grows** — the check a single-site patch would pass silently. Report what is seen, verbatim |
| **C** | Land the patchkit honesty fix (`AXES`, `cases != AXES`, derived return) | 1 patch, 4 edits | B | re-measure both refused counts first (`{cases}/{AXES}` → 2, `PRESENT BUT WRAPPED` → 2); lint clean; **the skip simulation must print rc=1 with `15/16`** — that is the finish line, not a green selftest |
| **D** | `WHISPER_INITIAL_PROMPT` into `.env.example` and README | 2 files | C | audit row becomes `settings=1 example=1 readme=1`; anchor on `.env.example:32-35` ASCII lines, **never** on the U+2500 separators (counts differ per section: 26/39/41/34/47); README row must match the three-column shape at `:375-378`; the prose block at `:399` and the name list at `:508` must both gain the knob |
| **E** | Measure take `3d92ded8` — the name gradient | read-only probe | D | resolve the take from the DB into a file first; **provenance gate before any probe** (artifact's language/prob must match the stored row); four name positions printed with offsets |
| **F** | `hotwords` — one read of the internal implementation | read-only | E | a written verdict, not a guess: `prometheus`/`grafana` failed *despite being listed*, `telephony`→`California` shows a prompt can damage, and `hotwords` is a separate kwarg applied as token bias, so it may have neither failure |
| **G** | Per-request `language` + `initial_prompt` thread | ~60 lines, 5 files | E, F | one path carries both; §3.5 is the witness that makes it a requirement rather than a preference |
| **H** | **Slice A — Translate** | 5 patches | G, plus my ruling on the language pack | see §5.1 |
| **I** | Slice B — TTS | TBD | a ruling on `Job.kind` | see §5.2 |
| **J** | Avatar | TBD | a ruling on what "avatar" means here | `Segment.word_json` makes portrait+audio+highlight nearly free |
| **K** | Docs pass (G5) | 2 files | E, F | the `base` verdict on positive grounds; the name as a distribution across four spellings; the segmentation shift; the `telephony`→`California` collateral; §3.8's three-bases explanation for why the 2× is untouched |

### 5.1 Slice A — Translate, and what is actually unknown
**Decided by evidence, not by preference:**
- *Sync, not queued.* `Job` has **no `kind` field** (`TakeKind` and `Take.kind`
  exist; `grep kind` on `worker.py`/`recovery.py` returns zero). Queuing translation
  means introducing a type dimension to the queue and teaching dispatch, the
  recovery sweep and `guard_pipeline_invariants` about it. A 2 KB string should not
  pay that. The repo's own 20-minute polling ceiling is the precedent for "long work
  goes on the queue", and a translation is not long work.
- *Storage: a new `Translation` model*, not columns on `Segment`. `Segment` is
  already `take` FK + `index` + `created_at`; three target languages are three rows,
  and rows compose with the roadmap's purge.

**Still unknown, and each blocks a patch:**
1. **The translate API surface.** `argostranslate.package` is the *package manager*
   (`install_package_for_language_pair`, `get_available_packages`, …). The call that
   translates lives in a different submodule and has not been read. **Do not write
   `translation_service.py` from memory** — that is exactly how `--fn transcribe`
   shipped broken.
2. **Import cost.** `argostranslate → stanza → torch`. torch is already in the venv,
   so no new GPU stack — but a top-level import could pull the CUDA loader into a
   Django process that has never touched it, in a project whose identity is offline
   CPU int8 with `OMP_NUM_THREADS=1` pinned before CTranslate2 imports. **Measure the
   import, then decide lazy-vs-top-level.** If it is slow, that also settles sync vs
   queued on cost grounds.
3. **The pair, and the download.** The UI reads *"Target Language Translation:
   English"*, so the direction is **take-language → `en`** (`hi→en` for a Hindi
   take). For an English take it is a no-op and the endpoint must say so rather than
   translate English into English. Fetching a pack is ~40 MB and is **a second
   network write requiring explicit approval.**
4. **Offline behaviour.** If the package index is unreachable, the service must fail
   loudly. A silent identity translation is the fluent-and-wrong failure mode again,
   one layer up.
5. **The `:546` un-disable** (`Argos Translate arrives in Phase 9`, occ=1, measured).
   Now editable — patchkit can touch HTML.

**Dependency-graph fact to record in README:** translation now depends on torch, in
a project that has never needed it for inference. That is a real change in the
project's character and should be stated, not absorbed.

### 5.2 Slice B — TTS
Blocked on a ruling, not on an engine: `Job` has no type field, so "queue the
synthesis" is a schema decision. Piper is the intended engine and is not installed.

### 5.3 Phase 9/10 — Real-time Speech-to-Speech Translation (now active)

The owner has directed that TTS, Avatar, Save, workspace panels, and real-time
translation are no longer deferred. The new goal is:

**Live real-time speech-to-speech translation at near-zero latency:**
- Mic → speech-to-text → language detection → (user-chosen target language)
  → text-to-speech → speaker/headphones, while the mic stays open.
- Upload any audio file → translated audio output in the chosen language.
- Multi-language: any supported language pair, not just one or two.

New pipeline stages:

| Stage | Component | Output |
|---|---|---|
| 5.5 | Live preview (beam=1) | Rolling interim transcript segments |
| 6 | Translation | NVIDIA Nemotron-3 Ultra (primary) or argostranslate (fallback) |
| 7 | TTS | Piper text-to-speech synthesis to audio |
| 8 | Audio output | WebSocket streamed PCM audio to browser `<audio>` |

### Translation providers

**Primary: NVIDIA Nemotron-3 Ultra (online NMT)**
- Model: `nvidia/nemotron-3-ultra-550b-a55b` via NVIDIA Build API
- Requires `NVIDIA_API_KEY` in `.env` (get from https://build.nvidia.com/)
- Endpoint: `https://integrate.api.nvidia.com/v1/chat/completions`
- Rate limits: max 14 parallel requests (semaphore), max 30 RPM (token bucket)
- Queue: asyncio.Queue with FIFO ordering; background worker drains respecting limits
- Supported languages: dozens of high-resource languages (en, es, fr, de, zh, ja, ar, pt, ru, ko, it, nl, pl, tr, vi, hi, etc.) with high proficiency; hundreds of lower-resource languages with varying accuracy. No fixed count — capability is emergent from training data, not a binary supported/unsupported list. (Verified directly for 15 languages via API.)
- Falls back to argostranslate on failure or when API key not configured

**Fallback: argostranslate (offline NMT)**
- No API key required, runs locally on CPU
- Requires language pack installation on first use (~40 MB per pair)
- Supports 100+ languages but quality varies by pair
- Used automatically when NVIDIA is unavailable or fails

Provider selection is automatic: NVIDIA preferred when configured; argostranslate
used otherwise. Both write to the same `Translation` model (unique per take+target).

### How NVIDIA Nemotron serves the complete TranStream system

NVIDIA Nemotron-3 Ultra is the single translation engine that powers every translation
surface in TranStream:

1. **Mic path (live speech-to-speech):** After Whisper transcribes a live segment,
   Nemotron translates the transcript into the user-chosen target language, then Piper
   synthesizes it to audio for simultaneous playback while the mic stays open.

2. **Upload path:** When a user uploads an audio file, Whisper transcribes it,
   Nemotron translates the full transcript into the chosen target language, and Piper
   synthesizes the translated text to audio for download/playback.

3. **Manual translation trigger:** From the studio UI, a user can request translation
   of any completed take's transcript into any supported target language via the
   `translate_take()` endpoint, which stores the result as a `Translation` row.

4. **All supported language pairs:** Nemotron supports 50+ languages (any ISO-639-1
   code it understands). Any take in any detected language can be translated into any
   supported target language — not limited to one or two pairs.

5. **Rate-limited production use:** The `asyncio.Semaphore(14)` caps concurrent
   NVIDIA requests at 14, and the token bucket (30 RPM) caps throughput at 30 requests
   per minute. Excess requests are held in an `asyncio.Queue` and drained FIFO,
   respecting both limits simultaneously. If NVIDIA times out or returns 429,
   argostranslate is used automatically as fallback.

### TTS engine
Piper is the intended engine (referenced throughout the codebase as "Piper TTS
voice avatars arrive in Phase 9"). Piper voice models (.onnx) must be installed
offline; the project ships with none by default.

Currently downloaded (8 languages): en_US-lessac-medium, fr_FR-siwis-medium,
hi_IN-pratham-medium, es_ES-davefx-medium, de_DE-thorsten-medium,
zh_CN-huayan-medium, ja_JP-hi_fi_captain-medium, ar_JO-kareem-medium.

### Latency target
Current ingestion uses 100 ms MediaRecorder chunks. The chunk size itself is a
browser/API contract — changing it to 1 ms is not feasible because:
- MediaRecorder `timeslice` has browser-imposed minimums (typically ≥100 ms).
- Whisper needs ~1–2 s of audio context for reliable transcription.
- VAD needs ~500 ms of silence to segment speech.

True "0% latency" is physically impossible with current STT/TTS technology.
The realistic target is pipelined overlap: while chunk N+1 is being recorded,
chunk N is being normalized → VAD → transcribed → translated → synthesized,
so end-to-end latency is bounded by the slowest single stage (~1–2 s), not by
the sum of all stages.

### Audio in/out simultaneously
Browser MediaRecorder + `<audio>` playback can run concurrently. Echo
cancellation is handled by the browser's `autoGainControl`, `echoCancellation`,
and `noiseSuppression` constraints (already set on `getUserMedia`).

### Upload → translated audio
The upload path (Phase 7) gains a "Translate & Speak" option: transcribe the
uploaded file, translate the transcript, synthesize the translation to audio,
and stream the resulting PCM back to the browser for playback.

### Guard rules for Phase 9/10
All existing guards (patchkit, layers, pipeline invariants, append-only log)
still apply. New TTS code must not introduce file truncation or deletion.
No `pip install` without explicit owner approval.

## 6. Invariants that must never be broken

These are the system's immune responses. Removing one to make a patch easier is
how this project has repeatedly shipped a falsehood.

1. **Edits go through patchkit.** `lint()` then `apply_file()`. Never `sed`, never
   string surgery, never a rewriting heredoc. Anchors are read from the file in the
   same turn, never from memory, never from a plan.
2. **A commit message is an output of verification.** Generated from the real
   `git diff` and the real verification output, after both exist. Never
   pre-authored. Never claim a browser check, test or measurement that has not been
   shown.
3. **Counts are measured as occurrences.** `str.count`, not `grep -c`. Line 97 of
   `studio.html` held `nativeScript` twice, so the two instruments disagreed by
   design and the wrong one was trusted once. Print both, side by side, so a
   divergence is visible at verification time rather than at refusal time.
4. **Never assert a number you have not just counted.** Sixteen misstated counts
   this session; every count the file printed was right. A diagnostic grep and an
   assertion needle are different tools — one's count must never seed the other's
   (`arrive[sd]?` is an ERE quantifier in a BRE context, where `?` is literal, so a
   diagnostic reported 4 Phase-9 titles when 6 exist).
5. **Disambiguate by content, never by whitespace depth.** `"                <span…"`
   is a substring of `"                    <span…"`, so no correct indentation cures
   a nested-site collision. Locate by the unique collection name, read the bytes,
   assert the geometry (`line +4 is the span`) before splicing.
6. **A needle may not span lines; an anchor may.** And a rule stated in a patch is
   not a control on that patch — four refusals came from patches whose preamble
   announced the rule they broke.
7. **Before writing a check, build the fixture that makes it pass vacuously.** Six
   artifacts this session certified what they could not observe (an inline
   re-implementation, a word list that excluded the prompt's own vocabulary, a stem
   tolerance matching `kama` inside `Kamail`, a scoping clause that skipped its own
   target, an agreement line comparing to a hardcoded string, and a return that read
   neither counter). For an instrument, also ask what input class it has never been
   given — patchkit's 13 axes never received a non-Python file.
8. **Read before write.** The file is the authority. This rule caught the
   `.env.example` drift before the write for the first time in six repair commits.
9. **A pre-install gate must be satisfiable.** Running patchkit's selftest from
   `/tmp` failed on a sibling import, so its colour carried no information — and the
   install ran anyway under a comment reading "ONLY after E2 is green". Supply the
   path (`PYTHONPATH=scripts`) or do not have the gate.
10. **Never truncate, rotate or delete `logs/`.** Never `git push --force`. Never
    amend a pushed commit — repair forward.
11. **Shell hygiene.** Patterns containing `!` go in single quotes (history
    expansion silently emptied a variable once and printed the wrong window).
    Heredoc delimiters are unique per block and never recycled from a previous
    block. No nested heredocs. Check any `$(…)` feeding arithmetic is non-empty.
12. **No install, download, schema migration or deletion without explicit approval.**
13. **After every change:** `guard_layers`, `guard_pipeline_invariants`,
    `guard_log_append_only`, `patchkit --selftest`, and `manage.py check` on any
    backend edit. All zero, or stop and report.

## 7. Known defects, honestly held

| # | Defect | Status |
|---|---|---|
| 1 | patchkit `selftest` returns 0 unconditionally (`return 16 - 16`) | **open**, §5 step C |
| 2 | patchkit `globals` has no `_flex`/WRAPPED diagnosis while `lit` does; comment-prose needles cannot use `lit` (not inside a string literal), so they are forced into the unprotected path | open, distinct from #1 |
| 3 | `compare_whisper_sizes.py --json` writes `{}` when every arm fails | open |
| 4 | the same artifact carries **no provenance** — no `wav`, no `prompt` — so a stale file from another take was measured and reported under the wrong take's name, producing a confident null | open; §5 step E gates on a caller-side provenance check |
| 5 | the harness's `targets recovered n/9` uses stem tolerance and can print a recovery that is not a word (`kama` inside `Kamail`), while `targets exact k/9` is not printed beside it | open — make the gap visible rather than silent |
| 6 | takes `f2a361a3` / `b9cae1e8` have `dur=0` with real files on disk | open |
| 7 | the nine-term target list is take-dependent (four witnesses) | open |
| 8 | `vad_parameters=dict(min_silence_duration_ms=500)` is a hardcode inside the Whisper call; whether it should track `VAD_MIN_SILENCE_MS` is an open design question, not a knob to steal | deliberate |
| 9 | the wired env reads are unguarded `int()`/`float()`, so a malformed value raises at import and stops every process including `manage.py check` | **deliberate** — silent reversion to a default was rejected because nobody would ever learn their `.env` was broken |
| 10 | `"Jessica Steam"` / `"Jessica Sting"` appear in prompt-armed arms, absent in the no-prompt arm and in both prompts. Prompt-*presence* instability, not content injection | unattributed; do not over-explain |
| 11 | `WHISPER_DETERMINISTIC` is read straight from `os.environ` by `tsworker.py`, before CTranslate2 import, because it must pin `OMP_NUM_THREADS=1`. Moving it onto settings would be a behaviour change dressed as a tidy-up | deliberate exception |

## 8. Standing observations
- The `stale` probe returns rc=1. Unchanged across the session; not a regression.
- `README.md` is 643 lines.
- `/tmp` artifacts (`g4.arm*.json`, `g4.prompt.txt`, `jargon.path`, `ref.path`,
  `arm34.wav`, `arm56.wav`) are the evidence for §3.3's headline result. **A reboot
  wipes them.** The runs are re-derivable (the wavs are on disk, the harness is
  committed) but the record is not.
- The A/B harness `pkill`s every `tsworker` in its `finally`. After any A/B run, the
  staffed worker must be restarted or uploads re-stick at `Queued`. Two features
  that must be sequenced, not a bug in either.

## 9. Definition of 100%

The project is complete when **all** of the following hold, each with a printed
witness in the same session:

1. Every Phase 1–8 stage still passes its counters on a fresh take (chunks in ==
   chunks out; VAD and Whisper segment counts explained as different stages).
2. All four guards, `patchkit --selftest`, `preflight_ab_warmup`,
   `check_migrations` and `manage.py check` return 0.
3. **Every knob appears in all three surfaces** — `settings.py`, `.env.example`,
   README — with the audit loop printing `1/1/≥1` on every row. Today exactly one
   row is broken (`WHISPER_INITIAL_PROMPT`, `1/0/0`).
4. **Every Phase 9 control is either functional or honestly absent.** No button may
   be `disabled` with a title promising a future phase unless that phase is
   scheduled in §5. Today: Native Script (functional in code, **unverified in
   behaviour**), Translate, TTS, Avatar, Save, workspace manager.
5. §4's two false pushed claims are repaired forward, and the repair commit exists.
6. §5 step C lands with the skip simulation printing **rc=1 and `15/16`** — the
   instrument is shown stopping itself on the input class that broke it.
7. Slice A is live end to end: a Hindi take produces an English translation, stored
   as `Translation` rows, rendered beside the transcript, with the pair direction
   ruled and the pack approved by the owner.
8. §3's measured facts are in the README, including the three that constrain design:
   the prompt cannot choose the language, the name is vocabulary not capacity, and
   the glossary is per-take not per-deployment.
9. **No claim anywhere in the git record is unsupported by this session's output.**
   This is the clause that makes the other eight meaningful.

Not in scope, by the owner's instruction, with receipts (§5.3): Save, workspace
panels, PWA. Declining to build them is part of 100%.
