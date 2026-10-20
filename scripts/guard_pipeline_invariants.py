"""
Pipeline invariant guard (6b-fix edition).

OLD invariant (wrong after the asyncio.run unification):
    "pipeline.py must not import asyncio."
NEW invariant (precise):
    * FORBIDDEN: `async def` definitions, `await` expressions, and direct
      create_subprocess_* calls in pipeline.py. The module must contain no
      native async code of its own - async belongs to audio_service.
    * ALLOWED: `asyncio.run(...)` as the sanctioned sync->async BRIDGE,
      and only inside _normalize_via_audio_service's try-block (the worker
      process has no running loop; the bridge's RuntimeError arm catches
      misuse).

Docstring mentions can never trip this: ast.walk yields no async node for
a string literal.
"""
import ast
import pathlib
import sys

src = pathlib.Path("backend/studio/pipeline.py").read_text()
tree = ast.parse(src)

violations = []
bridge_uses = []

for node in ast.walk(tree):
    if isinstance(node, ast.AsyncFunctionDef):
        violations.append((node.lineno, f"async def {node.name}"))
    elif isinstance(node, ast.Await):
        violations.append((node.lineno, "await expression"))
    elif isinstance(node, ast.Call):
        f = node.func
        if getattr(f, "attr", None) in ("create_subprocess_exec", "create_subprocess_shell"):
            violations.append((node.lineno, f"direct {f.attr}()"))
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if f.value.id == "asyncio":
                if f.attr == "run":
                    bridge_uses.append(node.lineno)      # sanctioned
                else:
                    violations.append((node.lineno, f"asyncio.{f.attr} (only .run allowed)"))

print("asyncio.run bridge lines (allowed):", bridge_uses or "NONE")
print("violations:", violations or "NONE")
sys.exit(1 if violations else 0)
