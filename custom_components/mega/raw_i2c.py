"""Async software I2C bit-bang over MegaD GPIO pins.

Each SCL/SDA transition is a separate HTTP request, mirroring the
approach in scd41.py.  The hub's _http_lck is held for the entire
write-delay-read sequence so no other request can corrupt the bus
while a transaction is in progress.
"""
from __future__ import annotations

import asyncio
import logging
import typing

if typing.TYPE_CHECKING:
    from .hub import MegaD

_LOGGER = logging.getLogger(__name__)

_CRC8_POLY = 0x31
_CRC8_INIT = 0xFF

# I2C lock priority — lower number wins; 100 lets normal requests (-1..0) go first.
_I2C_PRIORITY = 100


def _crc8(data: list[int]) -> int:
    c = _CRC8_INIT
    for byte in data:
        c ^= byte
        for _ in range(8):
            c = ((c << 1) ^ _CRC8_POLY) if (c & 0x80) else (c << 1)
    return c & 0xFF


class SoftI2C:
    """Software I2C using MegaD GPIO via HTTP.

    All _r() calls go through hub._raw_request() which bypasses the
    priority lock.  The caller is responsible for holding hub._http_lck
    before entering any public method.
    """

    def __init__(self, hub: MegaD, sda: str, scl: str) -> None:
        self._hub = hub
        self._sda = sda
        self._scl = scl

    async def _r(self, **kw: typing.Any) -> str:
        return await self._hub._raw_request(**kw) or ""

    async def _dir(self, pin: str, out: bool) -> None:
        await self._r(pt=pin, dir=1 if out else 0)

    async def _init(self) -> None:
        await self._dir(self._scl, True)
        await self._dir(self._sda, True)
        await self._r(cmd=f"{self._scl}:1;{self._sda}:1")

    async def _start(self) -> None:
        await self._r(cmd=f"{self._sda}:0;{self._scl}:0")

    async def _stop(self) -> None:
        await self._r(cmd=f"{self._sda}:0;{self._scl}:1;{self._sda}:1")

    async def _send_byte(self, byte: int) -> str:
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            await self._r(cmd=f"{self._sda}:{bit};{self._scl}:1;{self._scl}:0;{self._sda}:0")
        # ACK phase: release SDA (input), pulse SCL, restore SDA (output)
        await self._r(pt=self._sda, dir=0, cmd=f"{self._scl}:1;{self._scl}:0")
        return await self._r(pt=self._sda, cmd="get", dir=1)

    async def _read_byte(self, nack: bool = False) -> int:
        # Release SDA to input, pull SCL low before clocking
        await self._r(pt=self._sda, dir=0, cmd=f"{self._scl}:0")
        bits = 0
        for _ in range(8):
            await self._r(cmd=f"{self._scl}:1")
            val = await self._r(pt=self._sda, cmd="get")
            # ON = transistor conducting = line LOW = I2C logical 0
            bits = (bits << 1) | (0 if val == "ON" else 1)
            await self._r(cmd=f"{self._scl}:0")
        # Drive ACK (SDA low) or NACK (SDA high), then release
        await self._dir(self._sda, True)
        await self._r(cmd=f"{self._scl}:1")
        if nack:
            await self._r(cmd=f"{self._sda}:1")
        await self._r(pt=self._sda, dir=0, cmd=f"{self._scl}:0")
        return bits

    async def write(self, address: int, data: list[int]) -> None:
        await self._init()
        await self._start()
        await self._send_byte(address << 1)
        for b in data:
            await self._send_byte(b)
        await self._stop()

    async def read(self, address: int, n: int) -> list[int]:
        await self._init()
        await self._start()
        await self._send_byte((address << 1) | 1)
        result = [await self._read_byte(nack=(i == n - 1)) for i in range(n)]
        await self._stop()
        return result


# ── SCD41 ────────────────────────────────────────────────────────────────────

_SCD41_MEASURE_CMD = [0xEC, 0x05]   # measure single-shot
_SCD41_MEASURE_DELAY = 5.5          # seconds; SCD41 datasheet max 5000 ms
_SCD41_DEFAULT_ADDR = 0x62

# Sub-keys written into hub.values[(sda, scl, addr, key)]
SCD41_SUBKEYS: dict[str, tuple[str, str]] = {
    "co2":  ("CO2",         "ppm"),
    "temp": ("Temperature", "°C"),
    "rh":   ("Humidity",    "%"),
}


async def read_scd41(
    hub: MegaD,
    sda: str,
    scl: str,
    address: int = _SCD41_DEFAULT_ADDR,
) -> dict[str, float | int]:
    """Trigger one SCD41 measurement and return {'co2', 'temp', 'rh'}.

    Holds hub._http_lck for the full write → sleep → read cycle so no
    other HTTP request can interleave and corrupt the GPIO state.
    Partial results (only passing CRC checks) are returned on errors.
    """
    i2c = SoftI2C(hub, sda, scl)
    result: dict[str, float | int] = {}

    async with hub._http_lck(_I2C_PRIORITY):
        await i2c.write(address, _SCD41_MEASURE_CMD)
    # Release lock during measurement — bus is idle after stop condition.
    await asyncio.sleep(_SCD41_MEASURE_DELAY)
    async with hub._http_lck(_I2C_PRIORITY):
        r = await i2c.read(address, 9)

    if r[2] != _crc8(r[0:2]):
        _LOGGER.warning("SCD41 CO2 CRC error  sda=%s scl=%s addr=0x%02x", sda, scl, address)
    else:
        result["co2"] = (r[0] << 8) | r[1]

    if r[5] != _crc8(r[3:5]):
        _LOGGER.warning("SCD41 temp CRC error sda=%s scl=%s addr=0x%02x", sda, scl, address)
    else:
        result["temp"] = round(-45 + 175 * ((r[3] << 8) | r[4]) / 65535, 2)

    if r[8] != _crc8(r[6:8]):
        _LOGGER.warning("SCD41 RH CRC error   sda=%s scl=%s addr=0x%02x", sda, scl, address)
    else:
        result["rh"] = round(100 * ((r[6] << 8) | r[7]) / 65535, 2)

    return result
