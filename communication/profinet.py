import ctypes
import struct
import time
import logging
from typing import Optional
from .base import RobotCommunicator

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 0.25
POLL_INTERVAL = 0.008

I_READY = 0
I_ACK = 1
I_OK = 2
I_NOK = 3
I_ERROR = 4
I_LIFE = 10
I_WARN = 11

O_ACQ = 0
O_START = 1
O_RESET = 2
O_TYPE_0 = 3
O_TYPE_6 = 9


class CifXCommunicator(RobotCommunicator):
    def __init__(self, device_name: str = "cifX0", channel: int = 0):
        self.device_name = device_name.encode('ascii')
        self.channel_num = channel
        self.cifx = None
        self.handle = None
        self.channel = None
        self.life_bit = 0
        self.last_heartbeat = time.time()
        self._connected = False

    def connect(self) -> bool:
        try:
            self.cifx = ctypes.WinDLL("cifx32dll.dll")
        except OSError:
            try:
                self.cifx = ctypes.WinDLL("cifx32dll64.dll")
            except OSError as e:
                logger.error(f"cifX DLL not found: {e}")
                return False

        result = self.cifx.cifXOpen(self.device_name, None, None, None)
        if result != 0:
            logger.error(f"cifXOpen failed: {result}")
            return False

        self.handle = result
        self.channel = self.cifx.cifXChannelOpen(self.handle, self.channel_num, 0)
        if not self.channel:
            logger.error("cifXChannelOpen failed")
            self.cifx.cifXClose(self.handle)
            return False

        self._connected = True
        logger.info(f"cifX connected: {self.device_name.decode()}, channel {self.channel_num}")
        return True

    def disconnect(self) -> None:
        if self.channel and self.cifx:
            self.cifx.cifXChannelClose(self.channel)
        if self.handle and self.cifx:
            self.cifx.cifXClose(self.handle)
        self._connected = False
        logger.info("cifX disconnected")

    def is_connected(self) -> bool:
        return self._connected

    def _swap_bytes(self, val: int) -> int:
        return ((val & 0xFF) << 8) | ((val >> 8) & 0xFF)

    def read_inputs(self) -> int:
        if not self.channel:
            return 0
        buf = (ctypes.c_char * 2)()
        result = self.cifx.cifXRead(self.channel, buf, 2, ctypes.byref(ctypes.c_int(0)))
        if result != 0:
            logger.error(f"cifXRead failed: {result}")
            return 0
        raw = struct.unpack('<H', buf)[0]
        return self._swap_bytes(raw)

    def write_outputs(self, value: int) -> bool:
        if not self.channel:
            return False
        swapped = self._swap_bytes(value)
        buf = struct.pack('<H', swapped)
        result = self.cifx.cifXWrite(self.channel, buf, 2, ctypes.byref(ctypes.c_int(0)))
        if result != 0:
            logger.error(f"cifXWrite failed: {result}")
            return False
        return True

    def update_heartbeat(self) -> None:
        now = time.time()
        if now - self.last_heartbeat >= HEARTBEAT_INTERVAL:
            self.life_bit = 1 - self.life_bit
            self.last_heartbeat = now

    def wait_for_trigger(self, timeout: float = 30.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            self.update_heartbeat()
            inputs = self.read_inputs()
            o_acq = (inputs >> O_ACQ) & 1
            o_start = (inputs >> O_START) & 1
            if o_acq and o_start:
                outputs = (1 << I_READY) | (1 << I_ACK) | (self.life_bit << I_LIFE)
                self.write_outputs(outputs)
                return True
            outputs = (1 << I_READY) | (self.life_bit << I_LIFE)
            self.write_outputs(outputs)
            time.sleep(POLL_INTERVAL)
        return False

    def send_result(self, ok: bool, width_mm: float = 0.0, error_code: int = 0) -> bool:
        outputs = (1 << I_READY) | (self.life_bit << I_LIFE)
        if ok:
            outputs |= (1 << I_OK)
        else:
            outputs |= (1 << I_NOK)
            outputs |= (error_code << 4) & 0xF0
        return self.write_outputs(outputs)

    def wait_for_reset(self, timeout: float = 30.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            self.update_heartbeat()
            inputs = self.read_inputs()
            o_reset = (inputs >> O_RESET) & 1
            if o_reset:
                outputs = (1 << I_READY) | (self.life_bit << I_LIFE)
                self.write_outputs(outputs)
                return True
            outputs = (1 << I_READY) | (self.life_bit << I_LIFE)
            self.write_outputs(outputs)
            time.sleep(POLL_INTERVAL)
        return False

    def read_di(self, di_number):
        return False

    def wait_for_di(self, di_number, timeout=None):
        return False

    def wait_for_di_any(self, di_numbers, timeout=None):
        return None

    def reset_outputs(self):
        outputs = (1 << I_READY) | (self.life_bit << I_LIFE)
        return self.write_outputs(outputs)

    def set_conveyor(self, on: bool):
        pass

    def send_do(self, do_number, value: bool):
        pass


def precise_sleep(seconds: float) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass


def set_timer_resolution(ms: int = 1) -> None:
    ctypes.windll.winmm.timeBeginPeriod(ms)


def restore_timer_resolution(ms: int = 1) -> None:
    ctypes.windll.winmm.timeEndPeriod(ms)