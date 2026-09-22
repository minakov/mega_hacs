"""Software I2C over MegaD GPIO pins (HTTP bit-bang) and sensor drivers.

MegaD-328 has no native I2C support in firmware.  ab-log.ru ships the
"I2C-PHP" library that drives SDA/SCL through ordinary HTTP commands; this
module is an asyncio port of that approach so Home Assistant can read I2C
sensors attached to any MegaD-328 input port (for example through the
MegaD-14-IN module) without an intermediate PHP server.

Two transfer modes mirror the library's ``define("V", ...)`` setting:

* mode 1 - fully software: every SCL/SDA transition is one HTTP request.
* mode 2 - "partially hardware": a whole byte is written with a single
  ``pt=SDA&i2c=<byte>&scl=SCL:1;SCL:0;`` request (firmware >= 3.38).
  Reads are still bit-banged.

Every HTTP request goes through ``hub.request`` and therefore takes the
hub's priority lock only for the duration of that request, so relay and
dimmer commands are not blocked while a slow I2C transaction is running.
Other ports' commands never touch the SDA/SCL pins, so the bus state is
preserved between requests.

Supported drivers (``type`` in the ``raw_i2c`` YAML list):

* ``wallmount_d`` - MegaD-WallMount-Sensor-CO2-D, and any bare Sensirion
  SCD41 on the bus (CO2 / temperature / humidity)
* ``htu21d``   - HTU21D / Si7021 / SHT21 (temperature / humidity)
* ``opt3001``  - TI OPT3001 ambient light (lux)
* ``max44009`` - Maxim MAX44009 ambient light (lux)
* ``outdoor``  - MegaD-Outdoor-Sensor: HTU21D + OPT3001 (or MAX44009 on
  units made from 2024) on one bus; the light chip is auto-detected.
"""
from __future__ import annotations

import asyncio
import logging
import typing

if typing.TYPE_CHECKING:
    from .hub import MegaD

_LOGGER = logging.getLogger(__name__)

_CRC8_POLY = 0x31

# I2C lock priority - lower number wins; 100 lets normal requests (-1..0) go first.
_I2C_PRIORITY = 100

# Value keys produced by the drivers
KEY_CO2 = "co2"
KEY_TEMP = "temp"
KEY_HUM = "hum"
KEY_RH = "rh"      # legacy humidity key used by the SCD41 driver
KEY_LUX = "lux"


def _crc8(data: typing.Sequence[int], init: int = 0xFF) -> int:
    """CRC-8 with polynomial 0x31 (Sensirion / Measurement Specialties)."""
    c = init
    for byte in data:
        c ^= byte
        for _ in range(8):
            c = ((c << 1) ^ _CRC8_POLY) if (c & 0x80) else (c << 1)
    return c & 0xFF


def _hex(data: typing.Sequence[int]) -> str:
    return " ".join(f"{b:02X}" for b in data)


class I2CError(Exception):
    """Raised when the slave did not answer or returned bad data."""


class SoftI2C:
    """Software I2C using MegaD GPIO via HTTP.

    ``mode`` 1 bit-bangs writes, ``mode`` 2 uses the firmware's ``i2c=``
    byte-write helper (I2C-PHP "V=2").
    """

    def __init__(self, hub: MegaD, sda: str, scl: str, mode: int = 1) -> None:
        self._hub = hub
        self._sda = str(sda)
        self._scl = str(scl)
        self._mode = 2 if int(mode) == 2 else 1

    async def _r(self, **kw: typing.Any) -> str:
        ret = await self._hub.request(priority=_I2C_PRIORITY, **kw)
        return (ret or "").strip()

    async def _dir(self, pin: str, out: bool) -> None:
        await self._r(pt=pin, dir=1 if out else 0)

    async def _get(self, pin: str) -> str:
        """Sample an input pin.  Re-reads (harmless) if the answer is not ON/OFF."""
        val = ""
        for attempt in range(3):
            val = await self._r(pt=pin, cmd="get")
            if val in ("ON", "OFF"):
                return val
            _LOGGER.debug("unexpected answer %r while sampling pin %s (try %s)", val, pin, attempt + 1)
        return val

    async def _init(self) -> None:
        """Set both pins to output/high and recover a possibly stuck bus.

        A slave left mid-transfer (after a lost request or a mis-wired
        probe) holds SDA low until it gets clocks: clock SCL 9 times with
        SDA released, then issue a STOP.  This costs 3 requests per
        transaction and makes every transaction self-healing.
        """
        await self._dir(self._scl, True)
        await self._r(pt=self._sda, dir=0, cmd=f"{self._scl}:1")
        await self._r(cmd=";".join(f"{self._scl}:0;{self._scl}:1" for _ in range(9)))
        # STOP: SDA low -> SCL high (already) -> SDA high
        await self._r(pt=self._sda, dir=1, cmd=f"{self._sda}:0")
        await self._r(cmd=f"{self._scl}:1;{self._sda}:1")

    async def _start(self) -> None:
        await self._r(cmd=f"{self._sda}:0;{self._scl}:0")

    async def _stop(self) -> None:
        await self._r(cmd=f"{self._sda}:0;{self._scl}:1;{self._sda}:1")

    # NOTE on combined ``pt=..&dir=..&cmd=..`` requests: the order in which
    # the firmware applies ``dir`` and ``cmd`` is not documented, so they are
    # only combined where both orders produce the same bus waveform (SCL low
    # while SDA changes).  Releasing SDA while SCL is high would be a STOP.

    async def _send_byte(self, byte: int) -> typing.Optional[bool]:
        """Send one byte.  Returns True on ACK, False on NACK, None if unknown.

        The ACK value is best effort: it is sampled after the ACK clock
        pulse, as the I2C-PHP library does, so drivers must not rely on it.
        """
        if self._mode == 2:
            await self._r(pt=self._sda, i2c=byte & 0xFF, scl=f"{self._scl}:1;{self._scl}:0;")
            return None
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            await self._r(cmd=f"{self._sda}:{bit};{self._scl}:1;{self._scl}:0;{self._sda}:0")
        # ACK phase: release SDA (input), pulse SCL, restore SDA (output, low)
        await self._r(pt=self._sda, dir=0, cmd=f"{self._scl}:1;{self._scl}:0")
        ack = await self._r(pt=self._sda, cmd="get", dir=1)
        # ON = transistor conducting = line LOW = ACK
        if ack == "ON":
            return True
        if ack == "OFF":
            return False
        return None

    async def _read_byte(self, nack: bool = False) -> int:
        # SCL is already low here; release SDA so the slave can drive it
        await self._r(pt=self._sda, dir=0, cmd=f"{self._scl}:0")
        bits = 0
        for _ in range(8):
            await self._r(cmd=f"{self._scl}:1")
            val = await self._get(self._sda)
            # ON = transistor conducting = line LOW = I2C logical 0
            bits = (bits << 1) | (0 if val == "ON" else 1)
            await self._r(cmd=f"{self._scl}:0")
        # Master ACK (SDA low) / NACK (SDA high) while SCL is low ...
        await self._r(pt=self._sda, dir=1, cmd=f"{self._sda}:{1 if nack else 0}")
        # ... clock it out ...
        await self._r(cmd=f"{self._scl}:1")
        await self._r(cmd=f"{self._scl}:0")
        # ... and release SDA only after SCL is low again (no false STOP)
        await self._dir(self._sda, False)
        return bits

    async def write(self, address: int, data: typing.Sequence[int]) -> typing.Optional[bool]:
        """Write ``data`` to ``address``.  Returns the ACK of the address byte."""
        await self._init()
        await self._start()
        ack = await self._send_byte(address << 1)
        for b in data:
            await self._send_byte(b)
        await self._stop()
        return ack

    async def read(self, address: int, n: int) -> list[int]:
        await self._init()
        await self._start()
        await self._send_byte((address << 1) | 1)
        result = [await self._read_byte(nack=(i == n - 1)) for i in range(n)]
        # SDA is released after the last byte; take it back before STOP
        await self._dir(self._sda, True)
        await self._stop()
        return result

    async def write_read(
        self,
        address: int,
        data: typing.Sequence[int],
        n: int,
        delay: float = 0,
    ) -> list[int]:
        """Write ``data`` (e.g. a register pointer), optionally wait, read ``n`` bytes."""
        await self.write(address, data)
        if delay:
            await asyncio.sleep(delay)
        return await self.read(address, n)


# ── SCD41 ────────────────────────────────────────────────────────────────────

_SCD41_MEASURE_CMD = [0xEC, 0x05]   # measure single-shot
_SCD41_MEASURE_DELAY = 5.5          # seconds; SCD41 datasheet max 5000 ms
SCD41_DEFAULT_ADDR = 0x62


async def read_scd41(
    hub: MegaD,
    sda: str,
    scl: str,
    address: typing.Optional[int] = None,
    mode: int = 1,
    **_: typing.Any,
) -> dict[str, float | int]:
    """Trigger one SCD41 measurement and return {'co2', 'temp', 'rh'}.

    Partial results (only values passing CRC checks) are returned on errors.
    """
    address = SCD41_DEFAULT_ADDR if address is None else address
    i2c = SoftI2C(hub, sda, scl, mode)
    result: dict[str, float | int] = {}

    r = await i2c.write_read(address, _SCD41_MEASURE_CMD, 9, delay=_SCD41_MEASURE_DELAY)
    if all(b == 0xFF for b in r):
        raise I2CError("no answer from SCD41 (bus reads all ones)")

    if r[2] != _crc8(r[0:2]):
        _LOGGER.debug("SCD41 CO2 CRC error  sda=%s scl=%s addr=0x%02x frame=%s", sda, scl, address, _hex(r))
    else:
        result[KEY_CO2] = (r[0] << 8) | r[1]

    if r[5] != _crc8(r[3:5]):
        _LOGGER.debug("SCD41 temp CRC error sda=%s scl=%s addr=0x%02x frame=%s", sda, scl, address, _hex(r))
    else:
        result[KEY_TEMP] = round(-45 + 175 * ((r[3] << 8) | r[4]) / 65535, 2)

    if r[8] != _crc8(r[6:8]):
        _LOGGER.debug("SCD41 RH CRC error   sda=%s scl=%s addr=0x%02x frame=%s", sda, scl, address, _hex(r))
    else:
        result[KEY_RH] = round(100 * ((r[6] << 8) | r[7]) / 65535, 2)

    return result


# ── HTU21D / Si7021 / SHT21 ──────────────────────────────────────────────────

HTU21D_DEFAULT_ADDR = 0x40
_HTU21D_TRIG_TEMP_NOHOLD = 0xF3
_HTU21D_TRIG_HUM_NOHOLD = 0xF5
_HTU21D_MEASURE_DELAY = 0.1     # datasheet: 50 ms (temp, 14 bit) / 16 ms (hum, 12 bit)


async def _htu21d_measure(i2c: SoftI2C, address: int, cmd: int) -> typing.Optional[int]:
    """Run one no-hold measurement, return the 16-bit raw value or None on CRC error."""
    r = await i2c.write_read(address, [cmd], 3, delay=_HTU21D_MEASURE_DELAY)
    if r[0] == 0xFF and r[1] == 0xFF and r[2] == 0xFF:
        raise I2CError("no answer from HTU21D (bus reads all ones)")
    if _crc8(r[0:2], init=0x00) != r[2]:
        _LOGGER.debug("HTU21D cmd 0x%02X bad frame %s", cmd, _hex(r))
        return None
    # status bit 1: 0 = temperature frame, 1 = humidity frame; an all-zero frame
    # passes CRC but is physically impossible (-46.85 C / -6 %)
    expect_hum = cmd in (_HTU21D_TRIG_HUM_NOHOLD, 0xE5)
    if bool(r[1] & 0x02) != expect_hum or (r[0] == 0 and r[1] == 0):
        _LOGGER.debug("HTU21D cmd 0x%02X implausible frame %s", cmd, _hex(r))
        return None
    # two LSBs are status bits
    return ((r[0] << 8) | r[1]) & 0xFFFC


async def read_htu21d(
    hub: MegaD,
    sda: str,
    scl: str,
    address: typing.Optional[int] = None,
    mode: int = 1,
    **_: typing.Any,
) -> dict[str, float]:
    """Read HTU21D temperature and humidity -> {'temp', 'hum'}."""
    address = HTU21D_DEFAULT_ADDR if address is None else address
    i2c = SoftI2C(hub, sda, scl, mode)
    result: dict[str, float] = {}

    raw = await _htu21d_measure(i2c, address, _HTU21D_TRIG_TEMP_NOHOLD)
    if raw is None:
        _LOGGER.debug("HTU21D temp CRC error sda=%s scl=%s addr=0x%02x", sda, scl, address)
    else:
        result[KEY_TEMP] = round(-46.85 + 175.72 * raw / 65536, 2)

    raw = await _htu21d_measure(i2c, address, _HTU21D_TRIG_HUM_NOHOLD)
    if raw is None:
        _LOGGER.debug("HTU21D hum CRC error  sda=%s scl=%s addr=0x%02x", sda, scl, address)
    else:
        hum = -6 + 125 * raw / 65536
        result[KEY_HUM] = round(min(max(hum, 0.0), 100.0), 2)

    return result


# ── OPT3001 ──────────────────────────────────────────────────────────────────

OPT3001_DEFAULT_ADDR = 0x44          # ADDR pin -> GND; 0x45 -> VDD
OPT3001_ADDRESSES = (0x44, 0x45, 0x46, 0x47)
_OPT3001_REG_RESULT = 0x00
_OPT3001_REG_CONFIG = 0x01
_OPT3001_REG_DEVICE_ID = 0x7F
_OPT3001_DEVICE_ID = 0x3001
# auto full-scale range, 800 ms conversion, continuous conversions
_OPT3001_CONFIG_CONTINUOUS = (0xCE, 0x10)
_OPT3001_CONVERSION_DELAY = 1.0


async def opt3001_present(i2c: SoftI2C, address: int) -> bool:
    """True if an OPT3001 answers at ``address`` (device-ID register check)."""
    r = await i2c.write_read(address, [_OPT3001_REG_DEVICE_ID], 2)
    return ((r[0] << 8) | r[1]) == _OPT3001_DEVICE_ID


async def read_opt3001(
    hub: MegaD,
    sda: str,
    scl: str,
    address: typing.Optional[int] = None,
    mode: int = 1,
    **_: typing.Any,
) -> dict[str, float]:
    """Read OPT3001 ambient light -> {'lux'}."""
    address = OPT3001_DEFAULT_ADDR if address is None else address
    i2c = SoftI2C(hub, sda, scl, mode)
    # (re)arm continuous conversion; harmless when already running
    await i2c.write(address, [_OPT3001_REG_CONFIG, *_OPT3001_CONFIG_CONTINUOUS])
    await asyncio.sleep(_OPT3001_CONVERSION_DELAY)
    r = await i2c.write_read(address, [_OPT3001_REG_RESULT], 2)
    if r[0] == 0xFF and r[1] == 0xFF:
        raise I2CError("no answer from OPT3001 (bus reads all ones)")
    exponent = r[0] >> 4
    if exponent > 0x0B:
        raise I2CError(f"OPT3001 implausible result {_hex(r)}")
    mantissa = ((r[0] & 0x0F) << 8) | r[1]
    return {KEY_LUX: round(0.01 * (1 << exponent) * mantissa, 2)}


# ── MAX44009 ─────────────────────────────────────────────────────────────────

MAX44009_DEFAULT_ADDR = 0x4A         # A0 pin -> GND; 0x4B -> VCC
MAX44009_ADDRESSES = (0x4A, 0x4B)
_MAX44009_REG_LUX_HIGH = 0x03
_MAX44009_REG_LUX_LOW = 0x04
_MAX44009_REG_THRESH_HIGH = 0x05   # power-on default 0xFF
_MAX44009_REG_THRESH_LOW = 0x06    # power-on default 0x00


async def max44009_present(i2c: SoftI2C, address: int) -> bool:
    """True if a MAX44009 with default threshold registers answers at ``address``.

    The chip has no ID register; the threshold defaults are a cheap
    signature that random noise on a broken line is very unlikely to match.
    """
    high = (await i2c.write_read(address, [_MAX44009_REG_THRESH_HIGH], 1))[0]
    low = (await i2c.write_read(address, [_MAX44009_REG_THRESH_LOW], 1))[0]
    return high == 0xFF and low == 0x00


async def read_max44009(
    hub: MegaD,
    sda: str,
    scl: str,
    address: typing.Optional[int] = None,
    mode: int = 1,
    **_: typing.Any,
) -> dict[str, float]:
    """Read MAX44009 ambient light -> {'lux'} (default continuous auto-range mode)."""
    address = MAX44009_DEFAULT_ADDR if address is None else address
    i2c = SoftI2C(hub, sda, scl, mode)
    high = (await i2c.write_read(address, [_MAX44009_REG_LUX_HIGH], 1))[0]
    low = (await i2c.write_read(address, [_MAX44009_REG_LUX_LOW], 1))[0]
    if high == 0xFF and low == 0xFF:
        raise I2CError("no answer from MAX44009 (bus reads all ones)")
    if low & 0xF0:
        # bits 7:4 of the low lux register always read 0 on a real chip
        raise I2CError(f"MAX44009 implausible result {_hex([high, low])}")
    exponent = high >> 4
    if exponent == 0x0F:
        raise I2CError("MAX44009 overrange")
    mantissa = ((high & 0x0F) << 4) | (low & 0x0F)
    return {KEY_LUX: round((1 << exponent) * mantissa * 0.045, 2)}


# ── MegaD-Outdoor-Sensor (HTU21D + OPT3001 / MAX44009) ───────────────────────

LIGHT_AUTO = "auto"
LIGHT_OPT3001 = "opt3001"
LIGHT_MAX44009 = "max44009"
LIGHT_SENSORS = (LIGHT_AUTO, LIGHT_OPT3001, LIGHT_MAX44009)


async def _detect_light_sensor(
    hub: MegaD, sda: str, scl: str, mode: int
) -> typing.Optional[tuple[str, int]]:
    """Probe the bus for OPT3001 then MAX44009; returns (type, address) or None."""
    i2c = SoftI2C(hub, sda, scl, mode)
    for addr in OPT3001_ADDRESSES[:2]:
        try:
            if await opt3001_present(i2c, addr):
                return LIGHT_OPT3001, addr
        except I2CError:
            continue
    for addr in MAX44009_ADDRESSES:
        try:
            if await max44009_present(i2c, addr):
                await read_max44009(hub, sda, scl, addr, mode)
                return LIGHT_MAX44009, addr
        except I2CError:
            continue
    return None


async def read_outdoor(
    hub: MegaD,
    sda: str,
    scl: str,
    address: typing.Optional[int] = None,
    mode: int = 1,
    light: str = LIGHT_AUTO,
    light_address: typing.Optional[int] = None,
    **_: typing.Any,
) -> dict[str, float]:
    """Read MegaD-Outdoor-Sensor -> {'temp', 'hum', 'lux'}.

    ``address`` is the HTU21D address (default 0x40).  The light chip is
    detected once per bus and remembered in ``hub.raw_i2c_state``.
    """
    result: dict[str, float] = {}
    try:
        result.update(await read_htu21d(hub, sda, scl, address, mode))
    except I2CError as exc:
        _LOGGER.warning("Outdoor sensor sda=%s scl=%s: %s", sda, scl, exc)

    state_key = ("outdoor_light", str(sda), str(scl))
    detected: typing.Optional[tuple[str, int]]
    if light != LIGHT_AUTO:
        default = OPT3001_DEFAULT_ADDR if light == LIGHT_OPT3001 else MAX44009_DEFAULT_ADDR
        detected = (light, light_address if light_address is not None else default)
    else:
        detected = hub.raw_i2c_state.get(state_key)
        if detected is None:
            detected = await _detect_light_sensor(hub, sda, scl, mode)
            if detected is None:
                _LOGGER.warning(
                    "Outdoor sensor sda=%s scl=%s: no OPT3001/MAX44009 found on the bus",
                    sda, scl,
                )
                return result
            _LOGGER.info(
                "Outdoor sensor sda=%s scl=%s: detected %s at 0x%02x",
                sda, scl, detected[0], detected[1],
            )
            hub.raw_i2c_state[state_key] = detected

    reader = read_opt3001 if detected[0] == LIGHT_OPT3001 else read_max44009
    try:
        result.update(await reader(hub, sda, scl, detected[1], mode))
    except I2CError as exc:
        _LOGGER.warning("Outdoor sensor sda=%s scl=%s light: %s", sda, scl, exc)
        if light == LIGHT_AUTO:
            hub.raw_i2c_state.pop(state_key, None)   # re-detect next poll
    return result


# ── Registry ─────────────────────────────────────────────────────────────────

Driver = typing.Callable[..., typing.Awaitable[dict]]

TYPE_WALLMOUNT_D = "wallmount_d"
TYPE_HTU21D = "htu21d"
TYPE_OPT3001 = "opt3001"
TYPE_MAX44009 = "max44009"
TYPE_OUTDOOR = "outdoor"

RAW_I2C_DRIVERS: dict[str, Driver] = {
    TYPE_WALLMOUNT_D: read_scd41,
    TYPE_HTU21D: read_htu21d,
    TYPE_OPT3001: read_opt3001,
    TYPE_MAX44009: read_max44009,
    TYPE_OUTDOOR: read_outdoor,
}

# Value keys each driver produces (used to create sensor entities)
RAW_I2C_KEYS: dict[str, tuple[str, ...]] = {
    TYPE_WALLMOUNT_D: (KEY_CO2, KEY_TEMP, KEY_RH),
    TYPE_HTU21D: (KEY_TEMP, KEY_HUM),
    TYPE_OPT3001: (KEY_LUX,),
    TYPE_MAX44009: (KEY_LUX,),
    TYPE_OUTDOOR: (KEY_TEMP, KEY_HUM, KEY_LUX),
}

RAW_I2C_TYPES = tuple(RAW_I2C_DRIVERS)


def raw_i2c_value_key(sda: str, scl: str, sensor_type: str, key: str) -> tuple:
    """Key under which hub.values stores one raw-I2C reading."""
    return ("raw_i2c", str(sda), str(scl), sensor_type, key)


async def poll_raw_i2c(hub: MegaD, cfg: dict) -> None:
    """Read one ``raw_i2c`` YAML entry and store results in ``hub.values``.

    Bit-banged reads on MegaD-328 lose a word to a CRC error now and then
    (~20 % of 9-byte SCD41 frames in practice).  When a device answered but
    some values are missing, the read is repeated once; a warning is logged
    only if values are still missing after the retry.
    """
    sensor_type = cfg.get("type", TYPE_WALLMOUNT_D)
    driver = RAW_I2C_DRIVERS.get(sensor_type)
    if driver is None:
        _LOGGER.warning("unknown raw_i2c type %s", sensor_type)
        return
    sda = str(cfg.get("sda", ""))
    scl = str(cfg.get("scl", ""))
    kwargs = dict(
        address=cfg.get("address"),
        mode=cfg.get("mode", 1),
        light=cfg.get("light", LIGHT_AUTO),
        light_address=cfg.get("light_address"),
    )
    expected = set(RAW_I2C_KEYS.get(sensor_type, ()))
    vals: dict = {}
    for attempt in range(2):
        try:
            got = await driver(hub, sda, scl, **kwargs)
        except (I2CError, asyncio.TimeoutError) as exc:
            _LOGGER.warning("raw I2C %s poll error sda=%s scl=%s: %s", sensor_type, sda, scl, exc)
            return
        except Exception:
            _LOGGER.exception("raw I2C %s poll error sda=%s scl=%s", sensor_type, sda, scl)
            return
        vals.update(got)
        if expected <= set(vals) or not got:
            # complete, or nothing answered at all (device absent) - do not retry
            break
        _LOGGER.debug("raw I2C %s sda=%s scl=%s: partial frame %s, retrying", sensor_type, sda, scl, got)
    missing = expected - set(vals)
    if missing and vals:
        _LOGGER.warning(
            "raw I2C %s sda=%s scl=%s: no valid value for %s after retry",
            sensor_type, sda, scl, ", ".join(sorted(missing)),
        )
    _LOGGER.debug("raw I2C %s sda=%s scl=%s -> %s", sensor_type, sda, scl, vals)
    for k, v in vals.items():
        hub.values[raw_i2c_value_key(sda, scl, sensor_type, k)] = v
