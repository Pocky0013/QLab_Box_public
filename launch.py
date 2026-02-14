#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Point d'entrée principal QLab Box."""

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="QLab Box launcher")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="Run daemon (GPIO + LEDs).")
    sub.add_parser("unpair", help="Clear pairing state (manual).")
    sub.add_parser("pair", help="Run pairing once (for debug).")

    sp = sub.add_parser("discover", help="Broadcast /workspaces and list responders.")
    sp.add_argument("--bcast", default="255.255.255.255")
    sp.add_argument("--wait", type=float, default=1.2)

    sp2 = sub.add_parser("pair-auto", help="Auto pair using broadcast discovery.")
    sp2.add_argument("--bcast", default="255.255.255.255")
    sp2.add_argument("--wait", type=float, default=1.2)

    return parser


def main() -> None:
    args = build_parser().parse_args()

    # Import retardé pour permettre `--help` même si les dépendances runtime
    # ne sont pas encore installées dans l'environnement local.
    from app import daemon, discover

    if args.cmd == "daemon":
        daemon.run_daemon()
        return
    if args.cmd == "unpair":
        daemon.force_unpair_state()
        return
    if args.cmd == "pair":
        daemon.run_pairing_auto()
        return
    if args.cmd == "discover":
        responders = discover.discover_by_broadcast(args.bcast, args.wait)
        if not responders:
            discover.qc.LOGGER.info("DISCOVER: no responders")
            return
        for ip, wsmap in responders:
            discover.qc.LOGGER.info("DISCOVER: %s workspaces=%s", ip, list(wsmap.keys()))
        return

    try:
        discover.pair_auto(args.bcast, args.wait)
        discover.qc.LOGGER.info("PAIR-AUTO done")
    except discover.PairingError as exc:
        discover.qc.LOGGER.error("PAIR-AUTO failed: %s", exc)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
