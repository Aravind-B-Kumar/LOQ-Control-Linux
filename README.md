# Legion Control

A small, portable Linux replacement for the parts of Lenovo Vantage you use:
battery charge limit, Custom-mode CPU/GPU limits, power mode.
Pure Python 3 standard library + a browser UI. Nothing to install.
Tested on `LOQ-15ARP9` . 

## Run

```bash
chmod +x start.sh service.sh      # needed once if the files came from Windows/zip
./start.sh --probe                # optional: see what your laptop/kernel exposes
./start.sh                        # asks for your password once, opens the UI in your browser
```

Close it with the **Close app** button (it also exits by itself ~3 minutes after the tab is closed).

## What it controls and how

| Feature | Linux interface used |
|---|---|
| Conservation mode (75–80%) / Normal (100%) | `ideapad_laptop` → `/sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode` (or battery `charge_types`, or `charge_control_*_threshold` if your battery driver has real thresholds) |
| Limit 90% | Real threshold if supported; otherwise software: conservation ON at 90%, OFF at 87% |
| CPU Temperature Limit, PL1/PL2/Peak, Cross-Loading, APU sPPT, cTGP, Dynamic Boost (both ways), TPP-on-AC offset, GPU Temperature Limit | `/sys/class/firmware-attributes/lenovo*/attributes/*` (mainline `lenovo-wmi-other`, Linux 6.17+), fallback LenovoLegionLinux `/sys/bus/platform/drivers/legion/*/` |
| Power mode incl. `custom` | `/sys/firmware/acpi/platform_profile` |
| Fan curve / max fan `Untested` | hwmon `pwm*_auto_point*` + `fan_fullspeed` (LenovoLegionLinux) |

Settings that your kernel/firmware doesn't expose are hidden or shown with an explanation, and
the app never invents values: ranges come from the firmware (`min_value`/`max_value`).
Tunables are written in the `custom` power mode (the app switches to it when you apply).

If Custom mode or fan control shows "not found", your kernel doesn't expose them yet

## Background 90% limit (optional)

On laptops with only on/off conservation mode, 90% is emulated while the app runs.
To keep it active all the time:

```bash
./service.sh install      # systemd service, survives reboot
./service.sh uninstall    # removes the service and /etc/legion-control
```

## Files it touches

* `/etc/legion-control/config.json` (remembers the chosen charge mode)
* only with `service.sh install`: `/etc/systemd/system/legion-charge-limiter.service`, `/usr/local/libexec/legion-control/`

## Security

The backend runs as root but listens only on 127.0.0.1, checks the Host header, and requires a
random per-session token (kept in the URL fragment, never on the command line). It can only write
to the sysfs files it discovered at startup.
