#!/usr/bin/env python3
"""
Apply anchored edits to a file with the discipline this project has paid eight
vehicle failures to learn, so the caller supplies INTENT AS DATA and never
re-types the control flow that keeps breaking.

WHY THIS EXISTS AS A MODULE. Eight times the edits were RIGHT and the VEHICLE was
wrong: a `);` that killed the TAINT heredoc; an implicit-concatenation boundary
needle that killed METRIC; a stale anchor that killed METRIC2 against a file that
had moved in a three-day gap; an unmatched `)` in the EDITS tuple that killed
R2PATCH without writing a byte; PK2 - the patch meant to teach this engine the
wrap class - which died on TWO escaping errors in its own string-surgery heredoc
(under-escaped `\\n` became a real newline in the emitted source and broke
ast.parse; over-escaped `\\\\s+` emitted a regex matching a literal backslash-then-s,
so the wrap detector would have been dead even had it parsed); P2's caller carrying
FOUR data defects while the engine surfaced ONE (short-circuit masking); V3 - a
heredoc doing string surgery on THIS file, which the engine correctly refused
because its retired-fragment assertion was self-contradictory with v3's own design
(v3 keeps the raw global line in an else branch and only ADDS the code branch, so
the "retired" fragment legitimately survives); and the eighth, which is this
engine's own blind spot rather than a caller's - lint() on an HTML file died at
14:48:05 inside _collect -> segments_and_values -> ast.parse, raising
`SyntaxError: invalid character '📂' (U+1F4C2)` PAST the caller's except PatchError,
as a raw traceback. The file was not written (invariant 4 held under the crash),
but the engine could not name its own limit: it parses Python, unconditionally,
whatever the file is, and its thirteen-axis selftest never once handed it anything
else. So the long-standing note that this tool "cannot touch Markdown or dotenv"
was right about the symptom and wrong about the cause - the cause is one
unconditional line, and the fix is not a raw mode but a declaration.

The eighth is the lesson turning on itself twice over: the tool that forbids
hand-rolled string surgery must not itself be patched by hand-rolled string
surgery, and the tool whose whole claim is "shown stopping itself on every axis"
had an axis universe that excluded the input class that broke it.

THE MECHANISM LESSON, because it is the whole point of shipping via `cat`.
`cat > file <<'DELIM'` passes bytes VERBATIM: there is no outer Python, so a `\\n`
written in the heredoc is a `\\n` in the file and a `\\s+` is a `\\s+`. String
surgery (`python - <<'X'` doing str.replace) must instead emit source text THROUGH
a Python literal, which doubles every backslash and eats every `\\n` - and that
escape burden is what killed PK2, TAINT and R2PATCH, and what made V3 a heredoc on
the patcher. So: `cat` for whole files (6-for-6 this session: gate, pre-flight,
harness, patchkit v1, patchkit v2, patchkit v3); this tested engine plus
declarative data plus collect-all lint for edits (0-for-7 for hand-rolled
surgery). Never hand-count escapes in a replacement string - including when the
file being replaced is this one.

THE FIVE INVARIANTS, each enforced in code rather than in a comment:

  1. STRUCTURE OVER TEXT, AND ORDER-INDEPENDENT SPLICE. An edit may anchor on an
     AST node ("the Assign whose target is results['residual_s']") instead of on
     literal text. v1 resolved every span against the ORIGINAL text but spliced in
     LIST order, so an ast edit that was not first - or that followed a
     length-changing text edit - spliced at a STALE offset and silently corrupted
     the file. P2 happened to put its single ast edit first, so v1 was correct by
     luck of ordering, the same shape of luck as the blind gate's green. v2
     resolves every edit to (start,end) against the original, asserts the spans are
     PAIRWISE DISJOINT, and splices in DESCENDING start order, so no splice shifts
     an offset a later splice needs. Overlapping edits (ambiguous intent) are a
     refusal, not a double-splice. The redundant v1 check that an ast span's TEXT
     be unique is dropped: node uniqueness (exactly one Assign to that key) is the
     real guarantee, and span-text uniqueness would wrongly refuse a legitimate
     edit where the statement text coincidentally repeats.

  2. AN ANCHOR MAY SPAN LINES; A NEEDLE MAY NOT. An ANCHOR is matched against the
     file's own layout, so a layout change SHOULD abort it - that is the feature. A
     NEEDLE asserts that a PHRASE exists, and a phrase survives reflow while its
     source layout does not. So no needle, lit or retired, may contain a literal
     newline; and a lit needle that is absent by exact count but present under
     whitespace-widening is named PRESENT BUT WRAPPED, with the source excerpt,
     rather than reported as a bare count=0. The discriminator between a WRAP and a
     concatenation BOUNDARY is what sits between the words: pure whitespace (\\s+
     matches) is a wrap; a quote mark (\\s+ cannot cross) is a boundary. The two
     classes separate cleanly and each gets its own sentence.

  3. PROSE-BLINDNESS, AND THE code= KIND THAT GENERALISES IT. segments_and_values()
     returns both the folded VALUES and the per-literal SOURCE SEGMENTS, so a needle
     in a docstring and a needle in code are both "inside a literal" - this engine
     does NOT distinguish them, and must not be asked to. Therefore retired-shape
     ABSENCE (the id-less revoke URL, hand-rolled classify()) is the PRE-FLIGHT's
     jurisdiction, because the pre-flight scans code_only() and correctly ignores the
     documentation that explains why a shape is retired. But the same prose line
     bites PRESENCE globals too: APPLY2's ("runnable_jobs()", 1) fired because the
     phrase lives in BOTH the docstring ("the ONE predicate") and queue_count()'s
     body, so the raw count was 2. v1's short-circuit hid that as D-c; v3 makes it a
     KIND. A global may be ("needle", want) for a raw count, or ("needle", want,
     "code") to count against code_only() - the SAME implementation the pre-flight
     and the gate use, imported from guard_log_append_only, so "appears once IN
     CODE" means one thing in every textual check in the repo. Hand-narrowing a
     needle to a code-only form ("return runnable_jobs().count()") works for one
     global and is the hand-counting that produced D-b/c/d; the kind removes it for
     the debt files, whose docstrings quote the very symbols their edits assert.

  4. WRITE LAST, AND COLLECT ALL BEFORE WRITING. The new text is built in memory,
     every lit/src/retired/global assertion is run against it, ast.parse() is run
     against it when the source is Python, and only then is the file written. A
     failing assertion leaves the file byte-identical - and that held even when the
     parse itself raised, which is why the 14:48:05 crash wrote nothing. And lint()
     returns EVERY violation across EVERY edit and EVERY global in one pass, no
     short-circuit, because "one bug hides another" has now cost four extra cycles
     in this arc (the 400 masking the revoke route, check-4 masking check-5, D-a
     masking D-b/c/d, and V3's bad retired-fragment masking nothing only because it
     was the sole defect). A caller runs lint first; an empty list is the only thing
     that licenses apply_file.

     THE PYTHON DECLARATION, and it is part of this invariant rather than a new one.
     ast.parse() is a Python-only operation, and the lit diagnostics are built on
     it, so the engine cannot know a file's language by looking at a path. The
     caller therefore declares it: python=True (the default, so every existing
     caller is unaffected) parses and keeps the brace protection; python=False skips
     the parse entirely, which is what a text anchor plus raw-count globals needs,
     because neither requires a parser. A lit needle requested under python=False
     is misuse and is named as such. A non-Python file submitted under the default
     python=True is the 14:48:05 case and is now NAMED rather than raised, because
     forgetting to declare is the likely human error and it must not produce a
     traceback that escapes the caller's except PatchError.

  5. OBSERVED REFUSING. --selftest feeds the engine a synthetic source and proves
     it refuses on sixteen checks before it is allowed near a real file: acceptance;
     absent text anchor; ambiguous ast anchor; concatenation BOUNDARY needle;
     WRAPPED needle named as a wrap, plus a contiguous-needle control in the SAME
     wrapped source; overlapping edits refused; an ast edit placed AFTER a
     length-changing text edit still splices correctly (the v1 stale-span regression
     test); a retired needle containing a newline rejected; a retired string still
     present rejected; lint collecting MORE THAN ONE violation in a single pass; a
     code= global that PASSES where the same raw global REFUSES on prose (the
     runnable_jobs class, on a synthetic); a failing global leaving a real file
     byte-identical; a NON-PYTHON source with a text edit and raw globals applying
     cleanly with no parse attempted (python=False); a lit needle on that same
     source being NAMED as misuse; and - the 14:48:05 reproduction, on the DEFAULT
     path - a non-Python source under python=True being NAMED rather than raising
     SyntaxError out of lint(). A check that cannot stop you is a report; this one
     is shown stopping itself, on every axis, including the ones that bit it, the
     one that bit the attempt to patch it, and the one that bit the engine itself.

SCOPE, stated so it is not oversold: this is anchored EDIT application, not a merge
tool, not a semantic rewriter, and not a dataflow analyser. It will not fix a file
whose INTENT has drifted (that is what the read-only reconciliation step is for); it
refuses when an anchor does not match exactly once, and that refusal is the feature.
It parses Python when python=True and does not parse at all when python=False; it
does not guess a file's language from its name, and it has no opinion about
languages it cannot parse.
"""
import argparse
import ast
import pathlib
import re
import sys

# code_only() is owned by the append-only gate, which is also where the pre-flight
# imports it from. One implementation of prose-stripping, inherited by every textual
# check in the repo - so "this phrase appears once IN CODE" means the same thing in
# patchkit's code= globals and in the pre-flight's MUST/MUST_NOT. No cycle: the gate
# imports only stdlib, never patchkit.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from guard_log_append_only import code_only


class PatchError(Exception):
    """Any condition that makes the edit unsafe to apply. Raised before writing."""


# ─── invariant 2/3: the prose, boundary and wrap detector ───────────────────

def segments_and_values(src: str):
    """(per-literal SOURCE SEGMENTS, folded VALUES) for every string literal.

    A JoinedStr (f-string) contributes its whole source text including braces, so
    a needle that legitimately sits in the static chunk of an f-string is found as
    written. A plain Constant contributes both its segment (with quotes/newlines,
    as it appears in the file) and its folded value (what Python evaluates). The
    difference between the two lists is what distinguishes 'the needle is absent'
    from 'the needle exists only after implicit concatenation folded it' from 'the
    needle is there but the file WRAPS it across a line break inside one literal'.

    PYTHON-ONLY BY CONSTRUCTION: it calls ast.parse. Callers that are not editing
    Python pass python=False and never reach here - see invariant 4.
    """
    tree = ast.parse(src)
    segs, vals = [], []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            try:
                s = ast.get_source_segment(src, n)
            except Exception:
                s = None
            if s:
                segs.append(s)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            vals.append(n.value)
            try:
                s = ast.get_source_segment(src, n)
            except Exception:
                s = None
            if s:
                segs.append(s)
    return segs, vals


def _flex(frag: str):
    """A regex for `frag` with every whitespace run widened to \\s+.

    WHY: this is what distinguishes 'the phrase is absent' from 'the phrase is
    there but the file WRAPS it'. A needle written as 'the residual is now the
    worker clock' fails a plain count against source that reads
    'the residual is now\\nthe worker clock' - one physical literal, no
    concatenation, so the folded value does not contain the needle either and the
    boundary branch cannot fire. That is exactly what happened on 08:54:15 and the
    engine reported a bare count=0, which sent the reader hunting an absent phrase
    instead of a wrapped one.

    Widening whitespace does NOT collapse the concatenation case, because there
    the intervening characters are quote marks: '...one "'\\n     '"part two...'
    has a '"' between the words, and \\s+ cannot match a quote. So a flexible
    match inside a SEGMENT means wrap; no flexible match anywhere but an exact
    match in a folded VALUE means concatenation. The two classes separate cleanly
    and each gets its own sentence.
    """
    return re.compile(r"\s+".join(re.escape(w) for w in frag.split()))


# ─── invariant 1: locating an anchor, for BOTH kinds, as offsets ────────────

def _line_starts(src: str) -> list:
    starts = [0]
    for i, ch in enumerate(src):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _ast_span(src: str, node: ast.AST, starts: list):
    """Absolute (start, end) char offsets of a node's FULL physical lines,
    indentation included. Using line starts + col offsets rather than
    get_source_segment alone means the replaced span begins at column 0 of the
    node's first line, so the replacement text carries its own indentation and no
    alignment is ever counted by hand - which is the whole point of ast anchors."""
    start = starts[node.lineno - 1]
    end = starts[node.end_lineno - 1] + node.end_col_offset
    return start, end


def _find_assign_subscript(tree: ast.AST, container: str, key: str) -> ast.AST:
    """The Assign whose single target is container[key]. Handles the
    Subscript-on-Name form used everywhere in this repo's result dicts. Node
    uniqueness (exactly one match) is the guarantee; the span TEXT need not be
    unique, because the splice is by offset, not by search."""
    hits = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        t = n.targets[0]
        if not isinstance(t, ast.Subscript):
            continue
        if not (isinstance(t.value, ast.Name) and t.value.id == container):
            continue
        sl = t.slice
        if isinstance(sl, ast.Constant) and sl.value == key:
            hits.append(n)
    if len(hits) != 1:
        raise PatchError(
            f"ast anchor {container}[{key!r}] matched {len(hits)} nodes, want 1")
    return hits[0]


def _edit_span(src: str, edit: dict):
    """(start, end) absolute offsets of the text this edit replaces, computed
    against the SAME src, for either kind, plus a uniqueness guarantee. Text edits
    locate `old` by index (unique because count==1 is asserted); ast edits locate
    the node's full physical lines. Returning offsets for both kinds is what makes
    the splice order-independent - see _splice_all."""
    if edit.get("kind") == "ast":
        tree = ast.parse(src)
        node = _find_assign_subscript(tree, edit["container"], edit["key"])
        return _ast_span(src, node, _line_starts(src))
    old = edit["old"]
    c = src.count(old)
    if c != 1:
        raise PatchError(
            f"{edit['label']}: text anchor count={c}, want 1. "
            f"Anchor head: {old[:70]!r}")
    start = src.index(old)
    return start, start + len(old)


# ─── invariant 1: the order-independent splice ──────────────────────────────

def _splice_all(src: str, edits: tuple) -> str:
    """Resolve every edit's span against the ORIGINAL src, assert the spans are
    pairwise disjoint, then splice in DESCENDING start order so no splice shifts
    an offset that a later splice still needs.

    WHY: v1 resolved all spans against the original text but spliced in list
    order, so an ast edit that was not first in the list would splice at a stale
    offset once an earlier splice had changed the length. P2 happened to put its
    single ast edit first, so v1 was correct by luck of ordering - the same shape
    of luck as the blind gate's green. Descending order removes the dependence on
    list order entirely, and disjointness turns overlapping edits (ambiguous
    intent) into a refusal instead of a silent double-splice."""
    spans = []
    for e in edits:
        s, en = _edit_span(src, e)
        spans.append((s, en, e))
    ordered = sorted(spans, key=lambda t: t[0])
    for (a_s, a_e, a), (b_s, b_e, b) in zip(ordered, ordered[1:]):
        if b_s < a_e:
            raise PatchError(
                f"edits {a['label']!r} and {b['label']!r} overlap "
                f"([{a_s},{a_e}) vs [{b_s},{b_e})) - ambiguous intent; merge them "
                f"into one edit or separate them")
    new = src
    for s, en, e in sorted(spans, key=lambda t: -t[0]):
        new = new[:s] + e["new"] + new[en:]
    return new


# ─── invariant 2/3: the four-way lit diagnosis, as messages not raises ──────

def _check_lit(new: str, edit: dict, segs: list, vals: list) -> list:
    """Returns a list of violation strings (empty = clean). Four named diagnoses
    where v1 had one, so a wrap is never mistaken for an absence."""
    out = []
    for frag in edit.get("lit", ()):
        c = new.count(frag)
        if c == 1:
            if not any(frag in s for s in segs):
                out.append(
                    f"{edit['label']}: lit {frag[:48]!r} not inside one literal's "
                    f"source segment - boundary artifact")
            continue
        if "\n" in frag:
            out.append(
                f"{edit['label']}: lit {frag[:48]!r} CONTAINS A NEWLINE - needles "
                f"assert phrases and must be contiguous; anchors may span lines, "
                f"needles may not")
            continue
        rx = _flex(frag)
        hit = next((s for s in segs if rx.search(s)), None)
        if hit is not None:
            m = rx.search(hit)
            out.append(
                f"{edit['label']}: lit {frag[:48]!r} is PRESENT BUT WRAPPED - the "
                f"file breaks the line inside your phrase (source reads "
                f"{hit[max(0, m.start() - 12):m.end() + 12]!r}). Reflow the text so "
                f"the phrase sits on one line, or shorten the needle to one line. "
                f"This is a wrap, not an absence and not a concatenation.")
            continue
        if any(frag in v for v in vals):
            out.append(
                f"{edit['label']}: lit {frag[:48]!r} is in a FOLDED value but in no "
                f"single literal's source - implicit-concatenation BOUNDARY; pick a "
                f"needle inside one physical literal")
            continue
        out.append(
            f"{edit['label']}: lit {frag[:48]!r} count={c}, want 1 - genuinely "
            f"absent (not wrapped, not a boundary)")
    return out


def _check_src(new: str, edit: dict) -> list:
    out = []
    for frag in edit.get("src_assert", ()):
        c = new.count(frag)
        if c != 1:
            out.append(f"{edit['label']}: src {frag[:48]!r} count={c}, want 1")
    return out


def _check_retired(new: str, edit: dict) -> list:
    out = []
    for frag in edit.get("retired", ()):
        if "\n" in frag:
            out.append(
                f"{edit['label']}: retired {frag[:48]!r} CONTAINS A NEWLINE - a "
                f"retired needle that encodes a wrap breaks when someone reflows "
                f"the prose it is retiring, which is the opposite of its purpose. "
                f"Use a contiguous phrase, or rely on a global.")
            continue
        c = new.count(frag)
        if c != 0:
            out.append(
                f"{edit['label']}: retired {frag[:48]!r} still present (count={c})")
    return out


def _collect(src: str, edits: tuple, global_posts: tuple, python: bool = True) -> list:
    """The single source of truth for 'is this edit set safe to apply'. Returns
    every violation, never short-circuits. lint() and apply_text() both call it,
    so the preview and the commit cannot disagree.

    THE python FLAG, and why it is a parameter rather than a guess from the path.
    segments_and_values() parses the spliced text as PYTHON, because invariant 3's
    lit diagnostics need the per-literal source segments and folded values. That is
    correct for .py files and it was a CRASH for everything else until 14:48:05,
    when an HTML edit died there with `SyntaxError: invalid character '📂'`, out of
    lint(), past the caller's except PatchError. A text anchor and a raw-count
    global need no parser at all - so the parser is now only run when the caller
    says the source is Python, and a lit needle requested without that promise is
    named as misuse instead of raising.
    """
    try:
        new = _splice_all(src, edits)
    except PatchError as exc:
        return [str(exc)]
    errs = []
    wants_lit = any(e.get("lit") for e in edits)
    if python:
        # The brace protection is worth keeping for real Python. But a caller that
        # forgot to declare a non-Python file must get a sentence, not a traceback -
        # which is exactly what escaped the caller's except PatchError at 14:48:05,
        # and it is the DEFAULT path, so it is the case most likely to recur.
        try:
            segs, vals = segments_and_values(new)
        except SyntaxError as exc:
            return [
                "the spliced text does not parse as Python "
                f"({exc.msg} at line {exc.lineno}) - if this file is not Python, "
                "pass python=False; a text anchor and raw-count globals need no "
                "parser. If it IS Python, the replacement has a syntax error and "
                "must be fixed before anything is written."]
        for e in edits:
            errs.extend(_check_lit(new, e, segs, vals))
    elif wants_lit:
        for e in edits:
            for frag in e.get("lit", ()):
                errs.append(
                    f"{e['label']}: lit {frag[:48]!r} requested with python=False - "
                    f"the literal-boundary and wrap diagnostics cannot run on a "
                    f"non-Python source. Use a raw-count global instead.")
        if errs:
            return errs
    # src_assert and retired are raw counts on the text; they need no parser and
    # run identically under both declarations, which is why they are outside it.
    for e in edits:
        errs.extend(_check_src(new, e))
        errs.extend(_check_retired(new, e))
    for g in global_posts:
        if len(g) == 3 and g[2] == "code":
            frag, want = g[0], g[1]
            c = code_only(new).count(frag)
            if c != want:
                errs.append(
                    f"global(code) {frag[:48]!r} count-in-code={c}, want={want}"
                    " - raw count may differ if the needle also lives in prose")
        else:
            frag, want = g[0], g[1]
            c = new.count(frag)
            if c != want:
                errs.append(f"global {frag[:48]!r} count={c}, want={want}")
    return errs


def lint(src: str, edits: tuple, global_posts: tuple, python: bool = True) -> list:
    """Collect EVERY violation across every edit and every global, without raising
    and without writing. A caller runs this first so that a multi-edit change
    surfaces all its data defects in ONE pass instead of one per round trip - the
    'one bug hides another' pattern has now cost four extra cycles in this arc.
    Empty list == safe to apply.

    Callers editing non-Python (HTML, Markdown, dotenv) pass python=False. See
    invariant 4 for why the engine will not infer it from the filename."""
    return _collect(src, edits, global_posts, python=python)


def apply_text(src: str, edits: tuple, global_posts: tuple,
               python: bool = True) -> str:
    """Build the new text, run EVERY assertion, parse it when it is Python, return
    it. No file is touched here - this is the pure half, so --selftest can exercise
    the refusal paths on strings without a filesystem. Raises a composite listing
    ALL violations, never just the first."""
    errs = _collect(src, edits, global_posts, python=python)
    if errs:
        raise PatchError(
            "EDIT VALIDATION FAILED, all violations listed:\n  - "
            + "\n  - ".join(errs))
    new = _splice_all(src, edits)   # re-splice for the return value; _collect proved it clean
    if python:
        ast.parse(new)              # a bad brace dies here, not on disk
    return new


def apply_file(path: pathlib.Path, edits: tuple, global_posts: tuple,
               dry_run: bool = False, python: bool = True) -> list:
    """Read, apply_text, and - only if every assertion passed - write. Returns a
    human-readable report. dry_run computes and validates without writing, so the
    caller can preview a multi-edit change before committing to it."""
    src = path.read_text()
    new = apply_text(src, edits, global_posts, python=python)
    report = [f"  {e['label']}: {'ast' if e.get('kind')=='ast' else 'text'} anchor ok"
              for e in edits]
    report.append(f"  {len(global_posts)} global post-conditions hold")
    report.append(f"  delta: {len(src)} -> {len(new)} bytes "
                  f"({len(src.splitlines())} -> {len(new.splitlines())} lines)")
    if not python:
        report.append("  (python=False: no AST parse attempted - non-Python source)")
    if dry_run:
        report.append("  DRY RUN - nothing written")
        return report
    path.write_text(new)              # write last, after every guard above
    report.append("  WRITTEN")
    return report


# ─── invariant 5: observed refusing, on sixteen axes ────────────────────────

def selftest() -> int:
    """Sixteen checks, each naming the class it exercises, so a future regression
    that blinds one axis says which. Several exist because that exact failure
    happened to this engine or to a caller of it - including axes 14 to 16, which
    exist because the failure happened to the engine itself."""
    SRC = ('"""doc"""\n'
           'results = {}\n'
           'def run():\n'
           '    results["residual_s"] = round(1.0\n'
           '                                  - 2.0, 3)\n'
           '    print("bias runs AGAINST the effect")\n')

    GOOD = (
        dict(label="ast rewrite", kind="ast", container="results", key="residual_s",
             new='    results["residual_s"] = 9.9',
             lit=(), src_assert=('results["residual_s"] = 9.9',),
             retired=('round(1.0',)),
        dict(label="text rewrite", kind="text",
             old='    print("bias runs AGAINST the effect")',
             new='    print("bias runs FOR the effect")',
             lit=("bias runs FOR the effect",), src_assert=(),
             retired=("bias runs AGAINST",)),
    )
    GOOD_GLOBALS = (("bias runs AGAINST", 0), ("bias runs FOR the effect", 1),
                    ('results["residual_s"] = 9.9', 1))

    cases, fails = 0, 0

    # 1. acceptance: the good edits apply and satisfy every assertion.
    try:
        out = apply_text(SRC, GOOD, GOOD_GLOBALS)
        assert 'results["residual_s"] = 9.9' in out and "AGAINST" not in out
        cases += 1
        print("  [1] acceptance                 -> applied, assertions hold  OK")
    except PatchError as exc:
        fails += 1
        print(f"  [1] acceptance                 -> REFUSED WRONGLY: {exc}")

    # 2. wrong anchor count must refuse (a text anchor that is absent).
    try:
        apply_text(SRC, (dict(label="ghost", kind="text", old="NOT IN FILE",
                              new="x", lit=(), src_assert=(), retired=()),), ())
        fails += 1
        print("  [2] absent text anchor         -> APPLIED (must refuse)")
    except PatchError:
        cases += 1
        print("  [2] absent text anchor         -> refused  OK")

    # 3. ambiguous ast anchor must refuse (two assigns to the same key).
    AMBIG = SRC + 'results["residual_s"] = 1\n'
    try:
        apply_text(AMBIG, (dict(label="amb", kind="ast", container="results",
                                key="residual_s", new="x", lit=(),
                                src_assert=(), retired=()),), ())
        fails += 1
        print("  [3] ambiguous ast anchor       -> APPLIED (must refuse)")
    except PatchError:
        cases += 1
        print("  [3] ambiguous ast anchor       -> refused  OK")

    # 4. concatenation BOUNDARY must be NAMED as such, not as a bare count.
    BOUND = ('x = ("part one "\n'
             '     "part two needle")\n')
    try:
        apply_text(BOUND, (dict(label="bnd", kind="text",
                                old='x = ("part one "\n     "part two needle")',
                                new='x = ("part one "\n     "part two needle")',
                                lit=("one part two",), src_assert=(), retired=()),), ())
        fails += 1
        print("  [4] boundary needle            -> not detected (must refuse)")
    except PatchError as exc:
        if "BOUNDARY" in str(exc):
            cases += 1
            print("  [4] boundary needle            -> refused, named as BOUNDARY  OK")
        else:
            fails += 1
            print(f"  [4] boundary needle            -> refused but MISNAMED: {exc}")

    # 5. WRAPPED needle (the 08:54:15 case) must be named as a WRAP, not an absence.
    WRAP = 'x = """alpha beta\ngamma delta"""\n'
    try:
        apply_text(WRAP, (dict(label="wrap", kind="text",
                               old='x = """alpha beta\ngamma delta"""',
                               new='x = """alpha beta\ngamma delta"""',
                               lit=("beta gamma",), src_assert=(), retired=()),), ())
        fails += 1
        print("  [5] wrapped needle             -> not detected (must refuse)")
    except PatchError as exc:
        if "WRAPPED" in str(exc):
            cases += 1
            print("  [5] wrapped needle             -> refused, named as WRAPPED  OK")
        else:
            fails += 1
            print(f"  [5] wrapped needle             -> refused but MISNAMED: {exc}")

    # 6. control: a CONTIGUOUS needle in the SAME wrapped source must be accepted,
    #    so axis 5 is not a blanket refusal of anything near a line break.
    try:
        apply_text(WRAP, (dict(label="ok", kind="text",
                               old='x = """alpha beta\ngamma delta"""',
                               new='x = """alpha beta\ngamma delta"""',
                               lit=("alpha beta",), src_assert=(), retired=()),), ())
        cases += 1
        print("  [6] contiguous in wrapped src  -> accepted  OK")
    except PatchError as exc:
        fails += 1
        print(f"  [6] contiguous in wrapped src  -> REFUSED WRONGLY: {exc}")

    # 7. overlapping edits must refuse (ambiguous intent), not double-splice.
    try:
        apply_text(SRC, (
            dict(label="t", kind="text", old='results["residual_s"] = round(1.0',
                 new='results["residual_s"] = round(2.0', lit=(), src_assert=(),
                 retired=()),
            dict(label="a", kind="ast", container="results", key="residual_s",
                 new='results["residual_s"] = 5', lit=(), src_assert=(), retired=()),
        ), ())
        fails += 1
        print("  [7] overlapping edits          -> APPLIED (must refuse)")
    except PatchError as exc:
        if "overlap" in str(exc):
            cases += 1
            print("  [7] overlapping edits          -> refused, named as overlap  OK")
        else:
            fails += 1
            print(f"  [7] overlapping edits          -> refused but MISNAMED: {exc}")

    # 8. THE v1 STALE-SPAN REGRESSION: an ast edit placed AFTER a length-changing
    #    text edit must still splice at the right place. v1 computed the ast span
    #    against the original text but spliced in list order, so it would have
    #    corrupted this. Every debt caller (models/pipeline/recovery/README) is at
    #    risk of exactly this shape, which is why it is an axis and not a footnote.
    SRC9 = 'a = 1\nresults = {}\nresults["k"] = 0\n'
    try:
        out9 = apply_text(SRC9, (
            dict(label="text-first", kind="text", old="a = 1", new="aaaa = 1111",
                 lit=(), src_assert=("aaaa = 1111",), retired=()),
            dict(label="ast-second", kind="ast", container="results", key="k",
                 new='results["k"] = 42', lit=(),
                 src_assert=('results["k"] = 42',), retired=('results["k"] = 0',)),
        ), ())
        ok9 = ('results["k"] = 42' in out9 and 'aaaa = 1111' in out9
               and 'results = {}' in out9 and 'results["k"] = 0' not in out9)
        if ok9:
            cases += 1
            print("  [8] ast edit after text edit   -> spliced correctly (v1 bug fixed)  OK")
        else:
            fails += 1
            print(f"  [8] ast edit after text edit   -> CORRUPTED: {out9!r}")
    except PatchError as exc:
        fails += 1
        print(f"  [8] ast edit after text edit   -> refused WRONGLY: {exc}")

    # 9. a retired needle containing a newline must be rejected on principle.
    try:
        apply_text(SRC, (dict(label="rn", kind="text",
                              old='    print("bias runs AGAINST the effect")',
                              new='    print("x")',
                              lit=(), src_assert=(),
                              retired=("bias runs AGAINST\nthe effect",)),), ())
        fails += 1
        print("  [9] retired needle w/ newline   -> APPLIED (must refuse)")
    except PatchError as exc:
        if "CONTAINS A NEWLINE" in str(exc):
            cases += 1
            print("  [9] retired needle w/ newline   -> refused, named  OK")
        else:
            fails += 1
            print(f"  [9] retired needle w/ newline   -> refused but MISNAMED: {exc}")

    # 10. a retired string still present must refuse.
    try:
        apply_text(SRC, (dict(label="ret", kind="text",
                              old='    print("bias runs AGAINST the effect")',
                              new='    print("bias runs AGAINST the effect")',
                              lit=(), src_assert=(), retired=("bias runs AGAINST",)),), ())
        fails += 1
        print("  [10] retired string present     -> APPLIED (must refuse)")
    except PatchError:
        cases += 1
        print("  [10] retired string present     -> refused  OK")

    # 11. lint must collect MORE THAN ONE violation in a single pass - the whole
    #     point of axis 11 is that P2's four data defects were masked to one by
    #     short-circuiting. Here a wrap lit AND an absent global, both reported.
    viols = lint(WRAP, (dict(label="w", kind="text",
                             old='x = """alpha beta\ngamma delta"""',
                             new='x = """alpha beta\ngamma delta"""',
                             lit=("beta gamma",), src_assert=(), retired=()),),
                 (("this global is absent", 1),))
    if len(viols) >= 2 and any("WRAPPED" in v for v in viols) \
            and any("global" in v for v in viols):
        cases += 1
        print(f"  [11] lint collects all          -> {len(viols)} violations in one pass  OK")
    else:
        fails += 1
        print(f"  [11] lint collects all          -> only {viols} (must list >=2, no short-circuit)")

    # 12. the runnable_jobs() class, made mechanical: a needle that lives in a
    #     docstring AND in code must FAIL a raw presence-exactly-one global and
    #     PASS the same global in "code" mode. This is the D-c mirror that bit
    #     APPLY2 at 09:11:02; v3 turns "narrow the needle by hand" into a kind.
    PROSE_AND_CODE = ('"""the queue uses runnable_jobs() - the ONE predicate"""\n'
                      'def q():\n    return runnable_jobs().count()\n')
    raw_v = lint(PROSE_AND_CODE, (), (("runnable_jobs()", 1),))
    code_v = lint(PROSE_AND_CODE, (), (("runnable_jobs()", 1, "code"),))
    if raw_v and not code_v:
        cases += 1
        print("  [12] code= global vs raw      -> raw refused (prose), code accepted  OK")
    else:
        fails += 1
        print(f"  [12] code= global vs raw      -> raw={raw_v} code={code_v} (raw must refuse, code must pass)")

    # 13. write-last: a failing global must leave a real file byte-identical.
    import tempfile, os
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    try:
        pathlib.Path(tmp).write_text(SRC)
        before = pathlib.Path(tmp).read_bytes()
        try:
            apply_file(pathlib.Path(tmp), GOOD, (("this string does not exist", 99),))
            fails += 1
            print("  [13] failing global             -> WROTE (must not)")
        except PatchError:
            after = pathlib.Path(tmp).read_bytes()
            if after == before:
                cases += 1
                print("  [13] failing global             -> file untouched  OK")
            else:
                fails += 1
                print("  [13] failing global             -> file CHANGED despite refusal")
    finally:
        os.unlink(tmp)

    # 14. A NON-PYTHON SOURCE with a text edit and raw-count globals must apply.
    #     This is the 14:48:05 case, and it is the reason the python flag exists:
    #     an HTML edit died in _collect -> segments_and_values -> ast.parse with
    #     `SyntaxError: invalid character '📂' (U+1F4C2)`, out of lint(), past the
    #     caller's except PatchError, as a raw traceback. The file was not written
    #     (invariant 4 held under the crash) but the engine could not say why. A
    #     text anchor and a raw count need no parser, so declaring python=False
    #     must let them through - and must not lose a byte of the rest of the file.
    HTML = ('<div id="a">\n'
            '  <span class="t" x-text="seg.text"></span>\n'
            '</div>\n'
            '\U0001f4c2 Open File System\n')
    try:
        out14 = apply_text(HTML, (dict(label="html", kind="text",
                                       old='  <span class="t" x-text="seg.text"></span>',
                                       new='  <span class="t" x-text="seg.text"\n'
                                           '        :class="on ? \'big\' : \'\'"></span>',
                                       lit=(), src_assert=(), retired=()),),
                           ((':class="on ?', 1), ('x-text="seg.text"', 1)),
                           python=False)
        if ':class="on ?' in out14 and '\U0001f4c2' in out14 and '<div id="a">' in out14:
            cases += 1
            print("  [14] non-Python + text edit   -> applied, no parse attempted  OK")
        else:
            fails += 1
            print(f"  [14] non-Python + text edit   -> LOST CONTENT: {out14!r}")
    except PatchError as exc:
        fails += 1
        print(f"  [14] non-Python + text edit   -> REFUSED WRONGLY: {exc}")
    except SyntaxError as exc:
        fails += 1
        print(f"  [14] non-Python + text edit   -> TRACEBACK, not a refusal: {exc}")

    # 15. A lit needle requested under python=False must be NAMED as misuse, not
    #     raised. The caller declared the source is not Python and then asked for a
    #     diagnostic that can only run on Python; that is a contradiction in the
    #     caller's data, and contradictions get a sentence.
    try:
        apply_text(HTML, (dict(label="misuse", kind="text",
                               old='<div id="a">', new='<div id="a">',
                               lit=("seg.text",), src_assert=(), retired=()),),
                   (), python=False)
        fails += 1
        print("  [15] lit on non-Python        -> APPLIED (must refuse)")
    except PatchError as exc:
        if "python=False" in str(exc):
            cases += 1
            print("  [15] lit on non-Python        -> refused, named  OK")
        else:
            fails += 1
            print(f"  [15] lit on non-Python        -> refused but MISNAMED: {exc}")
    except SyntaxError as exc:
        fails += 1
        print(f"  [15] lit on non-Python        -> TRACEBACK, not a refusal: {exc}")

    # 16. THE ACTUAL 14:48:05 REPRODUCTION, ON THE DEFAULT PATH. python=True on a
    #     non-Python source must refuse with a named sentence rather than raise
    #     SyntaxError out of lint(). This is the most important of the three new
    #     axes, because python=True is the default: a caller that FORGETS to declare
    #     is the likely human error, and it must produce a sentence, not a traceback
    #     that escapes the caller's except PatchError and leaves the caller guessing
    #     whether the file was written.
    viols16 = lint(HTML, (dict(label="forgot", kind="text",
                               old='<div id="a">', new='<div id="b">',
                               lit=(), src_assert=(), retired=()),), ())
    if viols16 and any("python=False" in v for v in viols16):
        cases += 1
        print("  [16] undeclared non-Python    -> refused, named (no traceback)  OK")
    else:
        fails += 1
        print(f"  [16] undeclared non-Python    -> {viols16!r} (must name the parse failure "
              f"and point at python=False)")

    print(f"  selftest: {cases}/16 checks exercised, {fails} failure(s)")
    if fails:
        print("SELFTEST FAIL - patchkit cannot be trusted to edit a real file")
        return 1
    print("SELFTEST PASS - applies good edits; refuses absent/ambiguous/overlapping "
          "anchors; names BOUNDARY, WRAPPED and newline needles distinctly; splices "
          "an ast edit correctly even after a length-changing text edit (the v1 "
          "stale-span bug); lint lists every violation in one pass; a code= global "
          "passes where the same raw global refuses on prose; writes nothing when a "
          "global fails; applies a text edit on a NON-PYTHON source under "
          "python=False; and refuses a parse it cannot perform with a sentence "
          "instead of a traceback, on the default path as well as the declared one. "
          "Certified on sixteen axes before it touches the debt files.")
    return 16 - 16  # 0 on success; the arithmetic is deliberate, not a literal


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true",
                    help="prove the engine can refuse, on sixteen checks, before use")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    print("patchkit is a library; call lint() then apply_file() from a caller that "
          "supplies edits as data. Pass python=False for HTML, Markdown or dotenv. "
          "Run --selftest first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
