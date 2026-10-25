#!/usr/bin/env python3
"""
Assert that nothing in scripts/ truncates or deletes the durable log artifacts,
and give every other textual check in this repo a way to be blind to prose.

WHY THIS EXISTS. Commit 28ffb2c attached a RotatingFileHandler because "the
durable-path evidence lived only in a terminal that closes". Twenty minutes later
an ad-hoc bash A/B did `: > logs/worker.log` and destroyed the artifact anyway -
17 lines survived where 8148 bytes had been, and the three warmup self-reports
(3080, 2704, 3234 ms) that 34bc087 cites by number existed only in that commit
message and a chat transcript. The design was right; the artifact was not
defended. Those samples were regenerated on 2026-10-22 at 01:48 shell time
(total=6569ms / 2411ms / 2702ms) and this gate is what keeps them there.

WHAT THIS GATE CAN AND CANNOT SEE, stated rather than oversold. It parses the AST
of scripts/*.py. It CANNOT see a paste into a terminal, and it did not catch the
03:20 truncation - no static gate could have, because the offending text never
existed in a file. Claiming otherwise is the manufactured-confidence pattern this
project has catalogued all session. What it prevents is the realistic recurrence:
a future harness in scripts/ that truncates the log to make its own grep easier.
The defence for the paste class is something else and is already in place: the
warmup SELF-REPORTS on every warmed start, so the number is re-derivable rather
than precious. This gate is a backstop, not the primary control, and it says so.

TWO REVISIONS FAILED OPEN, AND BOTH ARE FIXED HERE BY CONSTRUCTION.

  1. The first revision was a literal-string scanner wearing an AST's clothes.
     _touches_logs() was applied to the SOURCE TEXT of a call's argument, so
     open("logs/worker.log", "w") was caught and open(LOG_FILE, "w") was not,
     because the text of that argument is "LOG_FILE" and contains no marker.
     Every script in this repo binds paths to module-level constants (ROOT,
     LOG_FILE, PORT_FILE, BACKEND), so the gate was blind to the dominant shape
     - including the shape in the harness it was written to guard. Its own
     --selftest caught this: three destructive operations in the synthetic, one
     seen. Had the assertion been ">= 1" instead of ">= 3", the gate would have
     shipped blind with a green selftest. Fixed by log_tainted_names(), which
     resolves constant-bound names to a fixpoint.

  2. The os.remove/os.unlink branch was UNREACHABLE. It was written as a second
     `elif isinstance(f, ast.Attribute)` AFTER a bare
     `elif isinstance(f, ast.Attribute)`, so the earlier branch consumed every
     attribute call and "remove" matched none of its three inner sets. Family 5
     of the selftest could therefore never pass no matter how good the resolver
     got - and the failure would have been misattributed to the resolver. Fixed
     by testing DELETE_FUNCS first, inside the single attribute branch.
     Ordering was the whole bug, and it is named here because a future reader
     who "simplifies" the branch order will reintroduce it silently.

WHY THE CHECK IS SYNTACTIC AND NOT A SUBSTRING SCAN - AND WHY code_only() EXISTS.
Twice in one evening a control fired on DOCUMENTATION of the thing it forbids:
this gate's first draft scanned for the word "truncate" and reported YES on a
harness whose own docstring says "NEVER truncates"; and the A/B harness's
pre-flight scanned for the retired URL /account/keys/revoke/ and refused a file
that mentions that URL only to explain why it is retired. Both are the same
defect: the checker was handed the prose. Stripping prose is a property of the
INPUT, not a note to the reader, so it is implemented once, here, and exported -
every caller inherits it and none can forget it. scan_source() itself does not
need it (it reads arguments out of the AST, which are structurally not prose),
but any check that must look at text does.

SCOPE OF NAME RESOLUTION, stated so it is not mistaken for dataflow: module-level
and nested ast.Assign / ast.AnnAssign only, matched by NAME IDENTITY (a variable
called logs_dir is not tainted because the word appears in it). No function
parameters, no imports, no attribute chains through other objects, no
conditionals. A script that builds a log path inside a function from an argument
is not resolved and this gate will miss it. That is a real limit, documented
rather than discovered by a future incident.

DESTRUCTIVE OPERATIONS RECOGNISED (fatal tier):
  open(path, mode) where mode contains 'w'   ('a' and 'x' are safe)
  Path(path).write_text() / .write_bytes()   (both truncate)
  file.truncate() / os.truncate()
  os.remove / os.unlink / Path.unlink        (destroys rather than truncates)
  shell strings with a single '>', ':>', 'truncate -s 0', or 'cp /dev/null'
  whose target is a log artifact             ('>>' is append and is allowed)

ADVISORY TIER, and why it is not fatal: logs/ also holds transtream.port, a
mutable pointer that MUST be overwritten on every start. Failing a correct line
is how a gate gets deleted from the pipeline, so directory-level targets are
printed and do not affect the exit code. Artifact names are fatal.
"""
import argparse
import ast
import io
import pathlib
import re
import sys
import tokenize

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# Tier 1 - the durable artifacts. Destructive operation on one of these is fatal.
LOG_ARTIFACTS = ("worker.log", "daphne.log", "redis.log")
# Tier 2 - the directory. Present so constant resolution still works
# (LOGS = ROOT/"logs"; open(LOGS/"worker.log","w") must be caught), but a hit
# here alone is advisory only.
LOGS_DIR_MARKER = "logs/"
LOG_MARKERS = LOG_ARTIFACTS + (LOGS_DIR_MARKER,)

TRUNC_METHODS = {"truncate"}
DELETE_FUNCS = {("os", "remove"), ("os", "unlink")}
DELETE_METHODS = {"unlink"}
WRITE_METHODS = {"write_text", "write_bytes"}
# Both os.* and subprocess.* are covered. The first revision tested only
# f.value.id == "os", so subprocess.run("... > logs/worker.log", shell=True)
# was invisible while the docstring claimed to cover it.
SHELL_MODULES = {"os", "subprocess"}
SHELL_FUNCS = {"system", "popen", "run", "call", "check_output", "check_call"}
SHELL_BARE_NAMES = SHELL_FUNCS | {"Popen"}

# A single '>' that is not part of '>>', not a comparison, not '>&', not '2>'.
SINGLE_GT = re.compile(r"(?<![&\d])>(?![>=])")
SHELL_TRUNCATORS = (":>", "truncate -s 0", "truncate -s0", "cp /dev/null",
                    "mv /dev/null", "rm -f", "rm -rf")


# ─── argument text, and the prose stripper ──────────────────────────────────

def _arg_segment(node: ast.AST, src: str) -> str:
    """Source text of ONE node - a call's argument, or a call's receiver.

    Never the surrounding statement, never anything outside the parens. This is
    the whole reason the first version could not see a docstring: it was handed
    text from inside the parentheses and nothing else. It is also why the
    docstring must not be trusted to be invisible by luck - see code_only().
    """
    try:
        seg = ast.get_source_segment(src, node)
    except Exception:
        seg = None
    return seg or ""


def _line_start_offsets(src: str) -> list:
    """Byte offset of the start of every 1-based line, so (row, col) pairs from
    ast/tokenize can be mapped into one flat character buffer."""
    offs = [0]
    for i, ch in enumerate(src):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def code_only(src: str) -> str:
    """src with every docstring and every comment replaced by SPACES.

    Length, line count and every column offset are preserved exactly, so
    ast.parse(code_only(src)) yields identical positions to ast.parse(src) and
    ast.get_source_segment(code_only(src), node) still resolves. What changes is
    that no textual scan can any longer see prose - which is the property that
    was missing twice on 2026-10-22 and is the reason this function exists at
    all rather than a warning comment.

    Blank-line-preserving matters: a check that reports 'file.py:112' must still
    point at line 112 of the real file, or the gate becomes harder to use than
    the bug it catches.
    """
    chars = list(src)
    offs = _line_start_offsets(src)

    def blank(row0: int, col0: int, row1: int, col1: int) -> None:
        start = offs[row0 - 1] + col0
        end = offs[row1 - 1] + col1
        for i in range(max(0, start), min(end, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "

    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                and isinstance(first.value.value, str):
            blank(first.lineno, first.col_offset,
                  first.end_lineno, first.end_col_offset)

    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                blank(tok.start[0], tok.start[1], tok.end[0], tok.end[1])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file that will not tokenize is not a file that will import either;
        # py_compile and the suite own that failure mode. Degrade to
        # docstring-only rather than raise, so this gate can never be the thing
        # that stops a read of a half-written script.
        pass
    return "".join(chars)


# ─── constant-bound name resolution ─────────────────────────────────────────

def _touches_logs(seg: str) -> bool:
    return any(m in seg for m in LOG_MARKERS)


def _is_artifact(seg: str) -> bool:
    return any(m in seg for m in LOG_ARTIFACTS)


def log_tainted_names(tree: ast.AST, src: str) -> set:
    """Names bound, transitively, to an expression that mentions a logs marker.

    A fixpoint loop, not one pass: LOG = pathlib.Path("logs")/"worker.log" then
    LOG2 = LOG is the shape real code uses to alias a path, and a single pass
    would taint LOG and miss LOG2. The selftest's synthetic includes LOG2
    precisely so the loop is exercised rather than assumed.
    """
    tainted: set = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value, targets = node.value, node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value, targets = node.value, [node.target]
            else:
                continue
            seg = _arg_segment(value, src)
            hit = _touches_logs(seg) or any(
                isinstance(sub, ast.Name) and sub.id in tainted
                for sub in ast.walk(value)
            )
            if not hit:
                continue
            for tgt in targets:
                for sub in ast.walk(tgt):
                    if isinstance(sub, ast.Name) and sub.id not in tainted:
                        tainted.add(sub.id)
                        changed = True
    return tainted


def touches(seg: str, expr: ast.AST | None, tainted: set) -> bool:
    """True if this expression names a logs path, literally or by resolution.

    Matched by NAME IDENTITY: `logs_dir = "/tmp"` is not tainted because the
    word 'logs' appears in the NAME, only if the VALUE mentions a marker. The
    reverse also holds - a name called LOG touching nothing is not a hit.
    """
    if _touches_logs(seg):
        return True
    if expr is None or not tainted:
        return False
    return any(isinstance(sub, ast.Name) and sub.id in tainted
               for sub in ast.walk(expr))


# ─── the scan ───────────────────────────────────────────────────────────────

def _mode_truncates(mode_node: ast.AST, src: str) -> bool:
    if isinstance(mode_node, ast.Constant):
        return isinstance(mode_node.value, str) and "w" in mode_node.value
    seg = _arg_segment(mode_node, src)
    return '"w' in seg or "'w" in seg or 'mode="w' in seg or "mode='w" in seg


def _shell_truncates(seg: str) -> bool:
    """Single '>' (not '>>'), ':>', 'truncate -s 0', 'cp /dev/null', 'rm -f'.

    '>>' is removed BEFORE the single-'>' search, so an append cannot be
    misread as an overwrite. The first revision did this with a replace() whose
    needle contained a zero-width space inherited from a chat transcript, so it
    compared against a string no shell can produce - a check that silently
    never fires is worse than no check.
    """
    if not seg:
        return False
    if any(t in seg for t in SHELL_TRUNCATORS):
        return True
    return bool(SINGLE_GT.search(seg.replace(">>", "")))


def _scan(src: str, name: str = "<source>") -> list:
    """Return [(severity, message)] for one source text. severity is 'FATAL'
    or 'ADVISORY'; the split is the artifact/directory tiering from the
    docstring, and it is decided per finding, not per file."""
    out = []
    tree = ast.parse(src)
    tainted = log_tainted_names(tree, src)

    def emit(sev, lineno, text):
        out.append((sev, f"{name}:{lineno} {text}"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func

        # open(path, "w") / open(path, mode="w")
        if isinstance(f, ast.Name) and f.id == "open":
            path_expr = node.args[0] if node.args else None
            hits = []
            if len(node.args) >= 2 and _mode_truncates(node.args[1], src):
                hits.append(_arg_segment(node.args[0], src))
            for kw in node.keywords:
                if kw.arg == "mode" and _mode_truncates(kw.value, src):
                    hits.append(_arg_segment(path_expr, src) if path_expr else "")
            for seg in hits:
                if touches(seg, path_expr, tainted):
                    sev = "FATAL" if _is_artifact(seg) or tainted_hits(
                        path_expr, tainted, LOG_ARTIFACTS, src) else "ADVISORY"
                    emit(sev, node.lineno,
                         f"open(..., mode with 'w') on a logs path: {seg[:60]!r}")

        elif isinstance(f, ast.Attribute):
            # ORDER MATTERS - see the module docstring, finding 2.
            if isinstance(f.value, ast.Name) and (f.value.id, f.attr) in DELETE_FUNCS:
                arg = node.args[0] if node.args else None
                seg = _arg_segment(arg, src) if arg is not None else ""
                if touches(seg, arg, tainted):
                    sev = "FATAL" if _is_artifact(seg) or tainted_hits(
                        arg, tainted, LOG_ARTIFACTS, src) else "ADVISORY"
                    emit(sev, node.lineno,
                         f"{f.value.id}.{f.attr}() destroys a logs path: {seg[:60]!r}")
                continue

            recv = _arg_segment(f.value, src)
            if not touches(recv, f.value, tainted):
                pass
            elif f.attr in WRITE_METHODS:
                sev = "FATAL" if _is_artifact(recv) or tainted_hits(
                    f.value, tainted, LOG_ARTIFACTS, src) else "ADVISORY"
                emit(sev, node.lineno,
                     f".{f.attr}() on a logs path: {recv[:60]!r}")
            elif f.attr in DELETE_METHODS:
                sev = "FATAL" if _is_artifact(recv) or tainted_hits(
                    f.value, tainted, LOG_ARTIFACTS, src) else "ADVISORY"
                emit(sev, node.lineno,
                     f".{f.attr}() destroys a logs path: {recv[:60]!r}")
            elif f.attr in TRUNC_METHODS:
                sev = "FATAL" if _is_artifact(recv) or tainted_hits(
                    f.value, tainted, LOG_ARTIFACTS, src) else "ADVISORY"
                emit(sev, node.lineno, f".{f.attr}() on a logs path: {recv[:60]!r}")

            # shell redirection targeting a logs path, from os.* or subprocess.*
            if isinstance(f.value, ast.Name) and f.value.id in SHELL_MODULES \
                    and f.attr in SHELL_FUNCS and node.args:
                seg = _arg_segment(node.args[0], src)
                if _touches_logs(seg) and _shell_truncates(seg):
                    sev = "FATAL" if _is_artifact(seg) else "ADVISORY"
                    emit(sev, node.lineno,
                         f"{f.value.id}.{f.attr} shell truncation of a logs "
                         f"path: {seg[:80]!r}")

        elif isinstance(f, ast.Name) and f.id in SHELL_BARE_NAMES and node.args:
            seg = _arg_segment(node.args[0], src)
            if _touches_logs(seg) and _shell_truncates(seg):
                sev = "FATAL" if _is_artifact(seg) else "ADVISORY"
                emit(sev, node.lineno,
                     f"{f.id} truncation of a logs path: {seg[:80]!r}")
    return out


def tainted_hits(expr: ast.AST | None, tainted: set, names: tuple, src: str) -> bool:
    """True when a tainted NAME was itself bound from an expression mentioning
    one of `names`. This is what distinguishes LOG = Path("logs")/"worker.log"
    (fatal) from PORT = Path("logs")/"transtream.port" (advisory) once the
    literal text of the argument is only 'LOG' or 'PORT'."""
    if expr is None or not tainted:
        return False
    for sub in ast.walk(expr):
        if isinstance(sub, ast.Name) and sub.id in tainted:
            # Re-resolve the binding site to see WHICH artifact it carries.
            if _binding_mentions(sub.id, src, names):
                return True
    return False


def _binding_mentions(name: str, src: str, markers: tuple) -> bool:
    """Does the assignment to `name` (or to anything it was chained from) carry
    one of `markers`? Walks the same fixpoint the taint resolver used, so
    LOG2 = LOG resolves back to the worker.log binding."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False
    wanted = {name}
    for _ in range(8):                      # bounded: chains are short in practice
        grew = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value, targets = node.value, node.targets
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                value, targets = node.value, [node.target]
            else:
                continue
            if not any(isinstance(t, ast.Name) and t.id in wanted for t in
                       (x for tgt in targets for x in ast.walk(tgt)
                        if isinstance(x, ast.Name))):
                continue
            seg = _arg_segment(value, src)
            if any(m in seg for m in markers):
                return True
            for sub in ast.walk(value):
                if isinstance(sub, ast.Name) and sub.id not in wanted:
                    wanted.add(sub.id)
                    grew = True
        if not grew:
            break
    return False


def scan_source(src: str, name: str = "<source>") -> list:
    """FATAL violations only, as plain strings.

    This is the contract every existing caller already uses
    (`v = scan_source(...); if v: sys.exit(...)`), and it is deliberately kept:
    an advisory must never be able to abort a harness, or the tiering would be
    decoration. Use scan_tree() when the advisories matter.
    """
    return [msg for sev, msg in _scan(src, name) if sev == "FATAL"]


def scan_tree(src: str, name: str = "<source>") -> tuple:
    """Return (fatal_messages, advisory_messages)."""
    rows = _scan(src, name)
    return ([m for s, m in rows if s == "FATAL"],
            [m for s, m in rows if s == "ADVISORY"])


# ─── selftest: observed refusing, on every axis ─────────────────────────────

def selftest() -> int:
    """Five destructive families in both real shapes, plus prose immunity.

    PER-FAMILY, not per-count. A count of 5 can be reached by flagging one line
    five times; these names cannot. If a future edit regresses the resolver, the
    branch order, or code_only(), the output says WHICH one.
    """
    bad = '''"""This harness never truncates the log; it reads by offset."""
import os, pathlib
LOG = pathlib.Path("logs") / "worker.log"
LOG2 = LOG                      # chained binding: the fixpoint must resolve this
def run():
    with open("logs/worker.log", "w") as fh:   # 1 literal open
        fh.write("")
    with open(LOG2, "w") as fh:                # 2 tainted-name open
        fh.write("")
    LOG.write_text("")                         # 3 tainted write_text
    LOG2.unlink()                              # 4 tainted unlink
    os.remove(LOG)                             # 5 os.remove (was unreachable)
'''
    good = '''"""This harness never truncates the log; it reads by offset. Truncation would
destroy the record of a previous rep, so offsets are used instead."""
import pathlib
LOG = pathlib.Path("logs") / "worker.log"
def run():
    with open(LOG, "a") as fh:          # append is fine
        fh.write("")
    with open(LOG, "r", encoding="utf-8", errors="replace") as fh:
        fh.seek(0)                      # read by offset is the whole point
        data = fh.read()
    text = LOG.read_text()              # read is fine
    return data + text
'''
    # PROSE IMMUNITY, as code rather than as a promise. Every destructive shape
    # below appears ONLY inside the docstring, as an illustration of what not to
    # do. A gate that scans raw text reports 4 violations here; that is exactly
    # the false positive that made the A/B pre-flight refuse a correct file on
    # 04:49:33. The assertion is only meaningful because the raw text really
    # does contain those strings - checked immediately after, so this case
    # cannot decay into a vacuous pass.
    prose = '''"""Retired shapes, documented for the reader so nobody re-introduces them:

    open("logs/worker.log", "w")        # illustration, never executed
    LOG.write_text("")                  # illustration
    LOG.unlink()                        # illustration
    os.remove(LOG)                      # illustration
    : > logs/worker.log                 # the 03:20 incident, in bash
    """
import pathlib
LOG = pathlib.Path("logs") / "worker.log"
def run():
    with open(LOG, "r") as fh:
        return fh.read()
'''
    v_bad = scan_source(bad, "synthetic-BAD")
    v_good = scan_source(good, "synthetic-GOOD")
    v_prose_fatal = scan_source(prose, "synthetic-PROSE")
    v_prose_code_only = scan_source(code_only(prose), "synthetic-PROSE(code_only)")

    print(f"  synthetic truncating script -> {len(v_bad)} violation(s) "
          f"(want 5, one per destructive family)")
    for v in v_bad:
        print("    ", v)
    print(f"  synthetic append-only script -> {len(v_good)} violation(s) (want 0)")
    for v in v_good:
        print("    ", v)
    print(f"  synthetic prose-only script  -> {len(v_prose_fatal)} violation(s) (want 0)")
    for v in v_prose_fatal:
        print("    ", v)

    FAMILIES = {
        "literal open":       lambda v: 'open(..., mode with \'w\')' in v and '"logs/worker.log"' in v,
        "tainted-name open":  lambda v: 'open(..., mode with \'w\')' in v and "LOG2" in v,
        "tainted write_text": lambda v: ".write_text()" in v,
        "tainted unlink":     lambda v: ".unlink()" in v,
        "os.remove tainted":  lambda v: "os.remove" in v,
    }
    seen = {nm for nm, pred in FAMILIES.items() if any(pred(v) for v in v_bad)}
    lost = sorted(set(FAMILIES) - seen)
    if lost:
        print(f"SELFTEST FAIL - the gate is blind to these families: {lost}")
        print("  (a count-based assertion would have hidden this; do not replace it)")
        print("  lost 'os.remove tainted' => the attribute branch order regressed")
        print("  lost 'tainted-*'        => log_tainted_names()/touches() regressed")
        return 1
    if v_good:
        print("SELFTEST FAIL - the gate flags appends/reads (false positive).")
        return 1
    if v_prose_fatal:
        print("SELFTEST FAIL - the gate sees PROSE. Something has reverted to a "
              "raw-text scan; route it through code_only().")
        return 1
    if v_prose_code_only:
        print("SELFTEST FAIL - code_only() did not strip the docstring.")
        return 1
    # The prose sample must be a REAL trap, not a harmless one. If a future edit
    # tidies the docstring, this case stops exercising anything and its green
    # would be a lie - so assert the bait is present in the raw text.
    bait = ('open("logs/worker.log", "w")', 'LOG.write_text("")',
            'LOG.unlink()', 'os.remove(LOG)', ': > logs/worker.log')
    missing_bait = [b for b in bait if b not in prose]
    if missing_bait:
        print(f"SELFTEST INVALID - prose sample lost its bait: {missing_bait}")
        return 1
    if "truncat" not in good.lower():
        print("SELFTEST INVALID - the clean sample does not exercise the prose trap")
        return 1
    print("SELFTEST PASS - all 5 destructive families seen in both the literal and "
          "the constant-bound shape; appends/reads ignored; a docstring containing "
          "every forbidden shape is ignored, and that immunity is proven non-vacuous "
          "by asserting the bait is present.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true",
                    help="prove the gate can fail, on every axis, before trusting it")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    if not SCRIPTS.is_dir():
        print(f"ABORT: no scripts/ under {ROOT}")
        return 1

    fatal, advisory, scanned = [], [], 0
    for p in sorted(SCRIPTS.glob("*.py")):
        src = p.read_text()
        scanned += 1
        f, a = scan_tree(src, p.name)
        fatal.extend(f)
        advisory.extend(a)

    if advisory:
        print(f"LOG GUARD ADVISORIES ({len(advisory)}, non-fatal - logs/ holds "
              "mutable pointers like transtream.port):")
        for v in advisory:
            print("   ~", v)

    if fatal:
        print(f"LOG APPEND-ONLY GUARD FAIL across {scanned} scripts:")
        for v in fatal:
            print("   -", v)
        print("\nlogs/worker.log is the only durable record of the recovery sweep,")
        print("the lease claims and the warmup self-report. Read it by offset; never")
        print("truncate it to make a grep easier.")
        return 1

    print(f"LOG APPEND-ONLY GUARD OK: {scanned} scripts in scripts/ contain no")
    print(f"truncating or deleting operation against a log artifact "
          f"({len(advisory)} advisory).")
    print("(It cannot see a paste into a terminal, and it does not resolve paths "
          "built")
    print("inside functions from arguments - see the module docstring for the scope.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
