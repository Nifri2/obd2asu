"""
Minimal MCP2515 SPI driver for MicroPython.
Implements only what the sniffer needs: reset, listen-only init, receive.

In listen-only mode (CANCTRL REQOP = 0b011) the MCP2515 never drives the
bus — no ACK bits, no error frames. Fully passive.
"""

from machine import SPI, Pin
import time

# SPI command bytes
_RESET   = const(0xC0)
_READ    = const(0x03)
_WRITE   = const(0x02)
_BITMOD  = const(0x05)
_RX_ST   = const(0xB0)   # read RX status
_RD_RX0  = const(0x90)   # read RX buffer 0
_RD_RX1  = const(0x94)   # read RX buffer 1

# Registers
_CANCTRL   = const(0x0F)
_CANSTAT   = const(0x0E)
_CNF1      = const(0x2A)
_CNF2      = const(0x29)
_CNF3      = const(0x28)
_RXB0CTRL  = const(0x60)
_RXB1CTRL  = const(0x70)

# CANCTRL REQOP values (bits [7:5])
_MODE_CONFIG = const(0x80)
_MODE_LISTEN = const(0x60)


class MCP2515:
    def __init__(self, spi: SPI, cs: Pin):
        self._spi = spi
        self._cs  = cs
        self._cs(1)  # deselect

    def read_reg(self, reg: int) -> int:
        self._cs(0)
        self._spi.write(bytes([_READ, reg]))
        v = self._spi.read(1)[0]
        self._cs(1)
        return v

    def write_reg(self, reg: int, val: int) -> None:
        self._cs(0)
        self._spi.write(bytes([_WRITE, reg, val]))
        self._cs(1)

    def reset(self) -> None:
        self._cs(0)
        self._spi.write(bytes([_RESET]))
        self._cs(1)
        time.sleep_ms(10)

    def init_listen_only(self, cnf1: int, cnf2: int, cnf3: int) -> bool:
        """
        Configure for listen-only at the bitrate described by cnf1/cnf2/cnf3.
        Returns True if mode was confirmed, False if chip not responding.
        """
        self.reset()
        # Must be in config mode after reset
        if (self.read_reg(_CANSTAT) & 0xE0) != _MODE_CONFIG:
            return False

        self.write_reg(_CNF1, cnf1)
        self.write_reg(_CNF2, cnf2)
        self.write_reg(_CNF3, cnf3)

        # Accept all frames in both RX buffers; enable RXB0→RXB1 rollover
        self.write_reg(_RXB0CTRL, 0x64)   # RXM=11 (all), BUKT=1 (rollover)
        self.write_reg(_RXB1CTRL, 0x60)   # RXM=11 (all)

        # Enter listen-only — the controller will never transmit after this point
        self.write_reg(_CANCTRL, _MODE_LISTEN)
        time.sleep_ms(5)

        return (self.read_reg(_CANSTAT) & 0xE0) == _MODE_LISTEN

    def recv(self):
        """
        Poll for a received frame.
        Returns (can_id: int, dlc: int, data: bytes, ext: bool) or None.
        """
        self._cs(0)
        self._spi.write(bytes([_RX_ST]))
        status = self._spi.read(1)[0]
        self._cs(1)

        if not (status & 0xC0):
            return None   # no frame waiting (bits 6/7 = RXB0/RXB1 full)

        cmd = _RD_RX0 if (status & 0x40) else _RD_RX1  # bit 6=RXB0, bit 7=RXB1
        self._cs(0)
        self._spi.write(bytes([cmd]))
        buf = bytearray(13)   # SIDH SIDL EID8 EID0 DLC D0..D7(max)
        self._spi.readinto(buf)
        self._cs(1)

        sidh, sidl, eid8, eid0 = buf[0], buf[1], buf[2], buf[3]
        dlc  = buf[4] & 0x0F
        data = bytes(buf[5:5 + dlc])

        ext = bool(sidl & 0x08)
        if ext:
            can_id  = ((sidh << 3) | (sidl >> 5)) << 18
            can_id |= ((sidl & 0x03) << 16) | (eid8 << 8) | eid0
        else:
            can_id = (sidh << 3) | (sidl >> 5)

        return (can_id, dlc, data, ext)
