"""Fixed-schema scalar wake trace. Never accepts audio, labels, IDs or exceptions."""
import threading


class WakeDiagnostics:
    LIMIT = 2_147_483_647
    FIELDS = (
        'start_calls', 'pause_calls', 'resume_calls', 'stop_calls', 'failures',
        'feed_calls', 'feed_samples', 'queue_enqueued', 'queue_dropped', 'queue_consumed',
        'queue_stale_dropped', 'queue_stop_dropped', 'engine_calls', 'engine_hits',
        'engine_no_hit_chunks', 'engine_errors', 'fire_errors', 'feed_errors',
        'callback_dispatched', 'callback_errors', 'cooldown_suppressed', 'event_emitted',
        'event_errors', 'event_attempts', 'event_refused', 'event_unknown',
    )
    STATES = ('idle', 'starting', 'listening', 'paused', 'stopped', 'failed')

    def __init__(self):
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(self.FIELDS, 0)
        self._state = 'idle'

    def bump(self, field, amount=1):
        if field not in self.FIELDS or type(amount) is not int or amount < 0:
            raise ValueError('invalid wake diagnostic counter')
        with self._lock:
            self._counts[field] = min(self.LIMIT, self._counts[field] + amount)

    def state(self, value):
        if value not in self.STATES:
            raise ValueError('invalid wake diagnostic state')
        with self._lock:
            self._state = value

    def snapshot(self):
        with self._lock:
            return {'state': self._state, **self._counts}
