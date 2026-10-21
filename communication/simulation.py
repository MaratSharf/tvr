import time
from .base import RobotCommunicator


class SimulationCommunicator(RobotCommunicator):
    def __init__(self, config):
        self.connected = False
        self.sim_step = 0
        self.sim_capture_count = 0
        self.sim_max_captures = 3

    def connect(self):
        self.connected = True
        return True

    def read_di(self, di_number):
        return False

    def wait_for_di(self, di_number, timeout=None):
        if not self.connected:
            return False
        if di_number == 4:
            time.sleep(0.2)
            return True
        elif di_number == 6:
            time.sleep(0.5)
            return True
        elif di_number == 5:
            time.sleep(0.3)
            return True
        return False

    def wait_for_di_any(self, di_numbers, timeout=None):
        if not self.connected:
            return None
        if 4 in di_numbers:
            time.sleep(0.2)
            return 4
        if 5 in di_numbers and 6 in di_numbers:
            self.sim_capture_count += 1
            if self.sim_capture_count <= self.sim_max_captures:
                time.sleep(0.5)
                return 6
            else:
                self.sim_capture_count = 0
                time.sleep(0.3)
                return 5
        if 6 in di_numbers:
            time.sleep(0.5)
            return 6
        if 5 in di_numbers:
            time.sleep(0.3)
            return 5
        return None

    def send_result(self, status: bool):
        print(f"  [SIM] Lamp: {'OK (green)' if status else 'NG (red)'}")
        return True

    def reset_outputs(self):
        print("  [SIM] All outputs reset")
        return True

    def set_conveyor(self, on: bool):
        print(f"  [SIM] Conveyor: {'ON' if on else 'OFF'}")
        return True

    def send_do(self, do_number, value):
        return True

    def disconnect(self):
        self.connected = False
