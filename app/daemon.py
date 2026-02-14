#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
QLabTrigger daemon (GPIO + LEDs + watchdog).

Comportement :
- Au boot du daemon : purge state (paired/endpoints) => PAIR volontaire requis (si STARTUP_FORCE_UNPAIR=True).
- PAIR appui court : pairing auto uniquement si jamais appairé.
- PAIR appui long 3s : relance un discovery + pairing complet.

LED (résumé) :
- Jamais appairé (avant tout PAIR) : bleu clignotant lent (toggle 1.0s) sur toutes les LED
- Discovery en cours : bleu clignotant rapide (toggle 0.25s) sur toutes les LED
- OK / prêt (paired + online) : vert fixe (par unité)
- Offline après pairing : rouge clignotant (toggle 0.5s) sur l’unité concernée
- Pairing échoué (aucun QLab répondant) : rouge fixe (toutes) jusqu’à nouveau PAIR
- Pairing incomplet (backup/aux manquant) : rouge fixe sur la/les LED manquantes pendant 10s puis OFF
- ACK (GO/PAUSE/PANIC/NEXT/PREV) : flash bleu sur l’unité qui a ack
- Conflit bloquant (noms ambigus / doublons) : violet fixe (toutes) jusqu’à nouveau PAIR
"""

import argparse
import os
import sys
import threading
import time
from typing import Any, Dict

from app import core as qc
from app import discover as qd
from config.loader import load_user_config

cfg = load_user_config()

# =========================
# GPIO (optionnel)
# =========================
GPIO_ENABLED = True
try:
    from gpiozero import Button
except Exception:
    GPIO_ENABLED = False

try:
    from rpi_ws281x import PixelStrip, Color, ws
    WS2812_AVAILABLE = True
except Exception:
    WS2812_AVAILABLE = False

WS2812_ENABLED = getattr(cfg, "WS2812_ENABLED", True)

# =========================
# LED CONFIG
# =========================
MASTER_DIM = getattr(cfg, "MASTER_DIM", 0.18)  # 25% par défaut (à ajuster)

def dim(c: tuple[int, int, int]) -> tuple[int, int, int]:
    f = max(0.0, min(1.0, MASTER_DIM))
    return (int(c[0]*f), int(c[1]*f), int(c[2]*f))

C_OFF     = (0, 0, 0)
C_BLUE    = (0, 0, 255)
C_GREEN   = (0, 255, 0)
C_RED     = (255, 0, 0)
C_VIOLET  = (255, 0, 255)
C_ORANGE  = (255, 80, 0)   # utile pour “pair incomplet” par ex

# =========================
# CONFIG DAEMON
# =========================
STARTUP_FORCE_UNPAIR = getattr(cfg, "STARTUP_FORCE_UNPAIR", True)
PAIR_HOLD_RESTART_SEC = getattr(cfg, "PAIR_HOLD_RESTART_SEC", 3.0)

DISCOVERY_BCAST_IP = getattr(cfg, "DISCOVERY_BCAST_IP", "255.255.255.255")
DISCOVERY_WAIT_SEC = getattr(cfg, "DISCOVERY_WAIT_SEC", 1.2)

LED_TICK = 0.05

RECONCILE_EVERY = getattr(cfg, "RECONCILE_EVERY", 5.0)

OFFLINE_BACKOFF_MIN = 2.0
OFFLINE_BACKOFF_MAX = 20.0
OFFLINE_BACKOFF_FACTOR = 2.0

BLINK_TOGGLE_SLOW = 1.0
BLINK_TOGGLE_FAST = 0.25
BLINK_TOGGLE_NORM = 0.50

MISSING_RED_SEC = 10.0
ACK_FLASH_SEC = 0.25
ACK_RETURN_FADE_SEC = 0.25

# =========================
# GPIO PINS (BCM)
# =========================
PIN_LED_DATA = getattr(cfg, "PIN_LED_DATA", 18)
LED_COUNT = getattr(cfg, "LED_COUNT", 3)
LED_BRIGHTNESS = getattr(cfg, "LED_BRIGHTNESS", 255)

PIN_BTN_GO = getattr(cfg, "PIN_BTN_GO", 5)
PIN_BTN_PAUSE = getattr(cfg, "PIN_BTN_PAUSE", 6)
PIN_BTN_PANIC = getattr(cfg, "PIN_BTN_PANIC", 12)

ENC_CLK = getattr(cfg, "ENC_CLK", 17)
ENC_DT  = getattr(cfg, "ENC_DT", 27)
ENC_SW  = getattr(cfg, "ENC_SW", 22)

BTN_BOUNCE = getattr(cfg, "BTN_BOUNCE", 0.08)
BTN_HOLD_IGNORE = getattr(cfg, "BTN_HOLD_IGNORE", 0.25)
ENCODER_EVENT_COOLDOWN = getattr(cfg, "ENCODER_EVENT_COOLDOWN", 0.12)
ENCODER_DIR_GLITCH_SEC = getattr(cfg, "ENCODER_DIR_GLITCH_SEC", 0.03)

BACKUP_OPTIONAL = getattr(cfg, "BACKUP_OPTIONAL", False)
AUX_OPTIONAL = getattr(cfg, "AUX_OPTIONAL", True)

# =========================
# GLOBAL FLAGS / RESTART
# =========================
RESTART_EVENT = threading.Event()

_DISCOVERY_LOCK = threading.Lock()
DISCOVERY_ACTIVE = False

_PAIR_FATAL_FAIL = False
_PAIR_CONFLICT = False

_MISSING_UNTIL: Dict[str, float] = {"backup": 0.0, "aux": 0.0}
_HEAL_MISMATCH_UNTIL: Dict[str, float] = {"main": 0.0, "backup": 0.0, "aux": 0.0}

# =========================
# RESTART / UNPAIR
# =========================
def restart_self() -> None:
    qc.LOGGER.info("SYS restart requested -> execv")
    launch_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "launch.py")
    os.execv(sys.executable, [sys.executable, launch_file, "daemon"])


def force_unpair_state() -> None:
    global _PAIR_FATAL_FAIL, _PAIR_CONFLICT
    st = qc.STATE.load()
    st.pop("paired", None)
    st.pop("endpoints", None)
    st.pop("paused", None)
    qc.STATE.save(st)
    qc.refresh_role_map_from_state(st)

    _PAIR_FATAL_FAIL = False
    _PAIR_CONFLICT = False
    _MISSING_UNTIL["backup"] = 0.0
    _MISSING_UNTIL["aux"] = 0.0

    qc.LOGGER.info("CFG UNPAIRED (state purged)")
# =========================
# LED DRIVER
# =========================
class Ws2812Controller:
    def __init__(self, led_count: int, pin_data: int) -> None:
        self.enabled = WS2812_AVAILABLE
        self._lock = threading.Lock()
        self.led_count = led_count
        self._pixels: list[tuple[int, int, int]] = [(0, 0, 0)] * led_count
        self.strip = None

        if not self.enabled or not WS2812_ENABLED:
            self.enabled = False
            return

        # rpi_ws281x a besoin d'un accès bas niveau (souvent /dev/mem).
        # Si le service tourne sans droits suffisants, on bascule en mode LED désactivé
        # plutôt que de faire planter tout le daemon.
        if not os.access("/dev/mem", os.R_OK | os.W_OK):
            self.enabled = False
            qc.LOGGER.warning("LED WS2812 disabled: no read/write access to /dev/mem.")
            return

        try:
            self.strip = PixelStrip(
                led_count,
                pin_data,
                800000,
                10,
                False,
                LED_BRIGHTNESS,
                0,
                strip_type=ws.WS2811_STRIP_RGB,
            )
            self.strip.begin()
            self.show()
        except Exception as e:
            self.enabled = False
            self.strip = None
            qc.LOGGER.warning("LED WS2812 init failed, continuing without LEDs: %s", e)

    def set_pixel(self, idx: int, rr: int, gg: int, bb: int) -> None:
        if not (0 <= idx < self.led_count):
            return
        with self._lock:
            self._pixels[idx] = (rr, gg, bb)

    def show(self) -> None:
        if not self.enabled or self.strip is None:
            return
        with self._lock:
            for idx, (rr, gg, bb) in enumerate(self._pixels):
                self.strip.setPixelColor(idx, Color(rr, gg, bb))
            self.strip.show()


class RGB:
    def __init__(self, controller: Ws2812Controller, pixel_index: int) -> None:
        self.controller = controller
        self.pixel_index = pixel_index
        self.enabled = self.controller.enabled
        self.off()

    def apply(self, rr: int, gg: int, bb: int) -> None:
        if not self.enabled:
            return
        self.controller.set_pixel(self.pixel_index, rr, gg, bb)

    def off(self) -> None:
        self.apply(*dim(C_OFF))


WS2812 = Ws2812Controller(LED_COUNT, PIN_LED_DATA)

LED_BY_ROLE = {
    "main": RGB(WS2812, 0),
    "backup": RGB(WS2812, 1),
    "aux": RGB(WS2812, 2),
}


class LEDWorker(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._lock = threading.Lock()
        self._steady: Dict[str, tuple[int, int, int]] = {
            r: dim(C_OFF) for r in LED_BY_ROLE.keys()
        }
        self._blink: Dict[str, bool] = {r: False for r in LED_BY_ROLE.keys()}
        self._toggle: Dict[str, float] = {r: BLINK_TOGGLE_NORM for r in LED_BY_ROLE.keys()}
        self._flash_until: Dict[str, float] = {r: 0.0 for r in LED_BY_ROLE.keys()}
        self._fade_start: Dict[str, float] = {r: 0.0 for r in LED_BY_ROLE.keys()}
        self._fade_from: Dict[str, tuple[int, int, int]] = {
            r: dim(C_OFF) for r in LED_BY_ROLE.keys()
        }
        self._last_rendered: Dict[str, tuple[int, int, int]] = {
            r: dim(C_OFF) for r in LED_BY_ROLE.keys()
        }
        self._flash_active: Dict[str, bool] = {r: False for r in LED_BY_ROLE.keys()}
        self._stop = False

    def set_state(self, role: str, steady_rgb: tuple[int, int, int],
                  blink: bool, toggle_sec: float = BLINK_TOGGLE_NORM) -> None:
        with self._lock:
            self._steady[role] = steady_rgb
            self._blink[role] = blink
            self._toggle[role] = max(0.05, float(toggle_sec))

    def flash_ack(self, role: str, duration: float = ACK_FLASH_SEC) -> None:
        with self._lock:
            self._flash_until[role] = qc.mono() + duration

    def _blink_on(self, now: float, toggle_sec: float) -> bool:
        return (int(now / toggle_sec) % 2) == 0

    @staticmethod
    def _lerp_rgb(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
        t = max(0.0, min(1.0, t))
        return (
            int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t),
        )

    def run(self) -> None:
        while not self._stop:
            now = qc.mono()

            with self._lock:
                for role, led in LED_BY_ROLE.items():
                    flash_active = now < self._flash_until.get(role, 0.0)
                    if flash_active:
                        self._flash_active[role] = True
                        color = dim(C_BLUE)
                        self._last_rendered[role] = color
                        led.apply(*color)
                        continue

                    if self._flash_active.get(role, False):
                        # Fin de flash ACK : fondu doux vers l'état nominal
                        self._flash_active[role] = False
                        self._fade_start[role] = now
                        self._fade_from[role] = self._last_rendered.get(role, dim(C_BLUE))

                    steady = self._steady.get(role, dim(C_OFF))
                    do_blink = self._blink.get(role, False)
                    toggle = self._toggle.get(role, BLINK_TOGGLE_NORM)
                    blink_on = self._blink_on(now, toggle)

                    if do_blink and not blink_on:
                        target = dim(C_OFF)
                    else:
                        target = steady

                    fade_t0 = self._fade_start.get(role, 0.0)
                    if fade_t0 > 0:
                        dt = now - fade_t0
                        if dt < ACK_RETURN_FADE_SEC:
                            start = self._fade_from.get(role, dim(C_BLUE))
                            color = self._lerp_rgb(start, target, dt / ACK_RETURN_FADE_SEC)
                        else:
                            self._fade_start[role] = 0.0
                            color = target
                    else:
                        color = target

                    self._last_rendered[role] = color
                    led.apply(*color)

                WS2812.show()

            time.sleep(0.02)


LEDW = LEDWorker()
LEDW.start()
def on_ack(ip: str, role: str) -> None:
    LEDW.flash_ack(role, ACK_FLASH_SEC)

qc.set_ack_callback(on_ack)


# =========================
# LED STATES
# =========================
def set_led_never_paired() -> None:
    for r in LED_BY_ROLE.keys():
        LEDW.set_state(r, dim(C_BLUE), blink=True, toggle_sec=BLINK_TOGGLE_SLOW)


def set_led_discovery() -> None:
    for r in LED_BY_ROLE.keys():
        LEDW.set_state(r, dim(C_BLUE), blink=True, toggle_sec=BLINK_TOGGLE_FAST)


def set_led_fatal_fail() -> None:
    for r in LED_BY_ROLE.keys():
        LEDW.set_state(r, dim(C_RED), blink=False)


def set_led_conflict() -> None:
    for r in LED_BY_ROLE.keys():
        LEDW.set_state(r, dim(C_VIOLET), blink=False)


def set_led_role_state(role: str, present: bool, online: bool) -> None:
    now = qc.mono()

    mismatch_until = _HEAL_MISMATCH_UNTIL.get(role, 0.0)
    if mismatch_until and now < mismatch_until:
        LEDW.set_state(role, dim(C_RED), blink=False)
        return

    if not present:
        until = _MISSING_UNTIL.get(role, 0.0)
        if until and now < until:
            LEDW.set_state(role, dim(C_RED), blink=False)
            return

        LEDW.set_state(role, dim(C_OFF), blink=False)
        return

    if online:
        LEDW.set_state(role, dim(C_GREEN), blink=False)
    else:
        LEDW.set_state(role, dim(C_RED), blink=True,
                       toggle_sec=BLINK_TOGGLE_NORM)


# =========================
# ACTIONS
# =========================
def warmup_before_action(eps: Dict[str, qc.Endpoint]) -> None:
    for role in ("main", "backup", "aux"):
        ep = eps.get(role)
        if not ep or not ep.workspace_id:
            continue
        qc.ensure_app_flags(ep.ip, force=True)
        qc.ensure_connected(ep, force=True)


def send_action(eps: Dict[str, qc.Endpoint], suffix: str) -> None:
    for role in ("main", "backup", "aux"):
        ep = eps.get(role)
        if ep and ep.workspace_id:
            qc.send_ws_fast(ep.ip, ep.workspace_id, suffix)


def pause_toggle(eps: Dict[str, qc.Endpoint]) -> None:
    st = qc.STATE.load()
    paused = bool(st.get("paused", False))
    if paused:
        send_action(eps, "resume")
        st["paused"] = False
        qc.LOGGER.info("PAUSE_TOGGLE -> RESUME")
    else:
        send_action(eps, "pause")
        st["paused"] = True
        qc.LOGGER.info("PAUSE_TOGGLE -> PAUSE")
    qc.STATE.save(st)


def heal_reconcile_strict() -> None:
    """
    Soft repair non destructif:
    - ne change jamais workspace_name
    - met à jour workspace_id seulement si workspace_name attendu est présent
    - force flags/connect
    """
    try:
        eps = qc.load_paired_endpoints()
    except Exception:
        qc.LOGGER.info("HEAL ignored: not paired")
        return

    st = qc.STATE.load()

    for role in ("main", "backup", "aux"):
        ep = eps.get(role)
        if not ep or not ep.workspace_id or not ep.workspace_name:
            continue
        try:
            qc.ensure_app_flags(ep.ip, force=True)

            r = qc.request_workspaces(ep.ip, timeout=0.9)
            wsmap = qc.parse_workspaces(r) if r else {}
            if ep.workspace_name not in wsmap:
                LEDW.set_state(role, dim(C_RED), blink=False)
                _HEAL_MISMATCH_UNTIL[role] = qc.mono() + 3.0
                qc.LOGGER.warning(
                    "HEAL mismatch %s ip=%s expected ws='%s' not open",
                    role.upper(), ep.ip, ep.workspace_name,
                )
                continue

            new_id = wsmap.get(ep.workspace_name)
            if new_id and new_id != ep.workspace_id:
                ep.workspace_id = new_id
                st.setdefault("endpoints", {}).setdefault(role, {})
                st["endpoints"][role]["workspace_id"] = new_id
                st["endpoints"][role]["workspace_name"] = ep.workspace_name
                qc.STATE.save(st)
                qc.refresh_role_map_from_state(st)
                qc.LOGGER.warning(
                    "HEAL wsid update %s '%s' -> %s",
                    role.upper(), ep.workspace_name, new_id,
                )

            qc.ensure_connected(ep, force=True)
            _HEAL_MISMATCH_UNTIL[role] = 0.0
            LEDW.flash_ack(role, 0.15)
        except Exception as e:
            qc.LOGGER.debug("HEAL %s failed: %s", role, e)

# =========================
# DISCOVERY / PAIR WRAPPER
# =========================
def _mark_incomplete_roles(assigned: Dict[str, qc.Endpoint]) -> None:
    now = qc.mono()
    _MISSING_UNTIL["backup"] = (now + MISSING_RED_SEC) if ("backup" not in assigned) else 0.0
    _MISSING_UNTIL["aux"] = (now + MISSING_RED_SEC) if ("aux" not in assigned) else 0.0


def run_pairing_auto() -> None:
    global DISCOVERY_ACTIVE, _PAIR_FATAL_FAIL, _PAIR_CONFLICT

    with _DISCOVERY_LOCK:
        if DISCOVERY_ACTIVE:
            return
        DISCOVERY_ACTIVE = True

    _PAIR_FATAL_FAIL = False
    _PAIR_CONFLICT = False

    try:
        qc.LOGGER.info("PAIR: discovery starting (broadcast)")
        set_led_discovery()

        assigned = qd.pair_auto(bcast_ip=DISCOVERY_BCAST_IP, wait_sec=DISCOVERY_WAIT_SEC)
        _mark_incomplete_roles(assigned)

        qc.LOGGER.info("PAIR: done roles=%s", sorted(list(assigned.keys())))

    except qd.ConflictError as e:
        _PAIR_CONFLICT = True
        qc.LOGGER.warning("PAIR: CONFLICT: %s", e)

    except qd.NoRespondersError as e:
        _PAIR_FATAL_FAIL = True
        qc.LOGGER.warning("PAIR: FAILED (no responders): %s", e)

    except Exception as e:
        _PAIR_FATAL_FAIL = True
        qc.LOGGER.warning("PAIR: FAILED: %s", e)

    finally:
        with _DISCOVERY_LOCK:
            DISCOVERY_ACTIVE = False

# =========================
# WATCHDOG + RECONCILE
# =========================
_last_thump_sent: Dict[str, float] = {}
_last_reconcile: Dict[str, float] = {}
_offline_backoff: Dict[str, float] = {}
_offline_next_try: Dict[str, float] = {}


def thump_fire(ep: qc.Endpoint) -> None:
    if not ep.workspace_id:
        return

    qc.ensure_app_flags(ep.ip, force=False)
    qc.ensure_connected(ep, force=False)

    now = qc.mono()
    k = f"{ep.ip}:{ep.workspace_id}"
    if (now - _last_thump_sent.get(k, 0.0)) < qc.HEARTBEAT_INTERVAL:
        return
    _last_thump_sent[k] = now

    qc.send_ws(ep.ip, ep.workspace_id, "thump")


def offline_gate(role: str) -> bool:
    return qc.mono() >= _offline_next_try.get(role, 0.0)


def bump_offline_backoff(role: str) -> None:
    cur = _offline_backoff.get(role, 0.0)
    cur = OFFLINE_BACKOFF_MIN if cur <= 0 else min(OFFLINE_BACKOFF_MAX, cur * OFFLINE_BACKOFF_FACTOR)
    _offline_backoff[role] = cur
    _offline_next_try[role] = qc.mono() + cur


def reset_offline_backoff(role: str) -> None:
    _offline_backoff[role] = 0.0
    _offline_next_try[role] = 0.0


def reconcile_endpoint(role: str, ep: qc.Endpoint) -> bool:
    if not offline_gate(role):
        return False

    now = qc.mono()
    if (now - _last_reconcile.get(role, 0.0)) < RECONCILE_EVERY:
        return False
    _last_reconcile[role] = now

    qc.ensure_app_flags(ep.ip, force=True)

    r = qc.request_workspaces(ep.ip, timeout=0.9)
    if not r:
        bump_offline_backoff(role)
        return False

    wsmap = qc.parse_workspaces(r)
    if not wsmap:
        bump_offline_backoff(role)
        return False

    desired = ep.workspace_name
    if not desired or desired not in wsmap:
        bump_offline_backoff(role)
        return False

    new_id = wsmap.get(desired)
    if not new_id:
        bump_offline_backoff(role)
        return False

    changed = False
    if new_id != ep.workspace_id:
        old_id = ep.workspace_id
        ep.workspace_id = new_id
        qc.LOGGER.warning("WSID changed for %s '%s': %s -> %s", role.upper(), desired, old_id, new_id)
        changed = True

        st = qc.STATE.load()
        st.setdefault("endpoints", {}).setdefault(role, {})
        st["endpoints"][role]["workspace_id"] = new_id
        st["endpoints"][role]["workspace_name"] = desired
        qc.STATE.save(st)
        qc.refresh_role_map_from_state(st)

    qc.ensure_connected(ep, force=True)
    qc.ensure_app_flags(ep.ip, force=True)

    reset_offline_backoff(role)
    return changed

# =========================
# GPIO BUTTONS
# =========================
_last_fire: Dict[str, float] = {}


def edge_guard(key: str, min_dt: float) -> bool:
    now = qc.mono()
    last = _last_fire.get(key, 0.0)
    if (now - last) < min_dt:
        return False
    _last_fire[key] = now
    return True


GPIO_BUTTONS: Dict[str, Any] = {}

_pair_pressed_mono = 0.0
_pair_held_fired = False

class RotaryEncoder:
    def __init__(self, pin_clk: int, pin_dt: int, callback_cw, callback_ccw):
        import lgpio
        self._lg = lgpio
        self.chip = lgpio.gpiochip_open(0)

        self.pin_clk = pin_clk
        self.pin_dt = pin_dt

        lgpio.gpio_claim_input(self.chip, pin_clk)
        lgpio.gpio_claim_input(self.chip, pin_dt)

        self.callback_cw = callback_cw
        self.callback_ccw = callback_ccw
        self.event_cooldown = ENCODER_EVENT_COOLDOWN
        self.dir_glitch_sec = ENCODER_DIR_GLITCH_SEC

        self.last_clk = lgpio.gpio_read(self.chip, pin_clk)
        self._last_emit_mono = 0.0
        self._last_dir = 0  # +1=cw, -1=ccw
        self._running = True

        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self._running:
            clk = self._lg.gpio_read(self.chip, self.pin_clk)

            if clk != self.last_clk:
                dt = self._lg.gpio_read(self.chip, self.pin_dt)
                direction = 1 if dt != clk else -1
                now = qc.mono()

                # Filtrage anti-rebond / anti-glitch:
                # - limite à 1 événement toutes les `event_cooldown` secondes
                # - ignore un changement de direction quasi instantané (glitch)
                too_soon = (now - self._last_emit_mono) < self.event_cooldown
                glitch_reverse = (
                    self._last_dir != 0
                    and direction != self._last_dir
                    and (now - self._last_emit_mono) < self.dir_glitch_sec
                )

                if not too_soon and not glitch_reverse:
                    self._last_emit_mono = now
                    self._last_dir = direction

                    if direction > 0:
                        self.callback_cw()
                    else:
                        self.callback_ccw()

            self.last_clk = clk
            time.sleep(0.001)

def button_setup() -> None:
    global _pair_pressed_mono, _pair_held_fired

    if not GPIO_ENABLED:
        qc.LOGGER.info("GPIO disabled (gpiozero not available).")
        return

    GPIO_BUTTONS["go"] = Button(PIN_BTN_GO, pull_up=True, bounce_time=BTN_BOUNCE)
    GPIO_BUTTONS["pause"] = Button(PIN_BTN_PAUSE, pull_up=True, bounce_time=BTN_BOUNCE)
    GPIO_BUTTONS["panic"] = Button(PIN_BTN_PANIC, pull_up=True, bounce_time=BTN_BOUNCE)

    def do_go() -> None:
        qc.LOGGER.info("BTN GO pressed")
        if not edge_guard("go", BTN_HOLD_IGNORE):
            return
        try:
            eps = qc.load_paired_endpoints()
            warmup_before_action(eps)
            send_action(eps, "go")
        except Exception as e:
            qc.LOGGER.debug("BTN GO ignored: %s", e)

    def do_pause() -> None:
        qc.LOGGER.info("BTN PAUSE pressed")
        if not edge_guard("pause", BTN_HOLD_IGNORE):
            return
        try:
            eps = qc.load_paired_endpoints()
            warmup_before_action(eps)
            pause_toggle(eps)
        except Exception as e:
            qc.LOGGER.debug("BTN PAUSE ignored: %s", e)

    def do_panic() -> None:
        qc.LOGGER.info("BTN PANIC pressed")
        if not edge_guard("panic", BTN_HOLD_IGNORE):
            return
        try:
            eps = qc.load_paired_endpoints()
            warmup_before_action(eps)
            send_action(eps, "panic")
        except Exception as e:
            qc.LOGGER.debug("BTN PANIC ignored: %s", e)

    def do_up() -> None:
        qc.LOGGER.info("BTN UP pressed")
        if not edge_guard("up", BTN_HOLD_IGNORE):
            return
        try:
            eps = qc.load_paired_endpoints()
            warmup_before_action(eps)
            send_action(eps, "select/previous")
        except Exception as e:
            qc.LOGGER.debug("BTN UP ignored: %s", e)

    def do_down() -> None:
        qc.LOGGER.info("BTN DOWN pressed")
        if not edge_guard("down", BTN_HOLD_IGNORE):
            return
        try:
            eps = qc.load_paired_endpoints()
            warmup_before_action(eps)
            send_action(eps, "select/next")
        except Exception as e:
            qc.LOGGER.debug("BTN DOWN ignored: %s", e)

    def do_pair_pressed() -> None:
        global _pair_pressed_mono, _pair_held_fired
        qc.LOGGER.info("BTN PAIR pressed")
        _pair_pressed_mono = qc.mono()
        _pair_held_fired = False

    def do_pair_held() -> None:
        global _pair_held_fired
        _pair_held_fired = True
        qc.LOGGER.info("BTN PAIR held %.1fs -> FORCE NEW DISCOVERY", PAIR_HOLD_RESTART_SEC)
        try:
            force_unpair_state()
        except Exception:
            pass
        threading.Thread(target=run_pairing_auto, daemon=True).start()

    def do_pair_released() -> None:
        global _pair_pressed_mono, _pair_held_fired
        dt = qc.mono() - _pair_pressed_mono
        qc.LOGGER.info("BTN PAIR released (%.2fs)", dt)

        if _pair_held_fired:
            return

        st = qc.STATE.load()
        if st.get("paired"):
            if not edge_guard("pair_short_heal", 0.5):
                return
            qc.LOGGER.info("BTN PAIR short -> HEAL/RECONCILE STRICT")
            threading.Thread(target=heal_reconcile_strict, daemon=True).start()
            return

        if not edge_guard("pair_short", 0.5):
            return

        threading.Thread(target=run_pairing_auto, daemon=True).start()

    GPIO_BUTTONS["go"].when_pressed = do_go
    GPIO_BUTTONS["pause"].when_pressed = do_pause
    GPIO_BUTTONS["panic"].when_pressed = do_panic

    # --- Encoder rotation ---
    def enc_cw():
        qc.LOGGER.info("ENCODER CW")
        do_down()

    def enc_ccw():
        qc.LOGGER.info("ENCODER CCW")
        do_up()

    RotaryEncoder(ENC_CLK, ENC_DT, enc_cw, enc_ccw)

    # --- Encoder push = PAIR ---
    GPIO_BUTTONS["pair"] = Button(
        ENC_SW,
        pull_up=True,
        bounce_time=BTN_BOUNCE,
        hold_time=PAIR_HOLD_RESTART_SEC,
        hold_repeat=False
    )

    GPIO_BUTTONS["pair"].when_pressed = do_pair_pressed
    GPIO_BUTTONS["pair"].when_held = do_pair_held
    GPIO_BUTTONS["pair"].when_released = do_pair_released


    qc.LOGGER.warning(
        "GPIO ready (GO=%d PAUSE=%d PANIC=%d ENC_CLK=%d ENC_DT=%d ENC_SW=%d hold=%.1fs)",
        PIN_BTN_GO, PIN_BTN_PAUSE, PIN_BTN_PANIC,
        ENC_CLK, ENC_DT, ENC_SW,
        PAIR_HOLD_RESTART_SEC
    )


# =========================
# DAEMON LOOP
# =========================
def _inject_last_seen(eps: Dict[str, qc.Endpoint]) -> None:
    try:
        lock = getattr(qc, "_last_seen_lock", None)
        if lock:
            with lock:
                for ep in eps.values():
                    ts = qc.LAST_SEEN_BY_IP.get(ep.ip)
                    if ts:
                        ep.last_seen_mono = max(ep.last_seen_mono, ts)
        else:
            for ep in eps.values():
                ts = qc.LAST_SEEN_BY_IP.get(ep.ip)
                if ts:
                    ep.last_seen_mono = max(ep.last_seen_mono, ts)
    except Exception:
        pass


def run_daemon() -> None:
    qc.start_osc_server()
    button_setup()

    if STARTUP_FORCE_UNPAIR:
        force_unpair_state()

    st0 = qc.STATE.load()
    qc.refresh_role_map_from_state(st0)

    qc.LOGGER.info("SYS daemon running (%s).", "GPIO OK" if GPIO_ENABLED else "NO GPIO backend")

    last_net_log = 0.0
    last_status_line = ""
    last_status_mono = 0.0
    HEARTBEAT_LOG_SEC = 60.0  # 0 pour désactiver le heartbeat périodique


    while True:
        if RESTART_EVENT.is_set():
            restart_self()
            return

        st = qc.STATE.load()
        qc.refresh_role_map_from_state(st)

        with _DISCOVERY_LOCK:
            discovery = DISCOVERY_ACTIVE

        if _PAIR_CONFLICT:
            set_led_conflict()
        elif _PAIR_FATAL_FAIL:
            set_led_fatal_fail()
        elif discovery:
            set_led_discovery()
        elif not st.get("paired"):
            set_led_never_paired()
        else:
            try:
                eps = qc.load_paired_endpoints()
            except Exception:
                set_led_fatal_fail()
                time.sleep(LED_TICK)
                continue

            _inject_last_seen(eps)

            for role in ("main", "backup", "aux"):
                ep = eps.get(role)
                if ep:
                    thump_fire(ep)

            for role in ("main", "backup", "aux"):
                ep = eps.get(role)
                if not ep:
                    continue
                if ep.online:
                    reset_offline_backoff(role)
                else:
                    reconcile_endpoint(role, ep)

            set_led_role_state("main", "main" in eps, eps["main"].online if "main" in eps else False)
            set_led_role_state("backup", "backup" in eps, eps["backup"].online if "backup" in eps else False)
            set_led_role_state("aux", "aux" in eps, eps["aux"].online if "aux" in eps else False)

            main_present = "main" in eps
            bkp_present = "backup" in eps
            aux_present = "aux" in eps
            main_online = eps["main"].online if main_present else False
            bkp_online = eps["backup"].online if bkp_present else False
            aux_online = eps["aux"].online if aux_present else False

            # log seulement si changement (et optionnellement heartbeat)
            now = qc.mono()
            parts = []
            if main_present:
                parts.append(f"MAIN {'ONLINE' if main_online else 'OFFLINE'} ip={eps['main'].ip} ws='{eps['main'].workspace_name}'")
            else:
                parts.append("MAIN NOT_PAIRED")

            if bkp_present:
                parts.append(f"BACKUP {'ONLINE' if bkp_online else 'OFFLINE'} ip={eps['backup'].ip} ws='{eps['backup'].workspace_name}'")
            else:
                parts.append("BACKUP NOT_PAIRED")

            if aux_present:
                parts.append(f"AUX {'ONLINE' if aux_online else 'OFFLINE'} ip={eps['aux'].ip} ws='{eps['aux'].workspace_name}'")
            else:
                parts.append("AUX NOT_PAIRED")

            status_line = "NET " + " | ".join(parts)

            changed = (status_line != last_status_line)
            heartbeat_due = (HEARTBEAT_LOG_SEC > 0 and (now - last_status_mono) >= HEARTBEAT_LOG_SEC)

            if changed or heartbeat_due:
                # WARNING uniquement si problème, INFO si tout est OK
                has_problem = (not main_present) or (main_present and not main_online) or (bkp_present and not bkp_online) or (aux_present and not aux_online)
                if has_problem:
                    qc.LOGGER.info(status_line)
                else:
                    qc.LOGGER.info(status_line)

                last_status_line = status_line
                last_status_mono = now

        time.sleep(LED_TICK)

# =========================
# CLI
# =========================
def main() -> None:
    p = argparse.ArgumentParser(description="QLabTrigger daemon (GPIO + LEDs).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="Run daemon (GPIO + LEDs).")
    sub.add_parser("unpair", help="Clear pairing state (manual).")
    sub.add_parser("pair", help="Run pairing once (for debug).")

    args = p.parse_args()

    if args.cmd == "unpair":
        force_unpair_state()
        return

    if args.cmd == "pair":
        run_pairing_auto()
        return

    if args.cmd == "daemon":
        run_daemon()
        return


if __name__ == "__main__":
    main()
