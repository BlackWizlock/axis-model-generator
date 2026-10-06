"""Diagnostic CLI; user-facing desktop application is a later phase."""

import argparse
import os
from pathlib import Path
import sys

from .limits import ReadError
from .package_validator import validate_package_path
from .validator import validate_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Inspect ZIP/FBX or portable packages without regulatory approval claims")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--kind", choices=("zip-fbx", "package"), default="zip-fbx")
    args = parser.parse_args(argv)
    try:
        if args.output:
            same_path = args.input.resolve() == args.output.resolve()
            same_file = args.output.exists() and os.path.samefile(args.input, args.output)
            if same_path or same_file:
                raise ValueError("Report output must not overwrite the input")
        report = validate_package_path(args.input) if args.kind == "package" else validate_path(args.input)
        serialized = report.to_json() + "\n"
        if args.output:
            args.output.write_text(serialized, encoding="utf-8")
        else:
            sys.stdout.write(serialized)
        return 1 if report.has_failures() else 0
    except (OSError, ReadError, ValueError) as exc:
        print(f"model-generator: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
