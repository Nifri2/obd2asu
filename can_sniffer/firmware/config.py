# MCP2515 sniffer configuration

# ── SPI wiring ────────────────────────────────────────────────────────────────
SCK_PIN  = 18
MOSI_PIN = 23   # SI on module
MISO_PIN = 19   # SO on module — 5V output; see level-shifter note in doc
CS_PIN   = 5
INT_PIN  = 4    # polled, not used as hardware interrupt

SPI_BAUDRATE = 1_000_000   # 1 MHz — conservative and reliable for sniffing

# ── MCP2515 bitrate ───────────────────────────────────────────────────────────
# CRITICAL: CAN_CRYSTAL must match the crystal soldered to your module.
# Wrong value = garbage frames or no frames at all.
# This module has an 8.000 MHz crystal.
CAN_CRYSTAL = 8_000_000
CAN_BITRATE = 500_000

# CNF register values for 8 MHz crystal, 500 kbps
# TQ = 2*(BRP+1)/Fosc = 2*1/8MHz = 250 ns   →   8 TQ / bit
# Segments: SyncSeg=1, PropSeg=2, PS1=3, PS2=2
CNF1 = 0x00   # BRP=0, SJW=1TQ
CNF2 = 0x91   # BTLMODE=1, PHSEG1=2 (3TQ), PRSEG=1 (2TQ)
CNF3 = 0x01   # PHSEG2=1 (2TQ)

# To try 250 kbps instead (if 500K yields no frames):
#   CNF1 = 0x01   BRP=1 → TQ=500ns, 8 TQ/bit at 250K
#   CNF2 = 0x91
#   CNF3 = 0x01

# ── Capture mode ─────────────────────────────────────────────────────────────
# A = raw dump     <millis> <hex_id> <dlc> <bytes>
# B = SLCAN ASCII  t<ID3><DLC><DATA>\r   (SavvyCAN / python-can)
# C = candump -tz  (<ts>) can0 <ID>#<DATA>  + # MARKER lines
DEFAULT_MODE = 'B'

# Marker button: GPIO0 is the BOOT button on most DevKitC boards
MARKER_BTN_PIN = 0
