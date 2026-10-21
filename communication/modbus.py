import time
import logging
from pymodbus.client.sync import ModbusTcpClient
from .base import RobotCommunicator

logger = logging.getLogger(__name__)


class ModbusCommunicator(RobotCommunicator):
    def __init__(self, config):
        self.ip = config['ip']
        self.port = config['port']
        self.unit = config.get('unit', 1)
        self.input_reg = config.get('input_reg', 51)
        self.output_write_reg = config.get('output_write_reg', 470)

        # DI bit mapping: {di_number: bit_position}
        di_map = config.get('di_map', {})
        self.di_bits = {
            4: di_map.get('di04', 4),
            5: di_map.get('di05', 5),
            6: di_map.get('di06', 6),
        }

        # DO bit mapping: {do_number: bit_position}
        do_map = config.get('do_map', {})
        self.do_bits = {
            0: do_map.get('do00', 0),  # Конвейер (пуск/стоп)
            1: do_map.get('do01', 1),  # Толкатель брака
            2: do_map.get('do02', 2),  # Лампа NG (красная)
            3: do_map.get('do03', 3),  # Лампа OK (зелёная)
        }

        self.client = None
        # Последнее прочитанное состояние DI: для детектирования фронта 0->1
        self._di_state = {}

    def connect(self):
        self.client = ModbusTcpClient(self.ip, port=self.port, timeout=3.0)
        result = self.client.connect()
        if result:
            logger.info(f"Modbus connected: {self.ip}:{self.port}")
        else:
            logger.error(f"Modbus connection error: {self.ip}:{self.port}")
        return result

    def _read_input_register(self):
        """Чтение входного регистра со всеми DI."""
        if not self.client:
            return None
        try:
            resp = self.client.read_holding_registers(self.input_reg, count=1, unit=self.unit)
            if resp.isError():
                return None
            return resp.registers[0]
        except Exception as e:
            logger.warning(f"Modbus read error: {e}")
            return None

    def read_di(self, di_number):
        """Чтение одного DI по номеру (4, 5, 6)."""
        val = self._read_input_register()
        if val is None:
            return False
        bit = self.di_bits.get(di_number)
        if bit is None:
            return False
        return (val >> bit) & 1 == 1

    @staticmethod
    def _di_active(di_number, value, di_bits):
        """Состояние DI из регистра. None - значение недоступно."""
        if value is None:
            return None
        bit = di_bits.get(di_number)
        if bit is None:
            return None
        return (value >> bit) & 1 == 1

    def wait_for_di(self, di_number, timeout=None):
        """Ожидание активации DI (фронт 0->1) с таймаутом.

        Удержанный сигнал срабатывает один раз: следующее ожидание
        требует отпускания входа перед повторным срабатыванием.
        """
        start = time.time()
        prev = self._di_state.get(di_number, False)
        while True:
            active = self._di_active(di_number, self._read_input_register(), self.di_bits)
            if active is not None:
                if active and not prev:
                    self._di_state[di_number] = True
                    return True
                prev = active
                self._di_state[di_number] = active
            if timeout and (time.time() - start) >= timeout:
                return False
            time.sleep(0.05)

    def wait_for_di_any(self, di_numbers, timeout=None):
        """Ожидание активации любого из DI. Возвращает номер DI или None."""
        start = time.time()
        prev = {di: self._di_state.get(di, False) for di in di_numbers}
        while True:
            value = self._read_input_register()
            if value is not None:
                for di in di_numbers:
                    active = self._di_active(di, value, self.di_bits)
                    if active is None:
                        continue
                    if active and not prev[di]:
                        self._di_state[di] = True
                        return di
                    prev[di] = active
                    self._di_state[di] = active
            if timeout and (time.time() - start) >= timeout:
                return None
            time.sleep(0.05)

    def send_result(self, status: bool):
        """Отправка результата через DO: DO3=OK (зелёная), DO2=NG (красная)."""
        if not self.client:
            return False
        try:
            resp_read = self.client.read_holding_registers(self.output_write_reg, count=1, unit=self.unit)
            if resp_read.isError():
                return False
            current = resp_read.registers[0]

            current &= ~(1 << self.do_bits[2])  # Сброс DO2 (NG)
            current &= ~(1 << self.do_bits[3])  # Сброс DO3 (OK)

            if status:
                current |= (1 << self.do_bits[3])  # Зажечь DO3 (OK зелёная)
            else:
                current |= (1 << self.do_bits[2])  # Зажечь DO2 (NG красная)

            resp = self.client.write_register(self.output_write_reg, current, unit=self.unit)
            return not resp.isError()
        except Exception as e:
            logger.warning(f"Modbus send result error: {e}")
            return False

    def reset_outputs(self):
        """Сброс всех DO (лампы, конвейер, толкатель)."""
        if not self.client:
            return False
        try:
            resp = self.client.write_register(self.output_write_reg, 0, unit=self.unit)
            return not resp.isError()
        except Exception as e:
            logger.warning(f"Modbus reset DO error: {e}")
            return False

    def set_conveyor(self, on: bool):
        """Управление конвейером: DO0."""
        return self.send_do(0, on)

    def send_do(self, do_number, value):
        """Запись дискретного выхода."""
        if not self.client:
            return False
        try:
            resp_read = self.client.read_holding_registers(self.output_write_reg, count=1, unit=self.unit)
            if resp_read.isError():
                return False
            current = resp_read.registers[0]
            bit = self.do_bits.get(do_number)
            if bit is None:
                return False
            if value:
                current |= (1 << bit)
            else:
                current &= ~(1 << bit)
            resp = self.client.write_register(self.output_write_reg, current, unit=self.unit)
            return not resp.isError()
        except Exception as e:
            logger.warning(f"Modbus write DO error: {e}")
            return False

    def disconnect(self):
        if self.client:
            self.client.close()
            logger.info("Modbus connection closed")

