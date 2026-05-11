# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant custom integration (HACS) for MegaD-2561 and MegaD-328 Ethernet smart home controllers. The integration bridges Home Assistant with MegaD hardware via HTTP polling and push callbacks. All code lives under `custom_components/mega/`.

## Validation Commands

There is no local test suite. Correctness is verified via GitHub Actions:

- **HACS validation**: `.github/workflows/validate.yaml` — runs `hacs/action`
- **HA manifest validation**: `.github/workflows/hassfest.yaml` — runs `home-assistant/actions/hassfest`
- **Docs deploy**: `.github/workflows/main.yml` — builds and deploys MkDocs to GitHub Pages

To validate the manifest and integration metadata locally, install the `homeassistant` package and run `python -m script.hassfest` from an HA dev environment.

Version bumps are managed via `bumpversion` (see `.bumpversion.cfg`); it updates `manifest.json` and creates git tags.

## Architecture

### Communication Flow

```
MegaD hardware ──HTTP──> hub.py (DataUpdateCoordinator)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
           light.py       sensor.py      switch.py / binary_sensor.py
              │               │               │
              └───────────────┴───────────────┘
                              │
                        entities.py (base classes)
                              │
                         Home Assistant
```

Push callbacks from MegaD arrive at the `/mega` HTTP endpoint handled by `http.py`, which dispatches state updates and fires HA events for button interactions (single/double/long press).

### Key Files

| File | Role |
|------|------|
| `hub.py` | Central `MegaHub` class — all HTTP communication with MegaD, polling via `DataUpdateCoordinator`, port config parsing (BeautifulSoup), extender support |
| `entities.py` | Base entity classes: `BaseMegaEntity`, `MegaOutPort` (outputs with smooth dimming), `MegaPushEntity` (push-update receiver) |
| `light.py` | Relay outputs, PWM dimmers, RGB/RGBW LED strips (ws28xx), smooth brightness transitions |
| `sensor.py` | Temperature, humidity, pressure, luminosity, 1-Wire; long-term statistics support |
| `http.py` | `/mega` push endpoint — receives real-time callbacks from MegaD, handles allowed_hosts auth and Jinja2 response templates |
| `config_flow.py` | UI-based config flow for adding hubs |
| `i2c.py` | I2C device scanning — parses MegaD HTML responses to discover I2C sensors |
| `config_parser.py` | BeautifulSoup parsing of MegaD HTML config pages |
| `tools.py` | `PriorityLock` for sequential command dispatch, unit conversion helpers |
| `const.py` | All voluptuous schema definitions and constants |

### Data Flow Patterns

- **Polling**: `MegaHub` extends `DataUpdateCoordinator`; platforms subscribe via `CoordinatorEntity`.
- **Push updates**: MegaD POSTs to `/mega`; `http.py` updates entity state directly without waiting for the next poll.
- **State restoration**: All entities extend `RestoreEntity`; last state is restored on HA restart before the first poll completes.
- **Sequential commands**: `PriorityLock` in `tools.py` ensures commands to a single MegaD are serialized to avoid races.
- **Multi-hub**: Multiple `MegaHub` instances can coexist; each is a separate config entry.

### Configuration

Entities can be customized via YAML in `configuration.yaml` using the `mega:` domain (see `docs/yaml.md`). The UI config flow (`config_flow.py`) handles initial hub setup. Voluptuous schemas in `const.py` validate all configuration.

### Dependencies

Declared in `manifest.json`: `beautifulsoup4`, `lxml` (HTML config parsing), `aiohttp` (implicit via HA). MQTT is an optional after-dependency for MQTT-based push.

## Documentation

Full Russian-language docs in `docs/` are built with MkDocs (Material theme) and published to GitHub Pages. `mkdocs.yml` is the build config.
