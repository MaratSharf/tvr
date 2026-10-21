from abc import ABC, abstractmethod


class RobotCommunicator(ABC):
    @abstractmethod
    def connect(self):
        pass

    @abstractmethod
    def read_di(self, di_number):
        """Чтение дискретного входа."""
        pass

    @abstractmethod
    def wait_for_di(self, di_number, timeout=None):
        """Ожидание активации DI."""
        pass

    @abstractmethod
    def wait_for_di_any(self, di_numbers, timeout=None):
        """Ожидание активации любого из DI. Возвращает номер DI или None."""
        pass

    @abstractmethod
    def send_result(self, status: bool):
        """Отправка результата: True=OK (DO3), False=NG (DO2)."""
        pass

    @abstractmethod
    def reset_outputs(self):
        """Сброс всех дискретных выходов."""
        pass

    @abstractmethod
    def set_conveyor(self, on: bool):
        """Управление конвейером (DO0)."""
        pass

    @abstractmethod
    def send_do(self, do_number, value: bool):
        """Запись дискретного выхода."""
        pass

    @abstractmethod
    def disconnect(self):
        pass
