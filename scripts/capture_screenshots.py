"""Capture CLI rich output to SVG for README screenshots.

Monkey-patches scryland.cli.console to a recording Console, then invokes
commands directly (bypassing CliRunner so rich styling survives).
"""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

import scryland.cli as cli_mod

OUT = Path(__file__).parent.parent / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)


def capture(name: str, args: list[str]) -> None:
    recorder = Console(record=True, width=110, force_terminal=True, color_system="truecolor")
    cli_mod.console = recorder
    # Also patch the rich RichHandler the logger uses so log lines land in
    # the recording too.
    try:
        cli_mod.cli.main(args, standalone_mode=False)
    except SystemExit:
        pass
    except Exception as e:
        recorder.print(f"[red]ERROR:[/red] {e}")
    svg = OUT / f"{name}.svg"
    recorder.save_svg(str(svg), title=f"scryland {' '.join(args)}")
    print(f"wrote {svg}")


if __name__ == "__main__":
    targets = {
        "doctor": ["doctor"],
        "status": ["status"],
        "sales-report": ["sales-report"],
    }
    only = sys.argv[1:]
    for name, args in targets.items():
        if only and name not in only:
            continue
        capture(name, args)
