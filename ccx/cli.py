"""ccx command line: daemon | mcp | doctor | codex | e2e."""

import argparse
import sys

from . import __version__


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ccx",
        description="Bridge Codex threads and Claude Code sessions as message peers.",
    )
    parser.add_argument("--version", action="version", version=f"ccx {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_daemon = sub.add_parser("daemon", help="supervise stubs for live Codex threads")
    p_daemon.add_argument("--codex-home", default=None)
    p_daemon.add_argument("--poll", type=float, default=2.0)
    p_daemon.add_argument("--verbose", action="store_true")
    sub.add_parser("mcp", help="run the stdio MCP server Codex talks to")
    sub.add_parser("doctor", help="check every protocol contract ccx depends on")

    p_codex = sub.add_parser("codex", help="launch codex attached to the app-server")
    p_codex.add_argument("args", nargs=argparse.REMAINDER)

    p_e2e = sub.add_parser("e2e", help="run the full-system acceptance suite")
    p_e2e.add_argument(
        "--only", action="append", metavar="NAME", help="run only this scenario"
    )
    p_e2e.add_argument(
        "--keep", action="store_true", help="leave the scratch dir behind for inspection"
    )

    args = parser.parse_args(argv)

    if args.command == "e2e":
        from e2e import runner

        return runner.run(only=args.only, keep=args.keep)

    if args.command == "daemon":
        from .bridged import Bridge

        return Bridge(args.codex_home, args.poll, args.verbose).run()

    if args.command == "mcp":
        from .mcp import main as mcp_main

        return mcp_main()

    if args.command == "doctor":
        return _doctor()

    print(f"ccx {args.command}: not implemented yet", file=sys.stderr)
    return 3


def _doctor():
    """Placeholder until M5 — the real version exercises all four contracts."""
    print("ccx doctor: not implemented yet (M5)")
    print("  contracts to check: Claude socket write, registry visibility,")
    print("  Codex daemon RPC, MCP _meta shape")
    return 3


if __name__ == "__main__":
    sys.exit(main())
