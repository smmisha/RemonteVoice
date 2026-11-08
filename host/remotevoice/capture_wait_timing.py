"""Opt-in scalar observation of existing capture waits; no audio/device access."""

import inspect
import sys
import threading
import time
import types


class CaptureWaitTiming:
    """Observe one recorder on its capture owner without replacing its methods.

    CPython 3.14 monitoring semantics were verified for the actual recorder
    bodies. Older/other runtimes and unsupported recorder shapes soft-skip.
    The caller controls opt-in gating, bounded per-read storage and final I/O.
    """

    def __init__(self, recorder, *, clock=time.perf_counter_ns):
        self.recorder = recorder
        self.clock = clock
        self.owner = None
        self.getter = None
        self.chunk = None
        self.tool = None
        self.name = 'remotevoice-capture-wait-' + str(id(self))
        self.pending = {}
        self.stats = dict(getter_calls=0, getter_ns=0, getter_errors=0,
                          sleep_calls=0, sleep_ns=0, requested_ns=0,
                          sleep_errors=0, observer_errors=0)
        self.incomplete_spans = 0
        self.status = 'new'
        self.failure_stage = None
        self.error_type = None
        self._entered = False
        self._interrupted = False
        self._cleanup_attempted = False
        self._events = ()
        self._monitor = getattr(sys, 'monitoring', None)
        try:
            self.owner = threading.current_thread()
        except Exception as error:
            self._fail('owner_check', error)
            self.status = 'unsupported'

    def _fail(self, stage, error):
        if self.failure_stage is None:
            self.failure_stage = stage
            self.error_type = type(error).__name__
            self.stats['observer_errors'] += 1

    def _unsupported(self, stage):
        self.status = 'unsupported'
        if self.failure_stage is None:
            self.failure_stage = stage
        return self

    def snapshot(self):
        """Flat scalar totals; incomplete/failed observations cannot be usable."""
        if self.status == 'active' and self.tool is not None:
            try:
                if self._monitor.get_tool(self.tool) != self.name:
                    self._fail('tool_ownership_lost', RuntimeError())
            except Exception as error:
                self._fail('tool_ownership_check', error)
            except BaseException:
                self._interrupted = True
                raise
        return dict(self.stats, pending_count=len(self.pending),
                    usable=(self.status in ('active', 'complete')
                            and self.failure_stage is None
                            and not self._interrupted and not self.incomplete_spans
                            and not self.pending and not self.stats['observer_errors']))

    def metadata(self):
        """Scalar lifecycle facts retained across repeated cleanup attempts."""
        return dict(status=self.status, failure_stage=self.failure_stage,
                    error_type=self.error_type, incomplete_spans=self.incomplete_spans,
                    cleanup_pending=self._cleanup_attempted and self.tool is not None)

    def _select_codes(self):
        # Static inspection must not evaluate deviceperiod or query native state.
        descriptor = inspect.getattr_static(type(self.recorder), 'deviceperiod')
        if not isinstance(descriptor, property) or not isinstance(descriptor.fget, types.FunctionType):
            return False
        chunk = inspect.getattr_static(self.recorder, '_record_chunk')
        if isinstance(chunk, types.MethodType):
            if chunk.__self__ is not self.recorder:
                return False
            chunk = chunk.__func__
        if not isinstance(chunk, types.FunctionType):
            return False
        codes = descriptor.fget.__code__, chunk.__code__
        if any(code.co_argcount < 1 or code.co_varnames[0] != 'self' for code in codes):
            return False
        self.getter, self.chunk = codes
        return True

    def _callback(self, stage, code_target, handler, *, sleep_only=False):
        # Capture the observed frame before entering any helper/handler. Never
        # infer recorder identity from a handler's changing stack depth.
        def callback(code, offset, *args):
            if self.failure_stage is not None or self._interrupted or code is not code_target:
                return
            if sleep_only and (not args or args[0] is not time.sleep):
                return
            try:
                if threading.current_thread() is not self.owner:
                    return
                frame = sys._getframe(1)
                if frame.f_code is not code or frame.f_locals.get('self') is not self.recorder:
                    return
                handler(*args)
            except Exception as error:
                self._fail(stage, error)
            except BaseException:
                self._interrupted = True
                raise
        return callback

    def __enter__(self):
        if self._entered:
            raise RuntimeError('CaptureWaitTiming contexts are single-use')
        self._entered = True
        if self.status != 'new':
            return self
        try:
            supported = (sys.implementation.name == 'cpython' and sys.version_info[:2] >= (3, 14))
        except Exception as error:
            self._fail('runtime_unsupported', error)
            return self._unsupported('runtime_unsupported')
        if not supported:
            return self._unsupported('runtime_unsupported')
        m = self._monitor
        if m is None:
            return self._unsupported('monitoring_unavailable')
        try:
            if threading.current_thread() is not self.owner:
                return self._unsupported('wrong_owner_thread')
        except Exception as error:
            self._fail('owner_check', error)
            return self._unsupported('owner_check')
        try:
            if not self._select_codes():
                return self._unsupported('recorder_unsupported')
        except Exception as error:
            self._fail('recorder_unsupported', error)
            return self._unsupported('recorder_unsupported')
        try:
            ev = m.events
            events = (ev.PY_START, ev.PY_RETURN, ev.PY_UNWIND, ev.CALL, ev.C_RETURN, ev.C_RAISE)
            self._events = events
            if not all(callable(getattr(m, name, None)) for name in
                       ('use_tool_id', 'get_tool', 'register_callback', 'set_local_events', 'set_events', 'free_tool_id')):
                return self._unsupported('monitoring_capabilities')
        except Exception as error:
            self._fail('monitoring_capabilities', error)
            return self._unsupported('monitoring_capabilities')
        for candidate in (3, 4):
            try:
                m.use_tool_id(candidate, self.name)
            except ValueError:
                continue
            except Exception as error:
                # Acquisition may have succeeded before raising. Reconcile its
                # actual ownership before cleanup; never free a foreign tool.
                self._fail('tool_acquire', error)
                self.tool = candidate
                self.close()
                if self.tool is None:
                    self.status = 'unsupported'
                return self
            except BaseException:
                self._interrupted = True
                self.tool = candidate
                try:
                    self.close()
                except BaseException:
                    pass
                raise
            self.tool = candidate
            break
        if self.tool is None:
            return self._unsupported('tool_ids_busy')
        try:
            for event, stage, code, handler, sleep_only in (
                (events[0], 'getter_start', self.getter, self._start, False),
                (events[1], 'getter_return', self.getter, self._finish, False),
                (events[2], 'getter_unwind', self.getter, self._unwind, False),
                (events[3], 'sleep_call', self.chunk, self._call, True),
                (events[4], 'sleep_return', self.chunk, self._returned, True),
                (events[5], 'sleep_raise', self.chunk, self._raised, True),
            ):
                m.register_callback(self.tool, event, self._callback(stage, code, handler, sleep_only=sleep_only))
            m.set_local_events(self.tool, self.getter, ev.PY_START | ev.PY_RETURN)
            m.set_local_events(self.tool, self.chunk, ev.CALL)
            m.set_events(self.tool, ev.PY_UNWIND)
            self.status = 'active'
        except Exception as error:
            self._fail('setup', error)
            self.close()
            if self.tool is None:
                self.status = 'unsupported'
        except BaseException:
            self._interrupted = True
            try:
                self.close()
            except BaseException:
                pass
            raise
        return self

    def _tick(self):
        tick = self.clock()
        if type(tick) is not int or tick < 0:
            raise ValueError('Invalid observation clock')
        return tick

    def _begin(self, kind):
        if kind in self.pending:
            raise RuntimeError('Overlapping observation span')
        self.pending[kind] = self._tick()

    def _start(self):
        self._begin('getter')

    def _finish(self, result):
        self._finish_span('getter', False)

    def _unwind(self, error):
        self._finish_span('getter', True)

    def _finish_span(self, kind, failed):
        if kind in self.pending:
            elapsed = self._tick() - self.pending[kind]
            if elapsed < 0:
                raise ValueError('Observation clock moved backwards')
            del self.pending[kind]
            self.stats[kind + '_calls'] += 1
            self.stats[kind + '_errors'] += int(failed)
            self.stats[kind + '_ns'] += elapsed

    def _call(self, callee, arg0):
        self._begin('sleep')
        self.stats['requested_ns'] += max(0, round(arg0 * 1e9))

    def _returned(self, callee, arg0):
        self._finish_span('sleep', False)

    def _raised(self, callee, arg0):
        self._finish_span('sleep', True)

    def close(self):
        if self.tool is not None:
            self._cleanup_attempted = True
            try:
                m = self._monitor
                # free_tool_id does not prove local masks/callbacks were reset.
                # Disable and unregister our hooks before releasing ownership;
                # an incomplete reset retains the slot for an explicit retry.
                actions = [
                    lambda: m.set_local_events(self.tool, self.getter, 0),
                    lambda: m.set_local_events(self.tool, self.chunk, 0),
                    lambda: m.set_events(self.tool, 0),
                ]
                actions.extend(lambda event=event: m.register_callback(self.tool, event, None)
                               for event in self._events)
                actions.append(lambda: m.free_tool_id(self.tool))
                for action in actions:
                    if m.get_tool(self.tool) != self.name:
                        self._fail('tool_ownership_lost', RuntimeError())
                        break
                    action()
            except Exception as error:
                # Retain the unresolved id for explicit cleanup retry. Do not
                # mask target output/errors or clear any foreign registration.
                self._fail('cleanup', error)
                self.status = 'cleanup_failed'
                return
            except BaseException:
                self._interrupted = True
                self.status = 'interrupted'
                raise
            self.tool = None
        self.incomplete_spans += len(self.pending)
        self.pending.clear()
        if self._interrupted:
            self.status = 'interrupted'
        elif self.status not in ('new', 'unsupported'):
            self.status = 'failed' if self.failure_stage else 'complete'

    def __exit__(self, kind, error, traceback):
        if kind is not None and not issubclass(kind, Exception):
            self._interrupted = True
        try:
            self.close()
        except BaseException:
            if kind is None:
                raise
            # An existing target exception, including user cancellation, has
            # precedence over a different interruption raised during cleanup.
        return False
