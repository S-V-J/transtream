#!/usr/bin/env python3
"""
Refuses to let a "green" claim be written unless the suite actually went green
AND actually ran to the end.

WHY THIS EXISTS. Commit 9e80209 was pushed asserting "phase.8 API suite green
61/61" and "FIRST FULLY GREEN RUN". The run behind it died at assertion 4 of 62
(S1 login POST returned 200, not 302) and S12's seeded probes have never
executed. The message was written from the PLAN's expectation instead of the
RUN's output - an expectation laundered into an assertion, and the first
manufactured-confidence incident of this project that escaped the machine into
git log, where readers who were not present will trust it.

WHY THE GATE IS STRUCTURAL AND NOT A COUNT. An earlier draft asserted
`assertions == 61`. That number was wrong (it is 62 - S12 went 4 -> 8
assertions) and, worse, the check was a PROXY: a count stands in for "ran to
completion", and the two come apart in exactly the cases that matter:
    failed=0, last=S13, count=62  -> a real green run, REFUSED by the count gate
    failed=1, last=S1             -> 23:56, truncated, count gate aborts for the
                                     wrong reason
    failed=0, last=S12 (Redis down early exit) -> proves nothing, count gate
                                     might pass it if the numbers aligned
So the gate asserts the property directly: failure-count is the CORRECTNESS
check, terminal-section presence is the COMPLETENESS check, and the count is
printed for information only. Any future edit that adds assertions therefore
does not require this file to change - which is the point, because the number
was the only part of the last draft that I had to get right and I did not.

STALE-REPORT GUARD. /tmp/ts_phase8_report.txt survives across runs, so a run
that crashed before writing one would leave the PREVIOUS run's verdict on disk
and this gate would certify something that never happened. Absent file = abort.
Always `rm -f` the report before starting a run.

Exit 0 = a green claim is now truthful. Non-zero = do not write one.
"""
import pathlib
import re
import sys

REPORT = pathlib.Path("/tmp/ts_phase8_report.txt")
TERMINAL_SECTION = "S13"

if not REPORT.exists():
    sys.exit(f"ABORT: no {REPORT} - the suite never ran to completion "
             f"(or was not preceded by 'rm -f'). Refusing to certify a stale file.")

lines = [ln for ln in REPORT.read_text().splitlines() if ln.strip()]
footer = lines[-1] if lines else ""

m = re.fullmatch(r"assertions:\s*(\d+)\s+passed:\s*(\d+)\s+failed:\s*(\d+)", footer)
if not m:
    sys.exit(f"ABORT: report footer unparsable: {footer!r}")
total, passed, failed = (int(x) for x in m.groups())

sections = [ln.split("]")[0].split("[")[-1].strip()
            for ln in lines if ln.startswith(("PASS [", "FAIL ["))]
if not sections:
    sys.exit("ABORT: report contains no assertion lines at all")

last = sections[-1]
if last != TERMINAL_SECTION:
    sys.exit(f"ABORT: TRUNCATED - last recorded section is {last}, not "
             f"{TERMINAL_SECTION}. {passed}/{total} assertions recorded, so the "
             f"run stopped early and proves nothing about what it never reached. "
             f"A green suite that ran 4 assertions is not green, it is truncated.")
if failed:
    bad = [ln for ln in lines if ln.startswith("FAIL [")]
    sys.exit(f"ABORT: NOT GREEN - {failed} of {total} assertions failed:\n   "
             + "\n   ".join(bad))

print(f"GREEN GATE PASS: {passed}/{total} assertions, 0 failures, ran through "
      f"{TERMINAL_SECTION}.")
print("A 'green' claim is now truthful. (The count is reported, not asserted - "
      "see the module docstring for why.)")
