import imp
import os
import logging

from collections import defaultdict

loaded = False
plugin_dir = os.path.join(os.path.dirname(__file__), 'plugins')
log = logging.getLogger('koschei.plugin')

listeners = defaultdict(list)


def load_plugins(only=None):
    global loaded
    if not loaded:
        for path in os.listdir(plugin_dir):
            if path.endswith('.py'):
                name = path[:-3]
                if (only is None or name in only) and not name.startswith('_'):
                    descriptor = imp.find_module(name, [plugin_dir])
                    log.info('Loading {} plugin'.format(name))
                    imp.load_module(name, *descriptor)
        loaded = True

def listen_event(name):
    def decorator(fn):
        if fn not in listeners[name]:
            listeners[name].append(fn)
        return fn
    return decorator

def dispatch_event(name, *args, **kwargs):
    for listener in listeners[name]:
        listener(*args, **kwargs)
