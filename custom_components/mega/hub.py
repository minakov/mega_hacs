import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import typing
import re
import json

from bs4 import BeautifulSoup

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, LIGHT_LUX, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from .config_parser import parse_config, DS2413, MCP230, MCP230_OUT, MCP230_IN, PCA9685, get_ext_mode
from .const import (
    TEMP,
    HUM,
    PRESS,
    LUX,
    PATT_SPLIT,
    DOMAIN,
    CONF_HTTP,
    EVENT_BINARY_SENSOR,
    CONF_CUSTOM,
    CONF_FORCE_D,
    CONF_DEF_RESPONSE,
    PATT_FW,
    CONF_FORCE_I2C_SCAN,
    REMOVE_CONFIG,
    CONF_RAW_I2C,
)
from .entities import set_events_off, BaseMegaEntity, MegaOutPort, safe_int
from .exceptions import CannotConnect, NoPort
from .i2c import parse_scan_page
from .raw_i2c import read_scd41
from .tools import make_ints, int_ignore, PriorityLock

TEMP_PATT = re.compile(r"temp:([01234567890\.]+)")
HUM_PATT = re.compile(r"hum:([01234567890\.]+)")
PRESS_PATT = re.compile(r"press:([01234567890\.]+)")
LUX_PATT = re.compile(r"lux:([01234567890\.]+)")
PATTERNS = {TEMP: TEMP_PATT, HUM: HUM_PATT, PRESS: PRESS_PATT, LUX: LUX_PATT}
UNITS = {TEMP: UnitOfTemperature.CELSIUS, HUM: PERCENTAGE, PRESS: "mmHg", LUX: LIGHT_LUX}
CLASSES = {
    TEMP: SensorDeviceClass.TEMPERATURE,
    HUM: SensorDeviceClass.HUMIDITY,
    PRESS: SensorDeviceClass.PRESSURE,
    LUX: SensorDeviceClass.ILLUMINANCE,
}
I2C_DEVICE_TYPES = {
    "2": LUX,  # BH1750
    "3": LUX,  # TSL2591
    "7": LUX,  # MAX44009
    "70": LUX,  # OPT3001
}


class MegaD:
    """MegaD Hub"""

    def __init__(
        self,
        hass: HomeAssistant,
        loop: asyncio.AbstractEventLoop,
        host: str,
        password: str,
        lg: logging.Logger,
        id: str,
        config: Optional[ConfigEntry] = None,
        mqtt_id: Optional[str] = None,
        scan_interval=60,
        port_to_scan=0,
        nports=38,
        update_all: bool = True,
        poll_outs: bool = False,
        fake_response: bool = True,
        force_d: Optional[bool] = None,
        allow_hosts: Optional[str] = None,
        protected=True,
        restore_on_restart=False,
        extenders=None,
        ext_in=None,
        ext_acts=None,
        i2c_sensors=None,
        new_naming=False,
        update_time=False,
        smooth: Optional[list] = None,
        **kwargs,
    ):
        """Initialize."""
        self.skip_ports = set()
        if config is not None:
            lg.debug(f"load config: %s", config.data)
        self.config = config
        self.http = hass.data.get(DOMAIN, {}).get(CONF_HTTP)
        if not self.http is None:
            self.http.allowed_hosts |= {host}
            self.http.hubs[host] = self
            if len(self.http.hubs) == 1:
                self.http.hubs["__def"] = self
            if mqtt_id:
                self.http.hubs[mqtt_id] = self
        self.smooth = smooth or []
        self.new_naming = new_naming
        self.extenders = extenders or []
        self.ext_in = ext_in or {}
        self.ext_act = ext_acts or {}
        self.i2c_sensors = i2c_sensors or []
        self._update_time = update_time
        self.poll_outs = poll_outs
        self.update_all = update_all if update_all is not None else True
        self.nports = nports
        self.fake_response = fake_response
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.hass = hass
        self.host = host
        self.sec = password
        self.id = id
        self.lck = asyncio.Lock()
        self.last_long = {}
        self._http_lck = PriorityLock()
        self._notif_lck = asyncio.Lock()
        self.cnd = asyncio.Condition()
        self.online = True
        self.entities: typing.List[BaseMegaEntity] = []
        self.ds2413_ports = set()
        self.poll_interval = scan_interval
        self.subs = None
        self.lg: logging.Logger = lg.getChild(self.id)
        self._scanned = {}
        self.sensors = []
        self.port_to_scan = port_to_scan
        self.last_update = datetime.now()
        self._callbacks: typing.DefaultDict[
            int, typing.List[typing.Callable[[dict], None]]
        ] = defaultdict(list)
        self._loop = loop
        self._customize = None
        self.values = {}
        self.last_port = None
        self.updater = DataUpdateCoordinator(
            hass,
            self.lg,
            name="megad",
            update_method=self.poll,
            update_interval=timedelta(seconds=self.poll_interval)
            if self.poll_interval
            else None,
        )
        self.updaters = []
        self.fw = ""
        self.notifiers = defaultdict(asyncio.Condition)
        if not mqtt_id:
            _id = host.split(".")[-1]
            self.mqtt_id = f"megad/{_id}"
        else:
            self.mqtt_id = mqtt_id
        self.restore_on_restart = restore_on_restart
        if force_d is not None:
            self.customize[CONF_FORCE_D] = force_d
        try:
            if DOMAIN in hass.data:
                if allow_hosts is not None:
                    _allow_hosts = set(allow_hosts.split(";"))
                    hass.data[DOMAIN][CONF_HTTP].allowed_hosts |= _allow_hosts
                hass.data[DOMAIN][CONF_HTTP].protected = protected
        except Exception:
            self.lg.exception("while setting allowed hosts")
        self.binary_sensors = []

    async def start(self):
        pass

    async def stop(self):
        if self.subs is not None:
            self.subs()
        for x in self._callbacks.values():
            x.clear()

    async def add_entity(self, ent):
        async with self.lck:
            self.entities.append(ent)

    async def get_sensors(self, only_list=False):
        self.lg.debug(self.sensors)
        ports = []
        for x in self.sensors:
            if only_list and x.http_cmd != "list":
                continue
            if x.port in ports:
                continue
            try:
                await self.get_port(x.port, force_http=True, http_cmd=x.http_cmd)
            except asyncio.TimeoutError:
                continue
            ports.append(x.port)

    @property
    def customize(self) -> dict:
        if self._customize is None:
            c = self.hass.data.get(DOMAIN, {}).get(CONF_CUSTOM) or {}
            c = c.get(self.id) or {}
            self._customize = c
        return self._customize or {}

    @property
    def force_d(self):
        return self.customize.get(CONF_FORCE_D, False)

    @property
    def def_response(self):
        return self.customize.get(CONF_DEF_RESPONSE, None)

    @property
    def is_online(self):
        return (datetime.now() - self.last_update).total_seconds() < (
            self.poll_interval + 10
        )

    def _warn_offline(self):
        if self.online:
            self.lg.warning("mega is offline")
            self.hass.states.async_set(
                f"mega.{self.id}",
                "offline",
            )
            self.online = False

    def _notify_online(self):
        if not self.online:
            self.hass.states.async_set(
                f"mega.{self.id}",
                "online",
            )
            self.online = True

    async def _get_ds2413(self):
        """
        обновление ds2413 устройств
        :return:
        """
        for x in self.ds2413_ports:
            self.lg.debug(f"poll ds2413 for %s", x)
            try:
                await self.get_port(
                    port=x, force_http=True, http_cmd="list", conv=False
                )
            except asyncio.TimeoutError:
                continue

    async def poll(self):
        """
        Polling ports
        """
        self.lg.debug("poll")
        if self._update_time:
            await self.update_time()
        for x in self.i2c_sensors:
            if not isinstance(x, dict):
                continue
            ret = await self._update_i2c(x)
            if isinstance(ret, dict):
                self.values.update(ret)

        _seen_raw: set = set()
        for cfg in self._raw_i2c_configs:
            sda = str(cfg.get('sda', ''))
            scl = str(cfg.get('scl', ''))
            addr = int(cfg.get('address', 0))
            key = (sda, scl, addr)
            if key in _seen_raw:
                continue
            _seen_raw.add(key)
            if cfg.get('type', 'scd41') == 'scd41':
                try:
                    vals = await read_scd41(self, sda, scl, addr)
                    for k, v in vals.items():
                        self.values[(sda, scl, addr, k)] = v
                except Exception:
                    self.lg.exception(
                        "raw I2C SCD41 poll error sda=%s scl=%s addr=0x%02x", sda, scl, addr
                    )

        for x in self.extenders:
            ret = await self._update_extender(x)
            if not isinstance(ret, dict):
                self.lg.warning(f"wrong updater result: {ret} from extender {x}")
                continue
            self.values.update(ret)

        await self.get_all_ports()
        await self.get_sensors(only_list=True)
        await self._get_ds2413()
        return self.values

    async def get_mqtt_id(self) -> str:
        async with aiohttp.request(
            "get", f"http://{self.host}/{self.sec}/?cf=2"
        ) as req:
            data = await req.text(encoding="iso-8859-5")
            soup = BeautifulSoup(data, features="lxml")
            _id = soup.find(attrs={"name": "mdid"})
            if _id:
                return str(_id["value"])
            return "megad/" + self.host.split(".")[-1]

    async def get_fw(self) -> str:
        data = await self.request()
        if data is None:
            return ""
        m = PATT_FW.search(data)
        return m.groups()[0] if m else ""

    async def send_command(self, port=None, cmd=None):
        return await self.request(pt=port, cmd=cmd)

    async def request(self, priority=0, **kwargs):
        cmd = "&".join([f"{k}={v}" for k, v in kwargs.items() if v is not None])
        url = f"http://{self.host}/{self.sec}"
        if cmd:
            url = f"{url}/?{cmd}"
        self.lg.debug("request: %s", url)
        async with self._http_lck(priority):
            for _ntry in range(3):
                try:
                    async with aiohttp.request(
                        "get", url=url, timeout=aiohttp.ClientTimeout(total=5)
                    ) as req:
                        if req.status != 200:
                            self.lg.warning(
                                "%s returned %s (%s)",
                                url,
                                req.status,
                                await req.text(encoding="iso-8859-5"),
                            )
                            return None
                        else:
                            ret = await req.text(encoding="iso-8859-5")
                            self.lg.debug("response %s", ret)
                            return ret
                except asyncio.TimeoutError:
                    self.lg.warning(f"timeout while requesting {url}")
                    # raise
                    await asyncio.sleep(1)
            raise asyncio.TimeoutError("after 3 tries")

    async def _raw_request(self, **kwargs) -> Optional[str]:
        """HTTP GET without acquiring _http_lck.  Caller must hold the lock."""
        cmd = "&".join([f"{k}={v}" for k, v in kwargs.items() if v is not None])
        url = f"http://{self.host}/{self.sec}"
        if cmd:
            url = f"{url}/?{cmd}"
        self.lg.debug("raw request: %s", url)
        for _ntry in range(3):
            try:
                async with aiohttp.request(
                    "get", url=url, timeout=aiohttp.ClientTimeout(total=5)
                ) as req:
                    if req.status != 200:
                        self.lg.warning("raw request %s returned %s", url, req.status)
                        return None
                    return await req.text(encoding="iso-8859-5")
            except asyncio.TimeoutError:
                self.lg.warning("timeout on raw request %s", url)
                await asyncio.sleep(1)
        return None

    @property
    def _raw_i2c_configs(self) -> list:
        """Raw I2C sensor entries from the YAML mega: block."""
        return (
            self.hass.data.get(DOMAIN, {})
            .get(CONF_CUSTOM, {})
            .get(self.id, {})
            .get(CONF_RAW_I2C, [])
        )

    async def save(self):
        await self.send_command(cmd="s")

    def parse_response(self, ret, cmd="get"):
        if ret is None:
            raise NoPort()
        if "busy" in ret:
            return None
        if ":" in ret:
            if ";" in ret:
                ret = ret.split(";")
            elif "/" in ret and not cmd == "list":
                ret = ret.split("/")
            else:
                ret = [ret]
            ret = {"value": dict([x.split(":") for x in ret if x.count(":") == 1])}
        elif "ON" in ret:
            ret = {"value": "ON"}
        elif "OFF" in ret:
            ret = {"value": "OFF"}
        else:
            ret = {"value": ret}
        return ret

    async def get_port(self, port, force_http=False, http_cmd="get", conv=True):
        """
        Запрос состояния порта. Состояние всегда возвращается в виде объекта, всегда сохраняется в центральное
        хранилище values
        """
        self.lg.debug(f"get port %s", port)
        if http_cmd == "list" and conv:
            await self.request(pt=port, cmd="conv")
            await asyncio.sleep(1)
        ret = self.parse_response(
            await self.request(pt=port, cmd=http_cmd), cmd=http_cmd
        )
        ntry = 0
        while http_cmd == "list" and ret is None and ntry < 3:
            await asyncio.sleep(1)
            ret = self.parse_response(await self.request(pt=port, cmd=http_cmd))
            ntry += 1
        self.lg.debug("parsed: %s", ret)
        self.values[port] = ret
        return ret

    @property
    def ports(self):
        return {e.port for e in self.entities if not isinstance(e.port, list)}

    async def get_all_ports(self, only_out=False, check_skip=False):
        try:
            ret = await self.request(cmd="all")
        except asyncio.TimeoutError:
            return
        if ret is None:
            return
        for port, x in enumerate(ret.split(";")):
            if port in self.ds2413_ports:
                continue
            if check_skip and not port in self.ports:
                continue
            ret = self.parse_response(x)
            self.values[port] = ret

    async def reboot(self, save=True):
        await self.save()

    async def _notify(self, port, value):
        async with self.notifiers[port]:
            cnd = self.notifiers[port]
            cnd.notify_all()

    def _process_msg(self, msg):
        try:
            d = msg.topic.split("/")
            port = d[-1]
        except ValueError:
            self.lg.warning("can not process %s", msg)
            return

        if port == "cmd":
            return
        try:
            port = int_ignore(port)
        except:
            self.lg.warning("can not process %s", msg)
            return
        self.lg.debug("process incomming message: %s", msg)
        value = None
        try:
            value = json.loads(msg.payload)
            if isinstance(value, dict):
                make_ints(value)
            self.values[port] = value
            for cb in self._callbacks[port]:
                cb(value)
            if isinstance(value, dict):
                value = value.copy()
                value["mega_id"] = self.id
                self.hass.bus.async_fire(
                    EVENT_BINARY_SENSOR,
                    value,
                )
        except Exception as exc:
            self.lg.warning(f"could not parse json ({msg.payload}): {exc}")
            return
        finally:
            asyncio.run_coroutine_threadsafe(self._notify(port, value), self._loop)

    def subscribe(self, port, callback):
        port = int_ignore(port)
        self.lg.debug(
            f"subscribe %s",
            port,
        )
        self.http.callbacks[self.id][port].append(callback)

    async def authenticate(self) -> bool:
        """Test if we can authenticate with the host."""
        async with aiohttp.request("get", url=f"http://{self.host}/{self.sec}") as req:
            if "Unauthorized" in await req.text(encoding="iso-8859-5"):
                return False
            else:
                if req.status != 200:
                    raise CannotConnect
                return True

    async def get_port_page(self, port):
        url = f"http://{self.host}/{self.sec}/?pt={port}"
        self.lg.debug(f"get page for port {port} {url}")
        async with aiohttp.request("get", url) as req:
            return await req.text(encoding="iso-8859-5")

    async def scan_port(self, port):
        data = await self.request(pt=port)
        if data is None:
            return None
        return parse_config(data)

    async def scan_ports(self, nports=37):
        for x in range(0, nports + 1):
            ret = await self.scan_port(x)
            if ret:
                yield x, ret
        self.nports = nports + 1

    async def _update_extender(self, port):
        """
        Обновление mcp230, так же подходит для PCA9685
        :param port:
        :return:
        """
        try:
            values = await self.request(pt=port, cmd="get")
        except asyncio.TimeoutError:
            return
        if values is None:
            return {}
        ret = {}
        for i, x in enumerate(values.split(";")):
            ret[f"{port}e{i}"] = x
        return ret

    async def _update_i2c(self, params):
        """
        Обновление портов i2c
        :param params: параметры url
        :return:
        """
        pt = params.get("pt")
        if pt in self.skip_ports:
            return
        if pt is not None:
            pass
        _params = tuple(params.items())
        delay = None
        if "delay" in params:
            delay = params.pop("delay")
        try:
            ret = {_params: await self.request(**params)}
        except asyncio.TimeoutError:
            return
        self.lg.debug("i2c response: %s", ret)
        if delay:
            self.lg.debug("delay %s", delay)
            await asyncio.sleep(delay)
        return ret

    async def get_config(self, nports=37) -> dict:
        ret: typing.Dict[str, typing.Any] = defaultdict(lambda: defaultdict(list))
        ret["mqtt_id"] = await self.get_mqtt_id()
        ret["extenders"] = extenders = []
        ret["ext_in"] = ext_int = {}
        ret["ext_acts"] = ext_acts = {}
        ret["i2c_sensors"] = i2c_sensors = []
        ret["smooth"] = smooth = []
        async for port, cfg in self.scan_ports(nports):
            _cust = self.customize.get(port)
            if not isinstance(_cust, dict):
                _cust = {}
            if cfg.pty == "0":
                ret["binary_sensor"][port].append({})
            elif cfg.pty == "1" and (cfg.m in ["0", "1", "3"] or cfg.m is None):
                if cfg.misc is not None:
                    smooth.append(port)
                ret["light"][port].append(
                    {"dimmer": cfg.m == "1", "smooth": safe_int(cfg.misc)}
                )
            elif cfg == DS2413:
                # ds2413
                _data = await self.get_port(
                    port=port, force_http=True, http_cmd="list", conv=False
                )
                if _data is None:
                    continue
                data = _data.get("value", {})
                if not isinstance(data, dict):
                    self.lg.warning(
                        f"can not add ds2413 on port {port}, it has wrong data: {_data}"
                    )
                    continue
                for addr, state in data.items():
                    ret["light"][port].extend(
                        [
                            {
                                "index": 0,
                                "addr": addr,
                                "id_suffix": f"{addr}_a",
                                "http_cmd": "ds2413",
                            },
                            {
                                "index": 1,
                                "addr": addr,
                                "id_suffix": f"{addr}_b",
                                "http_cmd": "ds2413",
                            },
                        ]
                    )
            elif cfg == MCP230:
                extenders.append(port)
                if cfg.inta:
                    ext_int[int_ignore(cfg.inta)] = port
                values = await self.request(pt=port, cmd="get")
                if values is None:
                    continue
                values = values.split(";")
                for n in range(len(values)):
                    ext_page = await self.request(pt=port, ext=n)
                    if ext_page is None:
                        continue
                    ext_cfg = parse_config(ext_page)
                    pt = f"{port}e{n}" if not self.new_naming else f"{port:02d}e{n:02d}"
                    if ext_cfg.ety == "1":
                        ret["light"][pt].append({})
                    elif ext_cfg.ety == "0":
                        if ext_cfg.eact:
                            ext_acts[pt] = ext_cfg.eact
                        ret["binary_sensor"][pt].append({})
            elif cfg == PCA9685:
                extenders.append(port)
                values = await self.request(pt=port, cmd="get")
                if values is None:
                    continue
                values = values.split(";")
                for n in range(len(values)):
                    pt = f"{port}e{n}"
                    name = pt if not self.new_naming else f"{port:02}e{n:02}"
                    port_type = await self.request(pt=port, ext=f"{n}")
                    if port_type is None:
                        continue
                    port_mode = get_ext_mode(port_type)
                    if port_mode == '0':
                        ret["light"][pt].append(
                            {
                                "dimmer": True,
                                "dimmer_scale": 16,
                                "name": f"{self.id}_{name}",
                            }
                        )
                    elif port_mode == '1':
                        ret["light"][pt].append({})
            if cfg.pty == "4":  # and (cfg.gr == '0' or _cust.get(CONF_FORCE_I2C_SCAN))
                # i2c в режиме ANY
                scan = cfg.src.find("a", text="I2C Scan") if cfg.src is not None else None
                self.lg.debug(f"find scan link: %s", scan)
                if scan is not None:
                    page = await self.request(pt=port, cmd="scan")
                    if page is None:
                        continue
                    req, parsed = parse_scan_page(page)
                    self.lg.debug(f"scan results: %s", (req, parsed))
                    ret["i2c"][port].extend(parsed)
                    i2c_sensors.extend(req)
            elif cfg.pty == "4" and cfg.m == "2":
                # scl исключаем из сканирования
                continue
            elif cfg.pty is None and nports < 30:
                # вроде как это ADC на 328 меге
                ret["sensor"][port].append(dict())
            elif cfg.pty in ("3", "2", "4"):
                http_cmd = "get"
                if cfg.d == "5" and cfg.pty == "3":
                    # 1-wire bus
                    values = await self.get_port(port, force_http=True, http_cmd="list")
                    http_cmd = "list"
                else:
                    values = await self.get_port(port, force_http=True)
                    if values is None or (
                        isinstance(values, dict)
                        and str(values.get("value")) in ("", "None")
                    ):
                        values = await self.get_port(
                            port, force_http=True, http_cmd="list"
                        )
                        http_cmd = "list"
                self.lg.debug(f"values: %s", values)
                if values is None:
                    self.lg.warning(
                        f"port {port} is of type sensor but response is None, skipping it"
                    )
                    continue
                if isinstance(values, dict) and "value" in values:
                    values = values["value"]
                if isinstance(values, str) and TEMP_PATT.search(values):
                    values = {TEMP: values}
                elif not isinstance(values, dict):
                    if cfg.pty == "4" and cfg.d in I2C_DEVICE_TYPES:
                        values = {I2C_DEVICE_TYPES.get(cfg.m or ""): values}
                    else:
                        values = {None: values}
                for key in values:
                    self.lg.debug(f"add sensor {key}")
                    ret["sensor"][port].append(
                        dict(
                            key=key,
                            unit_of_measurement=UNITS.get(key, UNITS[TEMP]),  # type: ignore[arg-type]
                            device_class=CLASSES.get(key, CLASSES[TEMP]),  # type: ignore[arg-type]
                            id_suffix=key,
                            http_cmd=http_cmd,
                        )
                    )
        return ret

    async def restore_states(self):
        for x in self.entities:
            if isinstance(x, MegaOutPort):
                if x.is_on:
                    await x.async_turn_on(brightness=x.brightness)
                else:
                    await x.async_turn_off()

    async def update_time(self):
        await self.request(cf=7, stime=datetime.now().strftime("%H:%M:%S"))

    async def reload(self, reload_entry=True):
        new = await self.get_config(nports=self.nports)
        if self.config is None:
            return new
        cfg = dict(self.config.data)
        for x in REMOVE_CONFIG:
            cfg.pop(x, None)
        cfg.update(new)
        self.lg.debug(f"new config: %s", cfg)
        self.hass.config_entries.async_update_entry(entry=self.config, data=cfg)
        if reload_entry:
            await self.hass.config_entries.async_reload(self.config.entry_id)
        return cfg


    def _wrap_port_smooth(self, from_, to_, time):
        self.lg.debug("dim from %s to %s for %s seconds", from_, to_, time)
        if time <= 0:
            return
        beg = datetime.now()
        diff = to_ - from_
        while True:
            _pct = (datetime.now() - beg).total_seconds() / time
            if _pct > 1:
                return
            val = from_ + round(diff * _pct)
            yield val

    async def smooth_dim(
        self,
        *config: typing.Tuple[typing.Any, int, int],
        time: float,
        jitter: int = 50,
        ws=False,
        updater=None,
        can_smooth_hardware=False,
        max_values=None,
        chip=None,
    ):
        """
        Плавное диммирование силами сервера, сразу нескольких портов (одной командой)

        :param config: [(port, from, to), (port, from, to)]
        :param time: время на диммирование
        :param jitter: дополнительное замедление между командами в милисекундах
        :param ws: если True, используется режим ws21xx
        :param updater: функция, в которую передается текущее состояние
        :param can_smooth_hardware: если True, используется аппаратная реализация smooth
        :param max_values: максимальные значения (необходимы для расчета тайминга аппаратного smooth)
        :param chip: кол-во чипов для ws-лент
        :return:
        """
        if can_smooth_hardware and max_values is not None:
            for i, (pt, from_, to_) in enumerate(config):
                pct = abs(from_ - to_) / max_values[i]
                tm = max([round(time / pct), 1])
                await self.request(pt=pt, pwm=to_, cnt=tm)

        last_step = tuple([to_ for (_, _, to_) in config])
        gen = [self._wrap_port_smooth(f, t, time) for (_, f, t) in config]
        c = None
        stop = False
        while True:
            if stop:
                return
            await asyncio.sleep(jitter / 1000)
            try:
                _next_val = tuple([next(x) for x in gen])
            except StopIteration:
                _next_val = last_step
                stop = True
            if _next_val == c:
                continue
            if updater is not None:
                updater(_next_val)
            if can_smooth_hardware:
                if _next_val == last_step:
                    return
                continue
            if not ws:
                _cmd = dict(
                    cmd=";".join(
                        [f"{pt}:{_next_val[i]}" for i, (pt, _, _) in enumerate(config)]
                    )
                )
                await self.request(**_cmd)  # type: ignore[arg-type]
            else:
                # для адресных лент
                _cmd = dict(
                    pt=config[0][0],
                    chip=chip,
                    ws="".join(
                        [hex(x).split("x")[1].rjust(2, "0").upper() for x in _next_val]
                    ),
                )
                await self.request(**_cmd)  # type: ignore[arg-type]

            if _next_val == last_step:
                return
            c = _next_val
