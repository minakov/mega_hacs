from dataclasses import dataclass, field
from typing import Optional
from bs4 import BeautifulSoup

inputs = [
    'eact',
    'inta',
    'misc',
]
selectors = [
    'pty',
    'm',
    'gr',
    'd',
    'ety',
]


@dataclass(frozen=True, eq=True)
class Config:
    pty: Optional[str] = None
    m: Optional[str] = None
    gr: Optional[str] = None
    d: Optional[str] = None
    ety: Optional[str] = None
    inta: Optional[str] = field(compare=False, hash=False, default=None)
    misc: Optional[str] = field(compare=False, hash=False, default=None)
    eact: Optional[str] = field(compare=False, hash=False, default=None)
    src: Optional[BeautifulSoup] = field(compare=False, hash=False, default=None)


def parse_config(page: str) -> 'Config':
    soup = BeautifulSoup(page, features="lxml")
    ret = {}
    for x in selectors:
        v = soup.find('select', {'name': x})
        if v is None:
            continue
        else:
            v = v.find(selected=True)
            if v:
                v = v['value']
                ret[x] = v
    for x in inputs:
        v = soup.find('input', {'name': x})
        if v:
            ret[x] = v['value']
    smooth = soup.find('input', {'name': 'misc'})
    if smooth is None or smooth.get('checked') is None:
        ret['misc'] = None
    return Config(**ret, src=soup)


def get_ext_mode(page: str) -> Optional[str]:
    soup = BeautifulSoup(page, features="lxml")
    ety_select = soup.find('select', {'name': 'ety'})
    if ety_select is not None:
        selected = ety_select.find(selected=True)
        if selected and selected['value']:
            return str(selected['value'])
    return None

DIGITAL_IN = Config(pty="0")
RELAY_OUT = Config(pty="1", m="0")
PWM_OUT = Config(pty="1", m="1")
DS2413 = Config(pty="1", m="2")
MCP230 = Config(pty="4", m="1", gr="3", d="20")
MCP230_OUT = Config(ety="1")
MCP230_IN = Config(ety="0")
PCA9685 = Config(pty="4", m="1", gr="3", d="21")
OWIRE_BUS = Config(pty="3", d="5")
