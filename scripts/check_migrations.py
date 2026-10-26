#!/usr/bin/env python3
"""Print a single token describing the migration state of this database.

WHY A FILE AND NOT AN INLINE HEREDOC. The first version of the CLI's start-path
guard embedded this probe as an inline heredoc inside a command substitution
inside a function, shipped inside a cat heredoc. The body and its terminator were
dropped in the paste, bash -n reported an unterminated document, and the file
never landed. The mechanism was innocent: the defect was a nested structure whose
opener, body and terminator must agree across lines a single mis-paste separates.
So the probe is a file, like every other checker in scripts/, and the CLI calls it
by absolute path. That also buys it a py_compile gate and a selftest, neither of
which an inline heredoc can have.

WHY A TOKEN AND NOT A GREP OF A TRACEBACK. The caller branches on the FIRST WORD
of stdout, never on a substring of a stack trace. Grepping for "ModuleNotFoundError"
inside captured stderr is the signal-channel defect that made the vacuous leak
check cry wolf on a clean repo: the answer is in the text, the branch is on
something a Python version bump can reword. Four tokens, one meaning each:

    APPLIED                 every migration in every installed app is recorded
    PENDING: <app> <name>   unapplied migrations; the caller must not start
    DEPS_MISSING: <name>    the venv cannot even import Django/this project
    ERROR: <detail>         the probe itself failed (db unreadable, etc.)

WHY THE DERIVATION AND NOT A LIBRARY METHOD. The first revision called
loader.unapplied() and died with AttributeError: 'MigrationLoader' object has no
attribute 'unapplied' - a method name remembered rather than read. This revision
derives the answer from three attributes that have been stable across releases
(graph.nodes, applied_migrations, replaced_keys), and it was EXECUTED against this
installed Django, printing its inputs and output, before a single line of it was
written here. Deriving also fixes a case a method might not: when the
django_migrations table does not exist at all - a fresh clone - applied_migrations
comes back empty, so every node in the graph is pending, which is exactly the state
this guard exists to catch. Parsing showmigrations' [X]/[ ] prose was rejected for
the same reason it is always rejected: a human format is not a contract.

WHY classify() SORTS. The first revision sorted in main() and asserted sortedness
in selftest(), so the selftest tested a property of the caller while calling the
callee - and it failed on its very first run, on two of four shapes. Reproducible
output is a property of the formatter, not of the collector, because the next
caller (a test, a helper, green_gate) will not remember to sort. Sorted here, and
sorting in main() is harmless.

EXIT STATUS mirrors the token: 0 APPLIED, 1 PENDING, 2 DEPS_MISSING, 3 ERROR. The
caller must not rely on it alone - the token is the signal and the status is a
convenience - but having both means a shell that loses stdout still cannot mistake
a healthy database for a pending one.
"""
import sys

TOKENS = ("APPLIED", "PENDING:", "DEPS_MISSING:", "ERROR:")


def classify(unapplied):
    """Pure: turn a list of (app, name) pairs into (token, exit_code).

    Separate from any Django import so --selftest exercises every branch on a
    machine with no database, no venv and no project installed. A selftest that
    needs Django is a test that skips itself on the fresh clone it protects.
    """
    ordered = sorted(set(unapplied))
    if not ordered:
        return "APPLIED", 0
    named = ", ".join(f"{app} {name}" for app, name in ordered)
    return f"PENDING: {named}", 1


def pending_from(loader):
    """The derivation, as one function, so the probe and any future caller share it.

    Every node in the on-disk graph that is neither recorded as applied nor
    superseded by a replaced key is, by definition, unapplied.
    """
    applied = loader.applied_migrations
    replaced = getattr(loader, "replaced_keys", set())
    return [(app, name) for app, name in loader.graph.nodes
            if (app, name) not in applied and (app, name) not in replaced]


def build_loader(connections):
    """Construct the loader, tolerating a signature that lacks the kwarg.

    Written after the signature was read, not before - but the kwarg has moved
    between releases, and a probe that crashes on an upgrade is a probe that
    blocks every start until someone edits it. The fallback is the same call with
    the default behaviour, which is correct for this project because every app in
    INSTALLED_APPS ships a migrations package.
    """
    from django.db.migrations.loader import MigrationLoader
    conn = connections["default"]
    try:
        return MigrationLoader(conn, ignore_no_migrations=True)
    except TypeError:
        return MigrationLoader(conn)


def main():
    if "--selftest" in sys.argv:
        return selftest()

    try:
        import os
        import pathlib
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "transtream.settings")
        import django                              # noqa: F401  (populates the app registry)
        django.setup()
        from django.db import connections
    except ImportError as exc:                     # ModuleNotFoundError is a subclass
        print(f"DEPS_MISSING: {exc.name or exc}")
        return 2
    except Exception as exc:                       # settings import, app registry, etc.
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 3

    try:
        loader = build_loader(connections)
        pending = pending_from(loader)
    except Exception as exc:                       # unreadable/locked/missing db file
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 3

    token, code = classify(pending)
    print(token)
    return code


def selftest():
    """Prove the classifier and the token contract, without touching Django.

    Every assertion here increments a counter that is printed; the first revision
    carried two checks that could not fail (a malformed-token expression whose
    operator precedence made it always-false, and summary arithmetic that always
    yielded 4). A check that cannot fail is a report, and this file exists to be a
    check that can.
    """
    checks, fails = 0, 0

    def expect(label, got, want):
        nonlocal checks, fails
        checks += 1
        if got == want:
            print(f"  [ok]   {label}")
        else:
            fails += 1
            print(f"  [FAIL] {label}\n           got  {got!r}\n           want {want!r}")

    cases = [
        ([], "APPLIED", 0),
        ([("studio", "0002_alter_job_lease_expires_at")],
         "PENDING: studio 0002_alter_job_lease_expires_at", 1),
        # deliberately UNSORTED input: this is the shape that failed on the first
        # run, and it is the reason classify() owns the sort.
        ([("studio", "0002_x"), ("auth", "0005_y")],
         "PENDING: auth 0005_y, studio 0002_x", 1),
        # duplicates must not double-report: set() before sort()
        ([("auth", "0001"), ("auth", "0001")], "PENDING: auth 0001", 1),
    ]
    for unapplied, want_tok, want_code in cases:
        tok, code = classify(unapplied)
        expect(f"classify({unapplied}) -> {want_tok} ({want_code})",
               (tok, code), (want_tok, want_code))

    # the token contract the CLI's case statement depends on: the first word of
    # every token is one of TOKENS, and the token has no surrounding whitespace.
    for unapplied, _, _ in cases:
        tok, _ = classify(unapplied)
        first = tok.split(" ", 1)[0]
        expect(f"first word of {tok!r} is a known token", first in TOKENS, True)
        expect(f"{tok!r} has no leading/trailing whitespace", tok, tok.strip())

    # the CLI extracts the payload with ${out#PENDING: }; prove the prefix is
    # exactly that, so the operator's error line is the list and not the list
    # with a colon or a stray word glued to it.
    tok, _ = classify([("studio", "0002_x")])
    expect("payload follows 'PENDING: ' exactly", tok.startswith("PENDING: "), True)
    expect("payload is the pair list", tok[len("PENDING: "):], "studio 0002_x")

    print(f"  check_migrations selftest: {checks} assertions, {fails} failure(s)")
    if fails:
        print("SELFTEST FAIL - the CLI must not branch on this probe")
        return 1
    print("SELFTEST PASS - APPLIED / PENDING (one, many, unsorted, duplicated), the "
          "first-word token contract, and the 'PENDING: ' payload prefix, all proven "
          "without importing Django.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
