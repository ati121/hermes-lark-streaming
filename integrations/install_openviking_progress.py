"""Install/uninstall the opt-in progress extension in an OpenViking environment.

Run with that environment's Python. --check performs no writes. A service
restart is needed after installation; this command never restarts anything.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
from pathlib import Path

INJECTION = (
    "    # hermes-lark-streaming: real retrieval progress\n"
    "    from openviking._hermes_progress import install as _install_hermes_progress\n"
    "    _install_hermes_progress(app)\n"
)


def patched_app(source: str) -> str:
    if INJECTION in source:
        return source
    tree = ast.parse(source)
    factory = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "create_app"), None)
    if factory is None:
        raise ValueError("Unsupported OpenViking: create_app not found")
    returns = [n for n in factory.body if isinstance(n, ast.Return) and isinstance(n.value, ast.Name) and n.value.id == "app"]
    if len(returns) != 1:
        raise ValueError("Unsupported OpenViking: expected one top-level return app")
    lines = source.splitlines(keepends=True)
    lines.insert(returns[0].lineno - 1, INJECTION)
    patched = "".join(lines)
    ast.parse(patched)
    return patched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    spec = importlib.util.find_spec("openviking")
    if spec is None or spec.origin is None:
        parser.error("Run with the Python environment that contains OpenViking")
    root = Path(spec.origin).parent
    app = root / "server/app.py"
    extension = root / "_hermes_progress.py"
    source = app.read_text(encoding="utf-8")
    if args.uninstall:
        updated = source.replace(INJECTION, "")
    else:
        for relative, signature in (
            ("retrieve/intent_analyzer.py", "async def analyze("),
            ("retrieve/hierarchical_retriever.py", "async def retrieve("),
        ):
            if signature not in (root / relative).read_text(encoding="utf-8"):
                parser.error(f"Unsupported OpenViking entry point: {relative}")
        updated = patched_app(source)
    if args.check:
        if INJECTION in source and not extension.is_file():
            parser.error("Progress initialization exists but its module is missing; reinstall the extension")
        print("Progress extension: " + ("installed" if INJECTION in source else "compatible, not installed"))
        return
    if args.install:
        content = Path(__file__).with_name("openviking_progress.py").read_text(encoding="utf-8")
        temporary = extension.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(extension)
    temporary = app.with_suffix(".hls-progress.tmp")
    temporary.write_text(updated, encoding="utf-8")
    temporary.replace(app)
    if args.uninstall:
        extension.unlink(missing_ok=True)
    print("Progress extension " + ("installed" if args.install else "removed") + "; restart OpenViking to apply.")


if __name__ == "__main__":
    main()
