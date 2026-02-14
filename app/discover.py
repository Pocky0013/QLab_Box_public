#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Découverte QLab en OSC (broadcast), puis pairing (main/backup/aux).

Principes :
- On ne valide QLab que via la réponse /workspaces (parse_workspaces()).
- Discovery en 2 phases, et la phase 2 est systématique (sécurise udpReplyPort / alwaysReply / forgetMeNot).
- Pairing déterministe :
  - 1 seul workspace "sans suffixe" => MAIN
  - "_main" et "_backup" : on choisit un couple cohérent (même base) en priorité
  - "_aux1" : optionnel, unique (sinon conflit)

Gestion d'ambiguïtés / conflits :
- >1 workspace sans suffixe => CONFLIT (bloquant)
- doublon exact "xxxx_main" (deux machines différentes) => CONFLIT (bloquant)
- plusieurs bases "_main" sans couple "_backup" => CONFLIT (bloquant)
"""

import argparse
import json
import time
from dataclasses import dataclass
from typing import Dict, Tuple, List, Optional

from app import core as qc


class PairingError(RuntimeError):
    pass

class NoRespondersError(PairingError):
    pass

class ConflictError(PairingError):
    pass


@dataclass(frozen=True)
class Candidate:
    ip: str
    ws_name: str
    ws_id: str
    kind: str   # "main" | "backup" | "aux" | "plain"
    base: str   # base du nom (avant suffixe)


def _classify(ws_name: str) -> Tuple[str, str]:
    """
    Retourne (kind, base).
    """
    # compat : noms fixes
    if ws_name == qc.EXPECTED_WS_MAIN:
        return ("main", "__legacy_expected__")
    if ws_name == qc.EXPECTED_WS_BACKUP:
        return ("backup", "__legacy_expected__")

    if ws_name.endswith(qc.SUFFIX_MAIN):
        return ("main", ws_name[: -len(qc.SUFFIX_MAIN)])
    if ws_name.endswith(qc.SUFFIX_BACKUP):
        return ("backup", ws_name[: -len(qc.SUFFIX_BACKUP)])
    if ws_name.endswith(qc.SUFFIX_AUX1):
        return ("aux", ws_name[: -len(qc.SUFFIX_AUX1)])

    return ("plain", ws_name)


def decide_roles(responders: List[Tuple[str, Dict[str, str]]]) -> Dict[str, qc.Endpoint]:
    """
    Applique les règles définies (main/backup par base, aux1 optionnel).
    Renvoie un dict role -> Endpoint.
    Peut lever ConflictError / NoRespondersError.
    """
    if not responders:
        raise NoRespondersError("No QLab responders.")

    cands: List[Candidate] = []
    for ip, wsmap in responders:
        for ws_name, ws_id in wsmap.items():
            kind, base = _classify(ws_name)
            if isinstance(ws_id, str) and ws_id:
                cands.append(Candidate(ip=ip, ws_name=ws_name, ws_id=ws_id, kind=kind, base=base))

    if not cands:
        raise NoRespondersError("No valid QLab workspaces.")

    # Aux1 (unique)
    aux_cands = [c for c in cands if c.kind == "aux"]
    if len(aux_cands) > 1:
        raise ConflictError("Multiple *_aux1 candidates found.")
    aux_ep: Optional[qc.Endpoint] = None
    if len(aux_cands) == 1:
        c = aux_cands[0]
        aux_ep = qc.Endpoint(ip=c.ip, role="aux", workspace_name=c.ws_name, workspace_id=c.ws_id)

    # main/backup by base (détecte doublons exacts)
    by_base: Dict[str, Dict[str, Candidate]] = {}
    for c in cands:
        if c.kind not in ("main", "backup"):
            continue
        by_base.setdefault(c.base, {})
        if c.kind in by_base[c.base]:
            raise ConflictError(f"Duplicate {c.base}_{c.kind} candidate.")
        by_base[c.base][c.kind] = c

    # 1) priorité : une base qui a main+backup
    complete_bases = [b for b, mm in by_base.items() if "main" in mm and "backup" in mm]
    selected_base: Optional[str] = None
    if complete_bases:
        complete_bases.sort()
        selected_base = complete_bases[0]
        if len(complete_bases) > 1:
            qc.LOGGER.warning("PAIR: multiple complete bases=%s -> pick '%s'", complete_bases, selected_base)
    else:
        # 2) mains suffixés sans backup : doit être unique (sinon ambigu)
        main_bases = [b for b, mm in by_base.items() if "main" in mm]
        if len(main_bases) > 1:
            main_bases.sort()
            raise ConflictError(f"Multiple *_main bases found (no matching *_backup): {main_bases}")
        if len(main_bases) == 1:
            selected_base = main_bases[0]

    assigned: Dict[str, qc.Endpoint] = {}

    if selected_base is not None:
        m = by_base[selected_base].get("main")
        if not m:
            raise ConflictError(f"Selected base '{selected_base}' has no main.")
        assigned["main"] = qc.Endpoint(ip=m.ip, role="main", workspace_name=m.ws_name, workspace_id=m.ws_id)

        b = by_base[selected_base].get("backup")
        if b:
            assigned["backup"] = qc.Endpoint(ip=b.ip, role="backup", workspace_name=b.ws_name, workspace_id=b.ws_id)
    else:
        # 3) fallback : un seul workspace "plain"
        plains = [c for c in cands if c.kind == "plain"]
        if len(plains) == 0:
            raise NoRespondersError("No selectable workspace (need *_main or a single plain workspace).")
        if len(plains) > 1:
            names = sorted({p.ws_name for p in plains})
            raise ConflictError(f"Multiple plain workspaces found (need suffixes): {names}")

        p = plains[0]
        assigned["main"] = qc.Endpoint(ip=p.ip, role="main", workspace_name=p.ws_name, workspace_id=p.ws_id)

    if aux_ep:
        assigned["aux"] = aux_ep

    return assigned


def discover_by_broadcast(bcast_ip: str = "255.255.255.255", wait_sec: float = 1.2) -> List[Tuple[str, Dict[str, str]]]:
    """
    Discovery en 2 phases (phase 2 systématique).

    Phase 1 :
      - broadcast /workspaces

    Phase 2 :
      - broadcast udpReplyPort/alwaysReply/forgetMeNot
      - broadcast /workspaces

    Résultat :
      - merge unique par IP
    """
    def _sleep_window() -> None:
        t0 = qc.mono()
        while qc.mono() - t0 < wait_sec:
            time.sleep(0.05)

    def _log_snapshot(tag: str) -> List[Tuple[str, Dict[str, str]]]:
        snap = qc.DISCOVERY.snapshot()
        qc.LOGGER.debug("%s: discovery store has %d IP(s): %s", tag, len(snap), sorted(list(snap.keys())))

        parsed: List[Tuple[str, Dict[str, str]]] = []
        for ip, payload in snap.items():
            status = payload.get("status")
            addr_field = payload.get("address")
            data = payload.get("data")
            qc.LOGGER.debug(
                "%s: RX ip=%s status=%s addr_field=%s data_type=%s data_len=%s",
                tag, ip, status, addr_field, type(data).__name__,
                len(data) if isinstance(data, list) else None
            )

            wsmap = qc.parse_workspaces(payload)
            if wsmap:
                qc.LOGGER.debug("%s: PARSED ip=%s workspaces=%s", tag, ip, list(wsmap.keys()))
                parsed.append((ip, wsmap))
            else:
                try:
                    raw = json.dumps(payload, ensure_ascii=False)
                    qc.LOGGER.debug("%s: PARSE_FAIL ip=%s raw=%s", tag, ip, raw[:260] + ("..." if len(raw) > 260 else ""))
                except Exception:
                    qc.LOGGER.debug("%s: PARSE_FAIL ip=%s (raw dump failed)", tag, ip)

        parsed.sort(key=lambda x: x[0])
        return parsed

    qc.start_osc_server()

    qc.DISCOVERY.clear()
    qc.LOGGER.debug("DISCOVER phase1: broadcast /workspaces bcast=%s port=%d wait=%.2fs", bcast_ip, qc.QLAB_PORT, wait_sec)
    qc.osc_broadcast_send(bcast_ip, qc.QLAB_PORT, qc.P_WORKSPACES, [])
    _sleep_window()
    r1 = _log_snapshot("DISCOVER phase1")

    qc.DISCOVERY.clear()
    qc.LOGGER.debug(
        "DISCOVER phase2: broadcast flags + /workspaces bcast=%s port=%d reply_port=%d wait=%.2fs",
        bcast_ip, qc.QLAB_PORT, qc.PI_REPLY_PORT, wait_sec
    )
    qc.osc_broadcast_send(bcast_ip, qc.QLAB_PORT, qc.P_UDP_REPLY_PORT, [qc.PI_REPLY_PORT])
    qc.osc_broadcast_send(bcast_ip, qc.QLAB_PORT, qc.P_ALWAYS_REPLY, [1])
    qc.osc_broadcast_send(bcast_ip, qc.QLAB_PORT, qc.P_FORGET_ME_NOT, [1])
    qc.osc_broadcast_send(bcast_ip, qc.QLAB_PORT, qc.P_WORKSPACES, [])
    _sleep_window()
    r2 = _log_snapshot("DISCOVER phase2")

    merged: Dict[str, Dict[str, str]] = {}
    for ip, wsmap in r1:
        merged[ip] = wsmap
    for ip, wsmap in r2:
        merged[ip] = wsmap

    responders = sorted([(ip, wsmap) for ip, wsmap in merged.items()], key=lambda x: x[0])
    qc.LOGGER.debug("DISCOVER done: %d responder(s) parsed as QLab", len(responders))
    return responders


def pair_auto(bcast_ip: str = "255.255.255.255", wait_sec: float = 1.2) -> Dict[str, qc.Endpoint]:
    qc.start_osc_server()

    responders = discover_by_broadcast(bcast_ip=bcast_ip, wait_sec=wait_sec)
    if not responders:
        raise NoRespondersError("PAIR-AUTO failed: no QLab responders.")

    qc.LOGGER.info("DISCOVER responders: " + " | ".join([f"{ip}({len(ws)})" for ip, ws in responders]))

    assigned = decide_roles(responders)

    for role, ep in assigned.items():
        qc.ensure_app_flags(ep.ip, force=True)
        qc.ensure_connected(ep, force=True)
        qc.LOGGER.info("PAIR lock %s ip=%s ws='%s' id=%s", role.upper(), ep.ip, ep.workspace_name, ep.workspace_id)

    qc.save_paired_state(assigned)
    return assigned


def main() -> None:
    p = argparse.ArgumentParser(description="QLab discovery/pairing tool (OSC broadcast).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("discover", help="Broadcast /workspaces and list responders.")
    sp.add_argument("--bcast", default="255.255.255.255")
    sp.add_argument("--wait", type=float, default=1.2)

    sp2 = sub.add_parser("pair-auto", help="Auto pair using broadcast discovery.")
    sp2.add_argument("--bcast", default="255.255.255.255")
    sp2.add_argument("--wait", type=float, default=1.2)

    args = p.parse_args()

    if args.cmd == "discover":
        responders = discover_by_broadcast(args.bcast, args.wait)
        if not responders:
            qc.LOGGER.info("DISCOVER: no responders")
            return
        for ip, wsmap in responders:
            qc.LOGGER.info("DISCOVER: %s workspaces=%s", ip, list(wsmap.keys()))
        return

    if args.cmd == "pair-auto":
        try:
            pair_auto(args.bcast, args.wait)
            qc.LOGGER.info("PAIR-AUTO done")
        except PairingError as e:
            qc.LOGGER.error("PAIR-AUTO failed: %s", e)
            raise SystemExit(2)


if __name__ == "__main__":
    main()
