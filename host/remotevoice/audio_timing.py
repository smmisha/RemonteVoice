"""Bounded scalar timings for a single system-audio producer; never accepts PCM."""
from collections import deque
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

LOGGER = logging.getLogger('remotevoice.audio')


class SystemAudioTiming:
    """Capture thread and its awaited send coroutine mark one packet at a time."""
    _TRACE_LIMIT = 6000  # Last minute at 100 packets/s; opt-in scalar rows only.
    _TRACE_MAX_LIMIT = 90000  # Explicit diagnostic history, about 15 minutes.
    _TCP_LIMIT = 600
    # TCP is sampled at most once per 100 ms, i.e. about once per 10 packets at 100 packets/s.
    # An explicit long packet history keeps TCP rows for the same span instead of the last minute.
    _TCP_PACKETS_PER_SAMPLE = 10
    _CAPTURE_SLOW_LIMIT = 128
    _PHASES = ('read_start', 'read_end', 'send_enter', 'send_lock', 'send_done')
    _CAPTURE_NATIVE_COLUMNS = ('frame', 'thread_cpu_ns', 'available_calls', 'available_ns',
        'buffer_calls', 'buffer_ns', 'release_calls', 'release_ns', 'zero_polls', 'unattributed_ns', 'capture_wall_ns')
    _CAPTURE_WAIT_COLUMNS = ('frame', 'getter_calls', 'getter_ns', 'getter_errors',
        'sleep_calls', 'sleep_ns', 'requested_ns', 'sleep_errors', 'observer_errors',
        'pending_count', 'unattributed_ns', 'capture_wall_ns', 'complete')
    _CAPTURE_WAIT_COUNTERS = _CAPTURE_WAIT_COLUMNS[1:9]

    def __init__(self, clock=time.monotonic_ns, *, trace_directory=None, tcp_observer=None,
                 trace_packet_limit=None):
        self.session_id = uuid.uuid4().hex
        self.clock = clock
        self.frames = 0
        self.previous_sent = None
        self.last_log = None
        self.points = {}
        self._trace_directory = Path(trace_directory) if trace_directory else None
        self._trace_limit = self._TRACE_LIMIT
        self._trace_retention_selection = 'default'
        if self._trace_directory is not None and trace_packet_limit is not None:
            self._trace_retention_selection = 'invalid_default'
            limit = trace_packet_limit
            if type(limit) is str:
                limit = limit.strip()
                if limit.isascii() and limit.isdecimal():
                    digits = limit.lstrip('0') or '0'
                    if len(digits) <= 5:
                        limit = int(digits)
            if type(limit) is int and 1 <= limit <= self._TRACE_MAX_LIMIT:
                self._trace_limit = limit
                self._trace_retention_selection = 'explicit'
        self._trace = deque(maxlen=self._trace_limit) if self._trace_directory is not None else None
        self._trace_count = 0
        self._trace_flushed = False
        self._tcp_observer = tcp_observer
        self._tcp_limit = self._TCP_LIMIT
        if self._trace_retention_selection == 'explicit':
            self._tcp_limit = max(self._TCP_LIMIT, -(-self._trace_limit // self._TCP_PACKETS_PER_SAMPLE))
        self._tcp_samples = deque(maxlen=self._tcp_limit)
        self._tcp_count = 0
        self._tcp_last = None
        self.thread_clock = time.thread_time_ns
        self._capture_native_rows = None
        self._capture_native_count = 0
        self._capture_slow_rows = None
        self._capture_slow_count = 0
        self._capture_native_data = None
        self._capture_cpu_start = None
        self._capture_wait_observer = None
        self._capture_wait_rows = None
        self._capture_wait_count = 0
        self._capture_wait_slow_rows = None
        self._capture_wait_slow_count = 0
        self._capture_wait_start = None
        self._capture_wait_data = None
        self._capture_wait_failure = None

    def observe_capture(self, recorder):
        """Time existing calls on their capture owner; never query native state.

        Disabled unless scalar tracing was explicitly requested. Native results,
        arguments and exceptions pass through unchanged. No PCM is inspected.
        A separate opt-in probe separates device-period queries and sleep calls;
        their durations include scheduling delay and do not establish its cause.
        """
        if self._trace is None or self._trace_flushed:
            return recorder
        if getattr(recorder, '_remotevoice_capture_timing', False):
            return recorder
        if self._capture_wait_observer is None:
            self._capture_wait_rows = deque(maxlen=self._trace_limit)
            self._capture_wait_slow_rows = deque(maxlen=self._CAPTURE_SLOW_LIMIT)
            try:
                from .capture_wait_timing import CaptureWaitTiming
                self._capture_wait_observer = CaptureWaitTiming(recorder, clock=self.clock)
                self._capture_wait_observer.__enter__()
            except Exception as error:
                self._wait_failed('setup', error)
        names = ('_capture_available_frames', '_capture_buffer', '_capture_release')
        methods = [getattr(recorder, name, None) for name in names]
        if not all(callable(method) for method in methods):
            return recorder

        def observed(original, calls_index, time_index, count_zero=False):
            def call(*args, **kwargs):
                row = self._capture_native_data
                if row is None:
                    return original(*args, **kwargs)
                try:
                    started = self.clock()
                except Exception:
                    return original(*args, **kwargs)
                result = None
                try:
                    result = original(*args, **kwargs)
                    return result
                finally:
                    try:
                        row[calls_index] += 1
                        row[time_index] += max(0, self.clock()-started)
                        if count_zero and type(result) is int and result == 0:
                            row[8] += 1
                    except Exception:
                        pass  # Observation cannot replace a native result/error.
            return call

        for index, (name, method) in enumerate(zip(names, methods)):
            setattr(recorder, name, observed(method, 2+index*2, 3+index*2, index == 0))
        recorder._remotevoice_capture_timing = True
        if self._capture_native_rows is None:
            self._capture_native_rows = deque(maxlen=self._trace_limit)
            self._capture_slow_rows = deque(maxlen=self._CAPTURE_SLOW_LIMIT)
        return recorder

    def _wait_failed(self, stage, error):
        if self._capture_wait_failure is None:
            self._capture_wait_failure = dict(status='failed', failure_stage=stage,
                error_type=type(error).__name__, incomplete_spans=0, cleanup_pending=False)

    def _wait_snapshot(self):
        if self._capture_wait_observer is None or self._capture_wait_failure is not None:
            return None
        try:
            return self._capture_wait_observer.snapshot()
        except Exception as error:
            self._wait_failed('snapshot', error)
            return None

    def close_capture_observation(self):
        """Release hooks on the capture owner, preserving a propagating error."""
        if self._capture_wait_observer is not None:
            original_error = sys.exc_info()
            for _ in range(2):
                try:
                    self._capture_wait_observer.__exit__(*original_error)
                except Exception as error:
                    self._wait_failed('cleanup', error)
                try:
                    if not self._capture_wait_observer.metadata()['cleanup_pending']:
                        break
                except Exception as error:
                    self._wait_failed('metadata', error)
                    break

    def _wait_metadata(self):
        metadata = {}
        if self._capture_wait_observer is not None:
            try:
                metadata = self._capture_wait_observer.metadata()
            except Exception as error:
                self._wait_failed('metadata', error)
        if self._capture_wait_failure is not None:
            # Retain actual cleanup/incomplete facts even after a prior failure.
            metadata = {**metadata, **self._capture_wait_failure,
                'cleanup_pending': metadata.get('cleanup_pending', False),
                'incomplete_spans': metadata.get('incomplete_spans', 0)}
        return metadata

    def mark(self, phase):
        if phase not in {'read_start', 'read_end', 'send_enter', 'send_lock', 'send_done'}:
            raise ValueError('Unknown audio timing phase')
        now = self.clock()
        self.points[phase] = now
        if self._capture_wait_rows is not None:
            if phase == 'read_start':
                self._capture_wait_start = self._wait_snapshot()
                self._capture_wait_data = None
            elif phase == 'read_end':
                before, after = self._capture_wait_start, self._wait_snapshot()
                self._capture_wait_start = None
                wall = max(0, now-self.points.get('read_start', now))
                row = [0] * len(self._CAPTURE_WAIT_COLUMNS)
                if before is not None and after is not None:
                    for index, key in enumerate(self._CAPTURE_WAIT_COUNTERS, 1):
                        row[index] = max(0, after[key]-before[key])
                    row[9] = after['pending_count']
                    row[12] = int(before['usable'] and after['usable'] and row[8] == 0)
                row[11] = wall
                self._capture_wait_data = row
        if self._capture_native_rows is not None:
            if phase == 'read_start':
                self._capture_native_data = [0] * len(self._CAPTURE_NATIVE_COLUMNS)
                try:
                    self._capture_cpu_start = self.thread_clock()
                except Exception:
                    self._capture_cpu_start = None
            elif phase == 'read_end' and self._capture_native_data is not None:
                row = self._capture_native_data
                try:
                    if self._capture_cpu_start is not None:
                        row[1] = max(0, self.thread_clock()-self._capture_cpu_start)
                except Exception:
                    pass
                wall = max(0, now-self.points.get('read_start', now))
                row[9] = max(0, wall-row[3]-row[5]-row[7])
                row[10] = wall
        if phase == 'read_end' and self._capture_wait_data is not None:
            native = self._capture_native_data
            native_ns = 0 if native is None else native[3]+native[5]+native[7]
            wait = self._capture_wait_data
            wait[10] = max(0, wait[11]-native_ns-wait[2]-wait[5])

    def finish(self):
        self.frames += 1
        points = self.points
        self.points = {}
        native = self._capture_native_data
        self._capture_native_data = None
        wait = self._capture_wait_data
        self._capture_wait_data = None
        self._capture_wait_start = None
        if set(points) != {'read_start', 'read_end', 'send_enter', 'send_lock', 'send_done'}:
            return None
        if self._trace is not None and not self._trace_flushed:
            self._trace.append([self.frames, *(points[phase] for phase in self._PHASES)])
            self._trace_count += 1
            if native is not None:
                native[0] = self.frames
                self._capture_native_rows.append(native)
                self._capture_native_count += 1
                if native[10] >= 120_000_000:
                    self._capture_slow_rows.append(native)
                    self._capture_slow_count += 1
            if wait is not None:
                wait[0] = self.frames
                self._capture_wait_rows.append(wait)
                self._capture_wait_count += 1
                if wait[11] >= 120_000_000:
                    self._capture_wait_slow_rows.append(wait)
                    self._capture_wait_slow_count += 1
            self._observe_transport(points['send_done'])
        end = points['send_done']
        gap = 0 if self.previous_sent is None else max(0, end-self.previous_sent)
        self.previous_sent = end
        result = dict(frame=self.frames, clock_ns=end,
                      capture_ms=max(0, points['read_end']-points['read_start'])/1e6,
                      dispatch_ms=max(0, points['send_enter']-points['read_end'])/1e6,
                      lock_ms=max(0, points['send_lock']-points['send_enter'])/1e6,
                      send_ms=max(0, end-points['send_lock'])/1e6, gap_ms=gap/1e6)
        # Normal capture batches can be ~100ms. Keep only outliers and bound
        # logging to one line per second even on a persistently slow device.
        if max(result['capture_ms'], result['dispatch_ms'], result['lock_ms'],
               result['send_ms'], result['gap_ms']) < 120:
            return None
        if self.last_log is not None and end-self.last_log < 1_000_000_000:
            return None
        self.last_log = end
        try:
            LOGGER.info('system_audio_timing frame=%d clock_ns=%d capture_ms=%.3f dispatch_ms=%.3f '
                        'lock_ms=%.3f send_ms=%.3f gap_ms=%.3f session=%s',
                        result['frame'], end, result['capture_ms'], result['dispatch_ms'],
                        result['lock_ms'], result['send_ms'], result['gap_ms'], self.session_id)
        except Exception:
            pass  # Observation must not close a working audio session.
        return result

    def _observe_transport(self, now):
        if self._tcp_observer is None or (self._tcp_last is not None and now-self._tcp_last < 100_000_000):
            return
        self._tcp_last = now
        started = self.clock()
        try:
            values = self._tcp_observer()
            if values is None:
                return
            from .windows_tcp_observer import TCP_FIELDS
            sample = {name: values[name] for name in TCP_FIELDS
                      if type(values.get(name)) is int and values[name] >= 0}
            self._tcp_samples.append(dict(frame=self.frames, clock_ns=started,
                probe_ns=max(0, self.clock()-started), **sample))
            self._tcp_count += 1
        except Exception:
            pass  # A diagnostic API failure cannot close a working session.

    def flush_trace(self):
        """Persist after workers stop, never on the capture/send path or by default."""
        if self._trace is None or not self._trace or self._trace_flushed:
            return None
        self._trace_flushed = True
        try:
            self._trace_directory.mkdir(parents=True, exist_ok=True)
            target = self._trace_directory / f'system-audio-{os.getpid()}-{self.session_id}.json'
            report = dict(version=1, columns=['frame', *(phase+'_ns' for phase in self._PHASES)],
                          complete_packets=self._trace_count,
                          omitted_packets=self._trace_count-len(self._trace), packets=list(self._trace),
                          session_id=self.session_id,
                          trace_retention=dict(packet_limit=self._trace_limit,
                              max_packet_limit=self._TRACE_MAX_LIMIT,
                              selection=self._trace_retention_selection,
                              history_complete=self._trace_count == len(self._trace),
                              first_retained_frame=self._trace[0][0],
                              last_retained_frame=self._trace[-1][0],
                              omitted_packets=self._trace_count-len(self._trace)))
            if self._tcp_observer is not None:
                report.update(tcp_samples=list(self._tcp_samples), tcp_omitted=self._tcp_count-len(self._tcp_samples),
                              tcp_retention=dict(sample_limit=self._tcp_limit,
                                  history_complete=self._tcp_count == len(self._tcp_samples)))
            if self._capture_native_rows is not None:
                report.update(capture_native_columns=list(self._CAPTURE_NATIVE_COLUMNS),
                    capture_native_rows=list(self._capture_native_rows),
                    capture_native_complete=self._capture_native_count,
                    capture_native_omitted=self._capture_native_count-len(self._capture_native_rows),
                    capture_native_slow_rows=list(self._capture_slow_rows),
                    capture_native_slow_complete=self._capture_slow_count,
                    capture_native_slow_omitted=self._capture_slow_count-len(self._capture_slow_rows))
            if self._capture_wait_rows is not None:
                report.update(capture_wait_columns=list(self._CAPTURE_WAIT_COLUMNS),
                    capture_wait_rows=list(self._capture_wait_rows),
                    capture_wait_complete=self._capture_wait_count,
                    capture_wait_omitted=self._capture_wait_count-len(self._capture_wait_rows),
                    capture_wait_slow_rows=list(self._capture_wait_slow_rows),
                    capture_wait_slow_complete=self._capture_wait_slow_count,
                    capture_wait_slow_omitted=self._capture_wait_slow_count-len(self._capture_wait_slow_rows),
                    capture_wait_observer=self._wait_metadata())
            with target.open('x', encoding='utf-8') as output:
                json.dump(report, output, separators=(',', ':'))
                output.write('\n')
            return target
        except OSError:
            try:
                LOGGER.warning('System audio timing trace could not be saved')
            except Exception:
                pass
            return None
        finally:
            self._trace.clear()
            self._tcp_samples.clear()
            if self._capture_native_rows is not None:
                self._capture_native_rows.clear()
                self._capture_slow_rows.clear()
            if self._capture_wait_rows is not None:
                self._capture_wait_rows.clear()
                self._capture_wait_slow_rows.clear()
