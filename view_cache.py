"""Memoization limited to one read-only view and one database connection."""
from contextvars import ContextVar
from functools import wraps

_current = ContextVar('view_read_cache', default=None)


def clone(value):
    if hasattr(value, 'keys'):
        return {key: clone(value[key]) for key in value.keys()}
    if isinstance(value, list):
        return [clone(item) for item in value]
    if isinstance(value, tuple):
        return tuple(clone(item) for item in value)
    return value


def scope(function):
    @wraps(function)
    def wrapped(c, *args, **kwargs):
        current = _current.get()
        if current is not None and current[0] is c.base:
            return function(c, *args, **kwargs)
        token = _current.set((c.base, {}))
        try:
            return function(c, *args, **kwargs)
        finally:
            _current.reset(token)
    return wrapped


def cached(key=None):
    def decorate(function):
        @wraps(function)
        def wrapped(c, *args, **kwargs):
            current = _current.get()
            if current is None or current[0] is not c.base:
                return function(c, *args, **kwargs)
            identity = key(c, *args, **kwargs) if key else (args, tuple(sorted(kwargs.items())))
            cache_key = (function.__module__, function.__name__, c.realm, c.boss, identity)
            cache = current[1]
            if cache_key not in cache:
                cache[cache_key] = clone(function(c, *args, **kwargs))
            return clone(cache[cache_key])
        return wrapped
    return decorate
