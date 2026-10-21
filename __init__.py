"""Lazy service exports keep standalone source readers equipment-independent."""
from importlib import import_module

_MODULES = {
    'ESP32Service': 'esp32_service',
    'AuditService': 'audit_service',
    'SystemLogService': 'audit_service',
    'AlertService': 'alert_service',
    'WaterLevelService': 'water_level_service',
    'NDRRMCService': 'ndrrmc_service',
}


def __getattr__(name):
    if name not in _MODULES:
        raise AttributeError(name)
    value = getattr(import_module('.' + _MODULES[name], __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "ESP32Service",
    "AuditService",
    "SystemLogService",
    "AlertService",
    "WaterLevelService",
    "NDRRMCService",
]
