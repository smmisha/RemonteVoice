"""Bounded H.264 encoder with one owned process and two queued access units.

The caller supplies an owned encoder command and the live consent predicate.
Frames never reach disk. Slow consumers apply bounded pipe/queue backpressure;
arbitrary dependent P frames are never dropped from the encoded stream.
"""
import asyncio
import queue
import re
import subprocess
import threading
import shutil
import os
import ctypes
import time

MAX_ACCESS_UNIT = 512 * 1024
_AUD = re.compile(b'\x00\x00\x00?\x01\x09')
_START = re.compile(b'\x00\x00\x00?\x01')
_END = object()
_OWNER = threading.Lock()
VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS = 1280, 800, 30


def encoder_available():
    """Capability detection never installs software or starts a capture/process."""
    return shutil.which('ffmpeg') is not None


def create_encoder(authorized):
    """Encode our consent-checked SRCCOPY capture, without FFmpeg's gdigrab."""
    executable = shutil.which('ffmpeg')
    if executable is None:
        raise StreamError('encoder_unavailable')
    command = [executable, '-hide_banner', '-loglevel', 'error', '-nostdin',
               '-filter_threads', '1', '-f', 'rawvideo', '-pixel_format', 'rgb24',
               '-framerate', str(VIDEO_FPS), '-video_size', f'{VIDEO_WIDTH}x{VIDEO_HEIGHT}',
               '-i', 'pipe:0', '-an',
               '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
               '-threads', '2', '-pix_fmt', 'yuv420p', '-profile:v', 'baseline',
               '-b:v', '4M', '-maxrate', '4M', '-bufsize', '1M', '-g', '30',
               '-bf', '0', '-x264-params', 'aud=1:repeat-headers=1', '-f', 'h264', 'pipe:1']
    return Encoder(command, authorized, capture=lambda: _capture_rgb(authorized))


def _capture_rgb(authorized):
    from PIL import Image
    from .screen import _NATIVE_CAPTURE_LOCK
    from .windows_screen import capture_primary_image
    if not _NATIVE_CAPTURE_LOCK.acquire(blocking=False):
        raise StreamError('busy')
    try:
        if not authorized():
            raise StreamError('unavailable')
        with capture_primary_image((VIDEO_WIDTH, VIDEO_HEIGHT), fast=True) as image:
            if not authorized():
                raise StreamError('unavailable')
            if image.mode != 'RGB' or image.width > VIDEO_WIDTH or image.height > VIDEO_HEIGHT:
                raise StreamError('capture_failed')
            with Image.new('RGB', (VIDEO_WIDTH, VIDEO_HEIGHT)) as canvas:
                canvas.paste(image, ((VIDEO_WIDTH-image.width)//2, (VIDEO_HEIGHT-image.height)//2))
                return canvas.tobytes()
    finally:
        _NATIVE_CAPTURE_LOCK.release()


class StreamError(RuntimeError):
    pass


class _WindowsJob:
    """Close the owned encoder automatically if the Host process exits."""
    def __init__(self, process):
        class Basic(ctypes.Structure):
            _fields_ = [('per_process', ctypes.c_int64), ('per_job', ctypes.c_int64),
                        ('flags', ctypes.c_uint32), ('minimum_working', ctypes.c_size_t),
                        ('maximum_working', ctypes.c_size_t), ('active', ctypes.c_uint32),
                        ('affinity', ctypes.c_size_t), ('priority', ctypes.c_uint32),
                        ('scheduling', ctypes.c_uint32)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self.api.CreateJobObjectW.restype = ctypes.c_void_p
        self.api.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.api.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.api.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise StreamError('encoder_unavailable')
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not self.api.AssignProcessToJobObject(self.handle, int(process._handle))):
            self.close()
            raise StreamError('encoder_unavailable')

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


class AccessUnits:
    def __init__(self):
        self.buffer = bytearray()
        self.reference = False

    def _frame(self, frame):
        if not 4 < len(frame) <= MAX_ACCESS_UNIT or _AUD.match(frame) is None:
            raise StreamError('invalid_stream')
        types = [frame[m.end()] & 31 for m in _START.finditer(frame) if m.end() < len(frame)]
        if not any(t in (1, 5) for t in types):
            raise StreamError('invalid_stream')
        if 5 in types:
            self.reference = True
        if not self.reference:
            raise StreamError('missing_keyframe')
        return frame

    def feed(self, chunk):
        if len(chunk) > 64 * 1024:
            raise StreamError('invalid_stream')
        self.buffer.extend(chunk)
        starts = [m.start() for m in _AUD.finditer(self.buffer)]
        if starts and starts[0] != 0:
            raise StreamError('invalid_stream')
        frames = [self._frame(bytes(self.buffer[a:b])) for a, b in zip(starts, starts[1:])]
        if len(starts) > 1:
            del self.buffer[:starts[-1]]
        if len(self.buffer) > MAX_ACCESS_UNIT:
            raise StreamError('frame_too_large')
        return frames

    def finish(self):
        if not self.buffer:
            return []
        frame = self._frame(bytes(self.buffer))
        self.buffer.clear()
        return [frame]


class Encoder:
    def __init__(self, command, authorized, capture=None):
        self.command = list(command)
        self.authorized = authorized
        self.capture = capture
        self.pending = queue.Queue(maxsize=2)
        self.stopped = threading.Event()
        self.process = None
        self.reader = self.errors = self.producer = None
        self.held = False
        self.job = None
        self.lifecycle = threading.RLock()

    def start(self):
        with self.lifecycle:
            self._start_locked()

    def _start_locked(self):
        if self.stopped.is_set() or self.process is not None or not self.authorized():
            raise StreamError('unavailable')
        if not _OWNER.acquire(blocking=False):
            raise StreamError('busy')
        self.held = True
        try:
            self.process = subprocess.Popen(self.command, stdin=(subprocess.PIPE if self.capture else subprocess.DEVNULL),
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if os.name == 'nt':
                self.job = _WindowsJob(self.process)
            self.reader = threading.Thread(target=self._read, daemon=True, name='remotevoice-h264')
            self.errors = threading.Thread(target=self._drain_errors, daemon=True, name='remotevoice-h264-errors')
            self.errors.start()
            self.reader.start()
            if self.capture is not None:
                self.producer = threading.Thread(target=self._capture, daemon=True, name='remotevoice-video-capture')
                self.producer.start()
        except Exception:
            self.stop()
            raise StreamError('encoder_unavailable') from None

    def _put(self, value):
        while not self.stopped.is_set() and self.authorized():
            try:
                self.pending.put(value, timeout=.1)
                return True
            except queue.Full:
                pass
        return False

    def _read(self):
        parser = AccessUnits()
        try:
            while not self.stopped.is_set() and self.authorized():
                # Buffered read(n) waits to fill n bytes. A static desktop may
                # emit tiny P frames, turning that into seconds of hidden lag.
                # read1 returns the bytes currently available from one pipe read.
                chunk = self.process.stdout.read1(8192)
                if not chunk:
                    for frame in parser.finish():
                        if not self._put(frame):
                            return
                    self._put(_END)
                    return
                for frame in parser.feed(chunk):
                    if not self._put(frame):
                        return
        except Exception:
            self._put(StreamError('invalid_stream'))

    def _drain_errors(self):
        # Drain without retaining stderr, which is not protocol data.
        while self.process.stderr.read(4096):
            pass

    def _capture(self):
        try:
            next_capture_at = time.monotonic()
            while not self.stopped.is_set() and self.authorized():
                started = time.monotonic()
                frame = self.capture()
                if not self.authorized() or self.stopped.is_set():
                    return
                if not isinstance(frame, bytes) or len(frame) != VIDEO_WIDTH * VIDEO_HEIGHT * 3:
                    raise StreamError('capture_failed')
                # Only one raw frame is held here; a blocked encoder applies
                # pipe backpressure instead of accumulating desktop images.
                if self.process.stdin.write(frame) != len(frame):
                    raise StreamError('capture_failed')
                self.process.stdin.flush()
                frame = None
                # Keep an absolute cadence: Windows wait granularity must not
                # add its oversleep to every following frame's interval.
                next_capture_at = max(next_capture_at + 1 / VIDEO_FPS, started)
                self.stopped.wait(max(0, next_capture_at - time.monotonic()))
        except Exception:
            self._put(StreamError('capture_failed'))
        finally:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def _get(self):
        while not self.stopped.is_set() and self.authorized():
            try:
                return self.pending.get(timeout=.1)
            except queue.Empty:
                pass
        raise StreamError('unavailable')

    async def next_frame(self):
        if self.process is None:
            raise StreamError('unavailable')
        value = await asyncio.wait_for(asyncio.to_thread(self._get), timeout=6)
        if not self.authorized():
            raise StreamError('unavailable')
        if value is _END:
            raise StreamError('encoder_ended')
        if isinstance(value, StreamError):
            raise value
        return value

    def stop(self):
        with self.lifecycle:
            self._stop_locked()

    def _stop_locked(self):
        self.stopped.set()
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            for thread in (self.reader, self.errors, self.producer):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(timeout=1)
            if any(t is not None and t.is_alive() for t in (self.reader, self.errors, self.producer)):
                raise StreamError('cleanup_unconfirmed')
            self.process.stdout.close()
            self.process.stderr.close()
        if self.job is not None:
            self.job.close()
            self.job = None
        while True:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                break
        if self.held:
            _OWNER.release()
            self.held = False
