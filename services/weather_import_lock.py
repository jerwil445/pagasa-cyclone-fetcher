"""Single-process import serialization shared by local and standalone readers."""
from functools import wraps
from threading import RLock

LOCK = RLock()


def serialized_weather_import(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with LOCK:
            return function(*args, **kwargs)
    return wrapped
