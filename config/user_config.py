"""Configuration utilisateur pour QLab Box.

Modifiez ce fichier pour adapter ports, chemins, GPIO et comportements.
"""

# OSC / réseau
QLAB_PORT = 53000
PI_LISTEN_IP = "0.0.0.0"
PI_REPLY_PORT = 53001
OSC_PASSCODE = "7777"

# Nommage workspaces
EXPECTED_WS_MAIN = "show_main"
EXPECTED_WS_BACKUP = "show_backup"
SUFFIX_MAIN = "_main"
SUFFIX_BACKUP = "_backup"
SUFFIX_AUX1 = "_aux1"

# Persistance / logs
LOG_DIR = "/var/log/qlab-box"
STATE_DIR = "/var/lib/qlab-box"

# Daemon
STARTUP_FORCE_UNPAIR = True
PAIR_HOLD_RESTART_SEC = 3.0
DISCOVERY_BCAST_IP = "255.255.255.255"
DISCOVERY_WAIT_SEC = 1.2
RECONCILE_EVERY = 5.0
BACKUP_OPTIONAL = False
AUX_OPTIONAL = True

# GPIO / LEDs
WS2812_ENABLED = True
MASTER_DIM = 0.18
PIN_LED_DATA = 18
LED_COUNT = 3
LED_BRIGHTNESS = 255

PIN_BTN_GO = 5
PIN_BTN_PAUSE = 6
PIN_BTN_PANIC = 12
ENC_CLK = 17
ENC_DT = 27
ENC_SW = 22

BTN_BOUNCE = 0.08
BTN_HOLD_IGNORE = 0.25
ENCODER_EVENT_COOLDOWN = 0.12
ENCODER_DIR_GLITCH_SEC = 0.03
