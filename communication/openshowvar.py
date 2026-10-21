import socket
import time
import logging
from .base import RobotCommunicator

logger = logging.getLogger(__name__)


class OpenShowVarCommunicator(RobotCommunicator):
    def __init__(self, config):
        self.ip = config['ip']
        self.port = config.get('port', 7000)
        self.sock = None

        # KUKA переменные для DI/DO
        self.di_vars = {
            4: config.get('di04_var', 'DI04_START'),
            5: config.get('di05_var', 'DI05_END'),
            6: config.get('di06_var', 'DI06_CAPTURE'),
        }
        self.do_vars = {
            0: config.get('do00_var', 'DO00_CONVEYOR'),
            1: config.get('do01_var', 'DO01_REJECT'),
            2: config.get('do02_var', 'DO02_NG'),
            3: config.get('do03_var', 'DO03_OK'),
        }

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        try:
            self.sock.connect((self.ip, self.port))
            logger.info(f"OpenShowVar connected: {self.ip}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"OpenShowVar connection error: {e}")
            return False

    def _send_command(self, var_name, value=None):
        if value is not None:
            payload = f"WRITE {var_name} {value}\n"
        else:
            payload = f"READ {var_name}\n"
        self.sock.sendall(payload.encode())
        time.sleep(0.01)
        response = self.sock.recv(1024).decode().strip()
        return response

    def read_di(self, di_number):
        var_name = self.di_vars.get(di_number)
        if not var_name:
            return False
        try:
            resp = self._send_command(var_name)
            return "TRUE" in resp.upper()
        except Exception:
            return False

    def wait_for_di(self, di_number, timeout=None):
        start = time.time()
        while True:
            if self.read_di(di_number):
                return True
            if timeout and (time.time() - start) >= timeout:
                return False
            time.sleep(0.05)

    def wait_for_di_any(self, di_numbers, timeout=None):
        start = time.time()
        while True:
            for di in di_numbers:
                if self.read_di(di):
                    return di
            if timeout and (time.time() - start) >= timeout:
                return None
            time.sleep(0.05)

    def send_result(self, status: bool):
        ok_var = self.do_vars.get(3)   # DO3 OK
        nok_var = self.do_vars.get(2)  # DO2 NG
        try:
            if status:
                self._send_command(ok_var, 1)
                self._send_command(nok_var, 0)
            else:
                self._send_command(ok_var, 0)
                self._send_command(nok_var, 1)
        except Exception as e:
            logger.warning(f"OpenShowVar send result error: {e}")

    def reset_outputs(self):
        for do_num in self.do_vars:
            self.send_do(do_num, False)
        return True

    def set_conveyor(self, on: bool):
        return self.send_do(0, on)

    def send_do(self, do_number, value):
        var_name = self.do_vars.get(do_number)
        if not var_name:
            return False
        try:
            self._send_command(var_name, 1 if value else 0)
            return True
        except Exception:
            return False

    def disconnect(self):
        if self.sock:
            self.sock.close()
            logger.info("OpenShowVar connection closed")
