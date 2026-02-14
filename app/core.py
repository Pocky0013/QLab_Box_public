#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Core partagé QLabTrigger.

Objectifs :
- Centraliser les briques “stables” (OSC send/receive, state, parsing workspaces, connect/flags)
- Réduire la duplication entre le daemon et l’outil de découverte
- Garder les chemins critiques courts et prévisibles

Ce module ne démarre rien “tout seul” : c’est app/daemon.py / app/discover.py qui orchestrent.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, List, Callable

from pythonosc import dispatcher
from pythonosc import osc_server
from pythonosc import udp_client
from pythonosc.osc_message_builder import OscMessageBuilder

from config.loader import load_user_config

cfg = load_user_config()

# =========================
# CONFIG (surcharge via config/user_config.py)
# =========================
QLAB_PORT = getattr(cfg, "QLAB_PORT", 53000)
PI_LISTEN_IP = getattr(cfg, "PI_LISTEN_IP", "0.0.0.0")
PI_REPLY_PORT = getattr(cfg, "PI_REPLY_PORT", 53001)

# Noms "historiques" (compatibilité)
EXPECTED_WS_MAIN = getattr(cfg, "EXPECTED_WS_MAIN", "show_main")
EXPECTED_WS_BACKUP = getattr(cfg, "EXPECTED_WS_BACKUP", "show_backup")

# Suffixes recommandés (naming)
SUFFIX_MAIN = getattr(cfg, "SUFFIX_MAIN", "_main")
SUFFIX_BACKUP = getattr(cfg, "SUFFIX_BACKUP", "_backup")
SUFFIX_AUX1 = getattr(cfg, "SUFFIX_AUX1", "_aux1")

OSC_PASSCODE = getattr(cfg, "OSC_PASSCODE", "7777")

P_UDP_REPLY_PORT = "/udpReplyPort"
P_ALWAYS_REPLY = "/alwaysReply"
P_FORGET_ME_NOT = "/forgetMeNot"
P_WORKSPACES = "/workspaces"

HEARTBEAT_INTERVAL = 2.0
OFFLINE_AFTER = 8.0

CONNECT_REFRESH_EVERY = 6.0
APPFLAGS_REFRESH_EVERY = 10.0

LOG_DIR = getattr(cfg, "LOG_DIR", "/var/log/qlab-box")
STATE_DIR = getattr(cfg, "STATE_DIR", "/var/lib/qlab-box")
LOG_FILE = os.path.join(LOG_DIR, "qlab-box.log")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

ROLES = ("main", "backup", "aux")


# =========================
# TIME (monotonic)
# =========================
def mono() -> float:
    return time.monotonic()


# =========================
# LOGGING
# =========================
def setup_logging(name: str = "qlab-box") -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # debug via env
    if os.getenv("QLABBOX_DEBUG", "").strip().lower() in ("1", "true", "yes"):
        logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    if not logger.handlers:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        ch.setLevel(logger.level)
        logger.addHandler(ch)

        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.setLevel(logger.level)
        logger.addHandler(fh)

    return logger


LOGGER = setup_logging()


# =========================
# STATE (cache + atomic save)
# =========================
class StateManager:
    """
    Lecture cache (mtime) + écriture atomique.
    - évite le spam disque dans une boucle daemon
    - garantit un state.json valide même en coupure alim (os.replace + fsync)
    """
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._cache: Dict[str, Any] = {}
        self._mtime: float = -1.0

    def _read_file(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                x = json.load(f)
                return x if isinstance(x, dict) else {}
        except Exception:
            return {}

    def load(self) -> Dict[str, Any]:
        with self._lock:
            try:
                mtime = os.stat(self.path).st_mtime
            except Exception:
                mtime = -1.0

            if mtime != self._mtime:
                self._cache = self._read_file()
                self._mtime = mtime
            return dict(self._cache)

    def save(self, st: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"

        with self._lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp, self.path)

            # fsync dir (durabilité)
            try:
                dfd = os.open(os.path.dirname(self.path), os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except Exception:
                pass

            self._cache = dict(st)
            try:
                self._mtime = os.stat(self.path).st_mtime
            except Exception:
                self._mtime = -1.0


STATE = StateManager(STATE_FILE)


# =========================
# ENDPOINT MODEL
# =========================
@dataclass
class Endpoint:
    ip: str
    role: str  # "main" | "backup" | "aux"
    workspace_name: Optional[str] = None
    workspace_id: Optional[str] = None
    last_seen_mono: float = 0.0

    @property
    def online(self) -> bool:
        return self.last_seen_mono > 0 and (mono() - self.last_seen_mono) < OFFLINE_AFTER


# =========================
# WAITERS (requests that expect reply)
# =========================
class ReplyWaiter:
    """
    Petit mécanisme de wait/notify (par clé).
    Permet d’attendre /workspaces ou /connect sur un IP connu.
    """
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: Dict[str, threading.Event] = {}
        self._payloads: Dict[str, Any] = {}

    def arm(self, key: str) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._events[key] = ev
            self._payloads.pop(key, None)
        return ev

    def set(self, key: str, payload: Any) -> None:
        with self._lock:
            self._payloads[key] = payload
            ev = self._events.get(key)
            if ev:
                ev.set()

    def pop(self, key: str) -> Any:
        with self._lock:
            self._events.pop(key, None)
            return self._payloads.pop(key, None)

    def cleanup(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
            self._payloads.pop(key, None)


WAITERS = ReplyWaiter()


# =========================
# DISCOVERY STORE (IP unknown a priori)
# =========================
class DiscoveryStore:
    """
    Collecte passive des /reply/workspaces (utile pour broadcast).
    """
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: Dict[str, Dict[str, Any]] = {}

    def clear(self) -> None:
        with self._lock:
            self._map.clear()

    def add(self, ip: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._map[ip] = payload

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._map)


DISCOVERY = DiscoveryStore()


# =========================
# ROLE MAP (ip -> role)
# =========================
ROLE_BY_IP: Dict[str, str] = {}
_role_lock = threading.Lock()

def refresh_role_map_from_state(st: Dict[str, Any]) -> None:
    mapping: Dict[str, str] = {}
    eps = st.get("endpoints", {})
    if isinstance(eps, dict):
        for role in ROLES:
            e = eps.get(role)
            if isinstance(e, dict):
                ip = e.get("ip")
                if isinstance(ip, str) and ip:
                    mapping[ip] = role
    with _role_lock:
        ROLE_BY_IP.clear()
        ROLE_BY_IP.update(mapping)

def role_from_ip(ip: str) -> Optional[str]:
    with _role_lock:
        return ROLE_BY_IP.get(ip)


# =========================
# LAST-SEEN (by IP)
# =========================
LAST_SEEN_BY_IP: Dict[str, float] = {}
_last_seen_lock = threading.Lock()

def mark_seen(ip: str) -> None:
    with _last_seen_lock:
        LAST_SEEN_BY_IP[ip] = mono()


# =========================
# OSC SEND (queue worker)
# =========================
OSC_CLIENTS: Dict[str, udp_client.SimpleUDPClient] = {}
_clients_lock = threading.Lock()

def get_client(ip: str, port: int = QLAB_PORT) -> udp_client.SimpleUDPClient:
    with _clients_lock:
        c = OSC_CLIENTS.get(ip)
        if c is None:
            c = udp_client.SimpleUDPClient(ip, port)
            OSC_CLIENTS[ip] = c
        return c


class SendWorker(threading.Thread):
    """
    Un seul thread d’émission OSC.
    Avantage : pas de threads jetables à chaque action, pas de blocage des callbacks GPIO.
    """
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.q: "queue.Queue[tuple[str, str, Any]]" = queue.Queue(maxsize=1000)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def send(self, ip: str, path: str, arg: Any) -> None:
        try:
            self.q.put_nowait((ip, path, arg))
        except queue.Full:
            LOGGER.warning("OSC TX queue full -> drop")

    def run(self) -> None:
        while not self._stop:
            try:
                ip, path, arg = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                get_client(ip).send_message(path, arg)
            except Exception as e:
                LOGGER.debug("OSC TX error ip=%s path=%s: %s", ip, path, e)
            finally:
                self.q.task_done()


SENDW = SendWorker()
SENDW.start()


def send_app(ip: str, path: str, *args: Any) -> None:
    if args:
        SENDW.send(ip, path, args[0] if len(args) == 1 else list(args))
    else:
        SENDW.send(ip, path, [])

def send_ws(ip: str, wsid: str, suffix: str, *args: Any) -> str:
    full = f"/workspace/{wsid}/{suffix}".replace("//", "/")
    if args:
        SENDW.send(ip, full, args[0] if len(args) == 1 else list(args))
    else:
        SENDW.send(ip, full, [])
    return full

def send_ws_fast(ip: str, wsid: str, suffix: str) -> str:
    full = f"/workspace/{wsid}/{suffix}".replace("//", "/")
    SENDW.send(ip, full, [])
    return full


# =========================
# OSC SERVER (singleton)
# =========================
OSC_SERVER = None
_on_ack: Optional[Callable[[str, str], None]] = None  # callback(ip, role)

def set_ack_callback(cb: Optional[Callable[[str, str], None]]) -> None:
    global _on_ack
    _on_ack = cb

def _osc_handler(client_address, address: str, *args: Any) -> None:
    src_ip = client_address[0] if client_address else "0.0.0.0"
    payload = args[0] if args else None

    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8", errors="ignore")
        except Exception:
            return
    if not isinstance(payload, str):
        return
    if len(payload) > 200_000:
        return

    try:
        j = json.loads(payload)
    except Exception:
        return
    if not isinstance(j, dict):
        return

    invoked = j.get("address", "")
    wsid = j.get("workspace_id")
    status = j.get("status")

    if address.startswith("/reply/workspaces") or invoked == "/workspaces":
        WAITERS.set(f"workspaces:{src_ip}", j)
        DISCOVERY.add(src_ip, j)
        return

    if isinstance(invoked, str) and invoked.endswith("/connect") and isinstance(wsid, str) and wsid:
        WAITERS.set(f"connect:{src_ip}:{wsid}", j)
        if status == "ok":
            mark_seen(src_ip)
        return

    if isinstance(invoked, str) and invoked.endswith("/thump") and isinstance(wsid, str) and wsid:
        if status == "ok":
            mark_seen(src_ip)
        return

    if status == "ok" and isinstance(wsid, str) and wsid and isinstance(invoked, str):
        for suffix in ("/go", "/panic", "/stop", "/pause", "/resume", "/select/next", "/select/previous"):
            if invoked.endswith(suffix):
                mark_seen(src_ip)
                r = role_from_ip(src_ip)
                if r and _on_ack:
                    _on_ack(src_ip, r)
                break


def start_osc_server() -> None:
    global OSC_SERVER
    if OSC_SERVER is not None:
        return

    disp = dispatcher.Dispatcher()
    disp.set_default_handler(_osc_handler, needs_reply_address=True)

    server = osc_server.ThreadingOSCUDPServer((PI_LISTEN_IP, PI_REPLY_PORT), disp)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    OSC_SERVER = server
    LOGGER.warning("SYS OSC server listening on %s:%d", PI_LISTEN_IP, PI_REPLY_PORT)


# =========================
# QLAB PROTOCOL HELPERS
# =========================
def ensure_app_flags(ip: str, force: bool = False, _last: Dict[str, float] = {}) -> None:
    now = mono()
    if not force and (now - _last.get(ip, 0.0)) < APPFLAGS_REFRESH_EVERY:
        return
    _last[ip] = now
    send_app(ip, P_UDP_REPLY_PORT, PI_REPLY_PORT)
    send_app(ip, P_ALWAYS_REPLY, 1)
    send_app(ip, P_FORGET_ME_NOT, 1)


def parse_workspaces(reply_json: Dict[str, Any]) -> Dict[str, str]:
    if not isinstance(reply_json, dict) or reply_json.get("status") != "ok":
        return {}
    data = reply_json.get("data")
    if not isinstance(data, list):
        return {}
    out: Dict[str, str] = {}
    for it in data:
        if not isinstance(it, dict):
            continue
        name = it.get("displayName") or it.get("name") or it.get("fileName")
        uid = it.get("uniqueID") or it.get("id") or it.get("workspace_id")
        if isinstance(name, str) and isinstance(uid, str):
            base = name.replace(".qlab5", "").replace(".qlab4", "")
            out[base] = uid
    return out


def request_workspaces(ip: str, timeout: float = 0.9) -> Optional[Dict[str, Any]]:
    key = f"workspaces:{ip}"
    ev = WAITERS.arm(key)
    send_app(ip, P_WORKSPACES)
    if not ev.wait(timeout):
        WAITERS.cleanup(key)
        return None
    payload = WAITERS.pop(key)
    return payload if isinstance(payload, dict) else None


def connect_endpoint(ep: Endpoint, timeout: float = 0.7) -> bool:
    if not ep.workspace_id:
        return False
    key = f"connect:{ep.ip}:{ep.workspace_id}"
    ev = WAITERS.arm(key)

    if OSC_PASSCODE:
        send_ws(ep.ip, ep.workspace_id, "connect", OSC_PASSCODE)
    else:
        send_ws(ep.ip, ep.workspace_id, "connect")

    if not ev.wait(timeout):
        WAITERS.cleanup(key)
        return False

    payload = WAITERS.pop(key)
    if isinstance(payload, dict) and payload.get("status") == "ok":
        mark_seen(ep.ip)
        return True
    return False


def ensure_connected(ep: Endpoint, force: bool = False, _last: Dict[str, float] = {}) -> None:
    if not ep.workspace_id:
        return
    now = mono()
    if not force and (now - _last.get(ep.ip, 0.0)) < CONNECT_REFRESH_EVERY:
        return
    _last[ep.ip] = now

    if connect_endpoint(ep):
        return

    ensure_app_flags(ep.ip, force=True)
    connect_endpoint(ep)


# =========================
# BROADCAST OSC (raw UDP + SO_BROADCAST)
# =========================
def osc_broadcast_send(bcast_ip: str, port: int, path: str, args: List[Any]) -> None:
    msg = OscMessageBuilder(address=path)
    for a in args:
        msg.add_arg(a)
    dgram = msg.build().dgram

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.sendto(dgram, (bcast_ip, port))
    finally:
        s.close()


# =========================
# STATE FORMAT (helpers)
# =========================
def load_paired_endpoints() -> Dict[str, Endpoint]:
    st = STATE.load()
    if not st.get("paired"):
        raise SystemExit("Not paired. Run discovery/pairing first.")
    eps = st.get("endpoints", {})
    out: Dict[str, Endpoint] = {}
    if not isinstance(eps, dict):
        return out

    for role in ROLES:
        e = eps.get(role)
        if not isinstance(e, dict):
            continue
        ip = e.get("ip")
        wsid = e.get("workspace_id")
        wsn = e.get("workspace_name")
        if isinstance(ip, str) and isinstance(wsid, str):
            out[role] = Endpoint(ip=ip, role=role, workspace_name=wsn, workspace_id=wsid)
    return out


def save_paired_state(assigned: Dict[str, Endpoint]) -> None:
    old = STATE.load()
    st = {
        "paired": True,
        "paired_at": time.time(),
        "qlab_port": QLAB_PORT,
        "pi_reply_port": PI_REPLY_PORT,
        "expected_ws_main": EXPECTED_WS_MAIN,
        "expected_ws_backup": EXPECTED_WS_BACKUP,
        "suffix_main": SUFFIX_MAIN,
        "suffix_backup": SUFFIX_BACKUP,
        "suffix_aux1": SUFFIX_AUX1,
        "endpoints": {
            role: {"ip": ep.ip, "workspace_name": ep.workspace_name, "workspace_id": ep.workspace_id}
            for role, ep in assigned.items()
        },
        "paused": bool(old.get("paused", False)),
    }
    STATE.save(st)
    refresh_role_map_from_state(st)
