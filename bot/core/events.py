from collections import defaultdict


class EventBus:

    def __init__(self):
        self._listeners = defaultdict(list)

    def subscribe(self, event_name, callback):
        self._listeners[event_name].append(callback)

    def publish(self, event_name, data=None):

        for callback in self._listeners[event_name]:
            callback(data)