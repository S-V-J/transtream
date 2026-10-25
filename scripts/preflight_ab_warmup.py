#!/usr/bin/env python3
"""
Refuse to let scripts/ab_warmup_first_take.py measure anything until its shape is
verified - and verify it against CODE, never against prose.

WHY THIS EXISTS AS A FILE. On 04:49:33 the same checks ran as a heredoc pasted
beside the harness. Check 4 fired on the harness's own DOCSTRING - which mentions
/account/keys/revoke/ only to explain that the route is retired - and sys.exit()
took down the process before check 5. Check 5 was the syntactic log check and its
synthetic-refusal proof: the control that would have caught a truncating harness.
So the harness wrote 23 KB to logs/worker.log ungated, and the numbers it printed
were never certified by the instrument built to certify them. Two lessons, both
already written down elsewhere in this project and both re-learned the hard way:
a control that lives BESIDE the thing it guards can be skipped, and a textual
check handed raw source will fire on the description of the forbidden thing.

WHAT IS DIFFERENT HERE. Every textual scan runs on code_only(src), imported from
scripts/guard_log_append_only.py - the module that owns the prose-stripping, so
there is one implementation and it is itself self-tested. Absence checks are
paired with a PRESENCE check for the correct shape, because "the retired thing is
gone" passes vacuously on an empty file: /account/keys/{kid}/revoke/ must be
found, not merely the id-less URL unfound.

CONTAMINATION RULE, restated because it is the reason this is not optional:
verify_phase8_api.finish() writes /tmp/ts_phase8_report.txt, which is the artifact
scripts/green_gate.py certifies. A harness that called finish() would have an
A/B run certified as the Phase 8 API suite. So check/bail/finish/RESULTS/
MINTED_KEYS/_CLEANUP/REPORT_PATH/BAILED must not appear as a Name or an imported
alias anywhere in this harness - collected from the AST, so discussing them at
length in a docstring (as the harness does) cannot trip it.
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
HARNESS = SCRIPTS_DIR / "ab_warmup_first_take.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from guard_log_append_only import code_only, scan_source   # noqa: E402

STDLIB_OK = {"argparse", "datetime", "json", "os", "pathlib", "re", "signal",
             "subprocess", "sys", "time", "urllib", "io", "tokenize", "ast"}
REPO_OK = {"studio", "verify_phase8_api", "guard_log_append_only",
           "preflight_ab_warmup"}
FORBIDDEN = {"check", "bail", "finish", "RESULTS", "MINTED_KEYS", "_CLEANUP",
             "REPORT_PATH", "BAILED"}

# (pattern, human label). Patterns are matched against code_only() text, so a
# docstring that quotes one of them neither satisfies nor violates the check.
MUST = (
    (r"from verify_phase8_api import Client, hdr, multipart",
     "imports exactly the three certified HTTP primitives"),
    (r'"X-CSRFToken": csrf',
     "session CSRF via header + Referer, not a form field"),
    (r"/account/keys/\{kid\}/revoke/",
     "PRESENT: the revoke URL carries the id (pair of the retired-shape check)"),
    (r'multipart\(\{\}, "file"',
     "upload goes through the certified multipart encoder"),
    (r"if not warm and saw_warmup", "cold-arm negative control is code, not intent"),
    (r"if warm and not saw_warmup", "warm-arm negative control is code, not intent"),
    (r"runnable_jobs\(\)", "queue check delegates to the ONE predicate"),
    (r"fh\.seek\(offset\)", "reads the log by offset"),
    (r"len\(csrf\) != 32", "asserts the 32-char csrftoken like S1"),
    (r"application/x-www-form-urlencoded",
     "form encoding survives - for /accounts/login/, which is a FORM endpoint"),
)
MUST_NOT = (
    (r"def classify\(", "hand-rolled classify()"),
    (r"class NoRedirect", "hand-rolled NoRedirect"),
    (r"class Client\s*:", "hand-rolled Client"),
    (r'urlencode\(\{"name"', "form-encoded key generation"),
    (r"/account/keys/revoke/", "the id-less revoke URL"),
)


def _imports(tree: ast.AST) -> set:
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    return mods


def _referenced_names(tree: ast.AST) -> set:
    """Names USED (calls) or IMPORTED (from-imports). Attributes are excluded on
    purpose: c.finish() on the certified client is a different finish()."""
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            names.add(n.func.id)
        elif isinstance(n, ast.ImportFrom):
            names |= {a.name for a in n.names}
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            names.add(n.id)
    return names


def run_preflight(verbose: bool = True) -> int:
    """0 = the harness may run. Non-zero = it must not. Called from main()."""
    def say(*a):
        if verbose:
            print(*a)

    if not HARNESS.exists():
        print(f"PREFLIGHT ABORT: {HARNESS} does not exist")
        return 1
    raw = HARNESS.read_text()
    code = code_only(raw)                      # <-- the whole point of this file
    try:
        tree = ast.parse(raw)
    except SyntaxError as exc:
        print(f"PREFLIGHT FAIL: harness does not parse: {exc}")
        return 1

    fails = []

    mods = _imports(tree)
    extra = mods - STDLIB_OK - REPO_OK
    say("  imports         :", sorted(mods))
    say("  non-stdlib      :", sorted(extra) or "NONE")
    if extra:
        fails.append(f"non-stdlib imports: {sorted(extra)}")

    hits = _referenced_names(tree) & FORBIDDEN
    say("  forbidden names :", sorted(hits) or "NONE")
    if hits:
        fails.append(f"would contaminate /tmp/ts_phase8_report.txt: {sorted(hits)}")

    for pat, label in MUST:
        ok = bool(re.search(pat, code))
        say(f"  {'OK  ' if ok else 'MISS'} {label}")
        if not ok:
            fails.append(f"missing certified shape: {label}")

    for pat, label in MUST_NOT:
        present = bool(re.search(pat, code))
        say(f"  {'MISS' if not present else 'HIT '} retired: {label}")
        if present:
            fails.append(f"retired shape present in CODE: {label}")

    fatal = scan_source(raw, HARNESS.name)
    say(f"  append-only gate on this harness -> {len(fatal)} fatal violation(s) (want 0)")
    for v in fatal:
        say("     ", v)
    if fatal:
        fails.append("harness truncates or deletes a log artifact")

    # NOT DECORATIVE: the gate that just blessed this harness must be observed
    # refusing, in the same breath. guard_layers.py --selftest,
    # guard_worker_boots.py --selftest and this line are the same rule - a check
    # that cannot stop you is a report.
    sv = scan_source('open("logs/worker.log", "w")\n', "synthetic")
    say(f"  same gate on a synthetic truncator -> {len(sv)} violation(s) (want 1)")
    if not sv:
        fails.append("guard_log_append_only.py cannot see a literal truncation; "
                     "run its --selftest before trusting anything above")

    # And the immunity this file exists to buy, proven rather than asserted:
    prose_only = '"""Do not write open("logs/worker.log", "w") in this repo."""\n'
    pv = scan_source(prose_only, "synthetic-prose")
    say(f"  gate on a docstring-only truncation -> {len(pv)} violation(s) (want 0)")
    if pv:
        fails.append("guard_log_append_only.py fires on prose; it must scan AST only")

    if fails:
        print("PREFLIGHT FAIL - ab_warmup_first_take.py must not run:")
        for f in fails:
            print("   -", f)
        return 1
    say("PREFLIGHT OK  (code_only scans, presence paired with absence, "
        "gate observed refusing, prose immunity proven)")
    return 0


def selftest() -> int:
    """This pre-flight must be observed refusing a bad harness, or it is a report."""
    bad_harness = '''"""We never call finish(); see the docstring for why not."""
import requests
from verify_phase8_api import Client, hdr, multipart, finish

def classify(x):
    return x

def main():
    c = Client("http://x")
    c.json("POST", "/account/keys/revoke/", data=b"")
    open("logs/worker.log", "w").close()
'''
    rc = _evaluate(bad_harness)
    print(f"  synthetic BAD harness -> {len(rc)} refusal(s) (want >= 4)")
    for r in rc:
        print("    ", r)
    need = ("non-stdlib", "contaminate", "retired shape present", "log artifact")
    missing = [k for k in need if not any(k in r for r in rc)]
    if missing:
        print(f"SELFTEST FAIL - this pre-flight cannot see: {missing}")
        return 1
    good_harness = HARNESS.read_text()
    grc = _evaluate(code_only(good_harness) and good_harness)
    print(f"  real harness          -> {len(grc)} refusal(s) (want 0)")
    for r in grc:
        print("    ", r)
    if grc:
        print("SELFTEST FAIL - refuses the harness it ships beside (false positive)")
        return 1
    print("SELFTEST PASS - refuses a non-stdlib, contaminating, retired-shape, "
          "log-truncating harness on all four axes, and accepts the real one.")
    return 0


def _evaluate(src: str) -> list:
    """run_preflight()'s rules against a string, as a list of refusals."""
    out = []
    code = code_only(src)
    tree = ast.parse(src)
    extra = _imports(tree) - STDLIB_OK - REPO_OK
    if extra:
        out.append(f"non-stdlib imports: {sorted(extra)}")
    hits = _referenced_names(tree) & FORBIDDEN
    if hits:
        out.append(f"would contaminate the certification artifact: {sorted(hits)}")
    for pat, label in MUST_NOT:
        if re.search(pat, code):
            out.append(f"retired shape present in CODE: {label}")
    for pat, label in MUST:
        if not re.search(pat, code):
            out.append(f"missing certified shape: {label}")
    for v in scan_source(src, "<eval>"):
        out.append(f"log artifact truncated/deleted: {v}")
    return out


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    sys.exit(run_preflight())
