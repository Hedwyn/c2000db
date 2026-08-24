"""
Utility to generate CCXML files as expected by Texas tooling.

@date: 24.08.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DebugProbe(Enum):
    """Supported debug probes for Texas Instruments devices."""

    XDS100V1 = "xds100v1"
    XDS100V2 = "xds100v2"
    XDS100V3 = "xds100v3"
    XDS110 = "xds110"
    XDS200 = "xds200"
    XDS560V2 = "xds560v2"


@dataclass
class CCXMLConfig:
    """Configuration for CCXML file generation."""

    device: str
    probe: DebugProbe = DebugProbe.XDS100V2


@dataclass(frozen=True)
class _ProbeInfo:
    """
    CCS target-configuration data for a debug probe, as found under
    ccs_base/common/targetdb/{connections,drivers} in a CCS install.
    """

    desc: str
    connection_id: str
    drivers: tuple[str, ...]


_CCXML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<configurations XML_version="1.2" id="configurations_0">
<configuration XML_version="1.2" id="configuration_0">
{probe_instance}
        <connection XML_version="1.2" id="{probe_desc}">
        \t
        \t\t   {driver_instances}
        \t

            <platform XML_version="1.2" id="platform_0">
{device_instance}
            </platform>
        </connection>
    </configuration>
</configurations>
"""

# Driver sets are per debug-probe *family*, not per CCXML_version: CCS pairs
# each probe with the C28x driver(s) that declare a matching connectionType
# in their targetdb driver XML (e.g. tixds560c28x.xml declares both
# TIXDS560 and TIXDS200, so XDS200 reuses the XDS560 driver family).
_PROBE_INFO: dict[DebugProbe, _ProbeInfo] = {
    DebugProbe.XDS100V1: _ProbeInfo(
        desc="Texas Instruments XDS100v1 USB Debug Probe",
        connection_id="TIXDS100usb",
        drivers=("tixds100icepick_c", "tixds100c28x", "tixds100cs_dap"),
    ),
    DebugProbe.XDS100V2: _ProbeInfo(
        desc="Texas Instruments XDS100v2 USB Debug Probe",
        connection_id="TIXDS100v2",
        drivers=("tixds100v2icepick_c", "tixds100v2c28x", "tixds100v2cs_child"),
    ),
    DebugProbe.XDS100V3: _ProbeInfo(
        desc="Texas Instruments XDS100v3 USB Debug Probe",
        connection_id="TIXDS100v3_Dot7",
        # XDS100v3 registers as connectionType TIXDS100v2, so it shares the
        # XDS100v2 driver family.
        drivers=("tixds100v2icepick_c", "tixds100v2c28x", "tixds100v2cs_child"),
    ),
    DebugProbe.XDS110: _ProbeInfo(
        desc="Texas Instruments XDS110 USB Debug Probe",
        connection_id="TIXDS110",
        drivers=("tixds510icepick_c", "tixds510c28x", "tixds510cs_child"),
    ),
    DebugProbe.XDS200: _ProbeInfo(
        desc="Texas Instruments XDS2xx USB Debug Probe",
        connection_id="TIXDS2XXUSB",
        # XDS200 registers as connectionType TIXDS200, which the XDS560
        # driver family also declares, so it shares those drivers.
        drivers=("tixds560icepick_c", "tixds560c28x", "tixds560cs_child"),
    ),
    DebugProbe.XDS560V2: _ProbeInfo(
        desc="Blackhawk XDS560v2-USB System Trace Emulator",
        connection_id="BH-XDS560v2-USB",
        drivers=("tixds560icepick_c", "tixds560c28x", "tixds560cs_child"),
    ),
}


def load_ccxml_config(data: dict[str, object]) -> CCXMLConfig:
    """Load CCXML configuration from a dictionary."""
    if "device" not in data:
        raise TypeError("Missing required key 'device' in config")

    device = data["device"]
    if not isinstance(device, str):
        raise TypeError(f"'device' must be a string, got {type(device).__name__}")

    probe = data.get("probe", "xds100v2")
    if not isinstance(probe, str):
        raise TypeError("`probe` should be a string")
    try:
        probe = DebugProbe(probe.lower())
    except ValueError as exc:
        raise ValueError(
            f"Unknown probe '{probe}'. Valid options: {[p.value for p in DebugProbe]}",
        ) from exc

    return CCXMLConfig(device=device, probe=probe)


def build_ccxml(config: CCXMLConfig) -> str:
    """Generate CCXML content from configuration.

    Args:
        config: CCXML configuration with device and probe parameters.

    Returns:
        Generated CCXML XML string.
    """
    try:
        probe_info = _PROBE_INFO[config.probe]
    except KeyError as exc:
        raise TypeError(f"Unsupported probe: {config.probe}") from exc

    device = config.device
    device_lower = device.lower()
    probe_desc = probe_info.desc
    probe_id = probe_info.connection_id

    driver_instances = "\n        ".join(
        f'<instance XML_version="1.2" href="drivers/{driver}.xml" id="drivers" '
        f'xml="{driver}.xml" xmlpath="drivers"/>'
        for driver in probe_info.drivers
    )

    probe_instance = (
        f'        <instance XML_version="1.2" desc="{probe_desc}" '
        f'href="connections/{probe_id}_Connection.xml" '
        f'id="{probe_desc}" xml="{probe_id}_Connection.xml" '
        f'xmlpath="connections"/>'
    )
    device_instance = (
        f'                <instance XML_version="1.2" desc="TMS320{device}" '
        f'href="devices/{device_lower}.xml" id="TMS320{device}" '
        f'xml="{device_lower}.xml" xmlpath="devices"/>'
    )

    return _CCXML_TEMPLATE.format(
        probe_instance=probe_instance,
        probe_desc=probe_desc,
        driver_instances=driver_instances,
        device_instance=device_instance,
    )


def dump_ccxml_config(config: CCXMLConfig) -> dict[str, str]:
    """Convert CCXML configuration to a dictionary.

    Args:
        config: CCXMLConfig instance to convert.

    Returns:
        Dictionary with 'device' and 'probe' keys.
    """
    return {
        "device": config.device,
        "probe": config.probe.value,
    }
