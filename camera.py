import cv2
import numpy as np
import os
import time
import logging
from pymodbus.client.sync import ModbusTcpClient
from pymodbus.exceptions import ModbusIOException, ConnectionException

logger = logging.getLogger(__name__)


class HikrobotCamera:
    def __init__(self, config):
        self.ip = config['ip']
        self.exposure = config.get('exposure', 10000)
        self.gain = config.get('gain', 1.0)
        self.image_folder = config.get('image_folder', '')
        self.cap = None
        self.simulation = config.get('simulation', False)
        self.last_image_time = 0

        # Modbus settings
        modbus_cfg = config.get('modbus', {})
        self.modbus_enabled = modbus_cfg.get('enabled', False)
        self.modbus_ip = modbus_cfg.get('ip', self.ip)
        self.modbus_port = modbus_cfg.get('port', 502)
        self.modbus_unit = modbus_cfg.get('unit', 1)
        self.control_offset = modbus_cfg.get('control_offset', 0)
        self.status_offset = modbus_cfg.get('status_offset', 1)
        self.result_offset = modbus_cfg.get('result_offset', 2)
        self.command_offset = modbus_cfg.get('command_offset', 500)
        self.byte_order = modbus_cfg.get('byte_order', 'little')
        self.command_terminator = modbus_cfg.get('command_terminator', '')
        self.trigger_timeout = modbus_cfg.get('trigger_timeout', 10)

        self.modbus_client = None
        self.reconnect_delay = 1.0
        self.max_reconnect_delay = 30.0
        self.last_reconnect_attempt = 0

    def connect(self):
        if self.simulation:
            return True
        if self.modbus_enabled:
            return self._connect_modbus()
        if self.image_folder:
            return os.path.exists(self.image_folder)
        url = f"rtsp://{self.ip}:554/live"
        self.cap = cv2.VideoCapture(url)
        return self.cap.isOpened()

    def _connect_modbus(self):
        logger.info(f"Modbus connecting to camera: {self.modbus_ip}:{self.modbus_port}")
        try:
            if self.modbus_client:
                self.modbus_client.close()
                time.sleep(0.5)
            self.modbus_client = ModbusTcpClient(
                self.modbus_ip,
                port=self.modbus_port,
                timeout=3.0
            )
            result = self.modbus_client.connect()
            if result:
                logger.info("Modbus connected successfully")
                self.reconnect_delay = 1.0
            else:
                logger.error("Modbus connection error")
            return result
        except Exception as e:
            logger.error(f"Modbus connection exception: {e}")
            return False

    def _reconnect_modbus(self):
        now = time.time()
        if now - self.last_reconnect_attempt < self.reconnect_delay:
            return False
        self.last_reconnect_attempt = now
        logger.warning(f"Modbus reconnecting (delay {self.reconnect_delay:.1f}s)...")
        success = self._connect_modbus()
        if success:
            self.reconnect_delay = 1.0
        else:
            self.reconnect_delay = min(self.reconnect_delay * 2, self.max_reconnect_delay)
        return success

    def _is_modbus_connected(self):
        if self.modbus_client is None:
            return False
        return getattr(self.modbus_client, 'socket', None) is not None

    def _ensure_modbus_connection(self):
        if self._is_modbus_connected():
            return True
        return self._reconnect_modbus()

    def _note_com_failure(self):
        """Обрыв связи: закрыть сокет и запустить backoff, чтобы не блокировать цикл."""
        self.last_reconnect_attempt = time.time()
        if self.modbus_client is not None:
            try:
                self.modbus_client.close()
            except Exception:
                pass

    def _write_register(self, reg, value):
        if not self._ensure_modbus_connection():
            return False
        try:
            resp = self.modbus_client.write_register(reg, value, unit=self.modbus_unit)
            if resp.isError():
                logger.error(f"Modbus write register {reg} error: {resp}")
                return False
            return True
        except (ConnectionResetError, BrokenPipeError, OSError,
                ModbusIOException, ConnectionException) as e:
            logger.warning(f"Modbus write communication error: {e}")
            self._note_com_failure()
            return False

    def _read_register(self, reg):
        if not self._ensure_modbus_connection():
            return None
        try:
            resp = self.modbus_client.read_holding_registers(reg, count=1, unit=self.modbus_unit)
            if resp.isError():
                logger.error(f"Modbus read register {reg} error: {resp}")
                return None
            return resp.registers[0]
        except (ConnectionResetError, BrokenPipeError, OSError,
                ModbusIOException, ConnectionException) as e:
            logger.warning(f"Modbus read communication error: {e}")
            self._note_com_failure()
            return None

    def _read_status(self):
        val = self._read_register(self.status_offset)
        if val is None:
            return None
        return {
            'trigger_ready': bool(val & 0x0001),
            'trigger_ack': bool(val & 0x0002),
            'acquiring': bool(val & 0x0004),
            'decoding': bool(val & 0x0008),
            'results_available': bool(val & 0x0100),
            'results_timeout': bool(val & 0x0200),
            'command_success': bool(val & 0x0400),
            'command_failed': bool(val & 0x0800),
            'general_fault': bool(val & 0x8000),
        }

    def _string_to_registers(self, text):
        data = text.encode('ascii')
        length = len(data)
        regs = []
        if self.byte_order == 'little':
            for i in range(0, length, 2):
                if i + 1 < length:
                    word = (data[i + 1] << 8) | data[i]
                else:
                    word = data[i]
                regs.append(word)
        else:
            for i in range(0, length, 2):
                if i + 1 < length:
                    word = (data[i] << 8) | data[i + 1]
                else:
                    word = data[i] << 8
                regs.append(word)
        return regs

    def _send_command(self, command_string, timeout=5):
        if not self._ensure_modbus_connection():
            return False
        cmd = command_string + self.command_terminator
        logger.info(f"Modbus команда: {repr(cmd)}")

        if not self._write_register(self.control_offset, 0x8000):
            return False
        time.sleep(0.1)

        if not self._write_register(self.command_offset, len(cmd)):
            return False

        regs = self._string_to_registers(cmd)
        if regs:
            try:
                resp = self.modbus_client.write_registers(self.command_offset + 1, regs, unit=self.modbus_unit)
                if resp.isError():
                    logger.error(f"Modbus command write error: {resp}")
                    return False
            except Exception as e:
                logger.exception("Modbus исключение при записи команды")
                return False

        control = self._read_register(self.control_offset)
        if control is None:
            return False
        control |= 0x0100
        if not self._write_register(self.control_offset, control):
            return False

        start = time.time()
        success = False
        while time.time() - start < timeout:
            time.sleep(0.1)
            status = self._read_status()
            if not status:
                continue
            if status.get('command_success'):
                logger.info("Modbus command executed")
                success = True
                break
            if status.get('command_failed'):
                logger.error("Modbus command failed")
                break
        else:
            logger.error("Modbus command timeout")

        control &= ~0x0100
        self._write_register(self.control_offset, control)
        return success

    def trigger_capture(self):
        """Отправка триггера камере через Modbus. Камера сохранит фото в папку."""
        if not self.modbus_enabled:
            logger.debug("Modbus not enabled, trigger skipped")
            return True

        logger.info("Sending trigger to camera")

        # Сброс состояния
        status = self._read_status()
        if status:
            self._write_register(self.control_offset, 0x8000)
            time.sleep(0.05)
            self._write_register(self.control_offset, 5)
            time.sleep(0.05)

        # Включение Trigger Enable
        if not self._write_register(self.control_offset, 1):
            logger.error("Trigger Enable error")
            return False
        time.sleep(0.02)

        # Ожидание Trigger Ready
        start = time.time()
        while time.time() - start < self.trigger_timeout:
            status = self._read_status()
            if status and status.get('trigger_ready'):
                logger.info("Trigger Ready")
                break
            time.sleep(0.05)
        else:
            logger.error("Trigger Ready timeout")
            self._write_register(self.control_offset, 5)
            return False

        # Отправка триггера
        if not self._write_register(self.control_offset, 3):
            logger.error("Trigger send error")
            return False

        # Ожидание завершения захвата.
        # Не считать завершённым до появления признака занятости камеры:
        # сразу после записи триггера acquiring ещё не успел выставиться.
        start = time.time()
        trigger_sent = time.time()
        saw_busy = False
        completed = False
        timed_out = False
        while time.time() - start < self.trigger_timeout:
            status = self._read_status()
            if not status:
                time.sleep(0.1)
                continue
            if status.get('results_available'):
                completed = True
                break
            busy = status.get('acquiring') or status.get('decoding')
            if busy:
                saw_busy = True
            elif saw_busy:
                completed = True
                break
            elif time.time() - trigger_sent > 1.0:
                logger.warning("Acquiring flag never asserted - treating capture as completed")
                completed = True
                break
            if status.get('results_timeout'):
                logger.warning("Result timeout")
                timed_out = True
                break
            time.sleep(0.05)
        else:
            timed_out = True

        if timed_out:
            logger.error("Capture timeout exceeded")
            self._write_register(self.control_offset, 5)
            return False

        if completed:
            logger.info("Capture completed")

        self._write_register(self.control_offset, 5)
        return True

    def capture(self):
        if self.simulation:
            return self._generate_test_image()
        if self.modbus_enabled:
            return self._read_latest_from_folder()
        if self.image_folder:
            return self._read_latest_from_folder()
        if not self.cap:
            return None
        ret, frame = self.cap.read()
        if ret:
            return frame
        return None

    def _read_latest_from_folder(self):
        """Чтение нового кадра из папки.

        last_image_time обновляется ТОЛЬКО после успешной загрузки файла,
        иначе неудачное чтение (файл ещё дописывается камерой) приводило
        к потере кадра навсегда.
        """
        extensions = ('.jpg', '.jpeg', '.png', '.bmp')
        candidates = []
        try:
            with os.scandir(self.image_folder) as it:
                for entry in it:
                    try:
                        if not entry.is_file():
                            continue
                        if os.path.splitext(entry.name)[1].lower() not in extensions:
                            continue
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    if mtime <= self.last_image_time:
                        continue
                    candidates.append((mtime, entry.path))
        except OSError as e:
            logger.warning(f"Camera folder scan error: {e}")
            return None

        if not candidates:
            return None

        candidates.sort(key=lambda c: c[0])
        # Пробуем от нового к старому: незаконченный файл пропускается,
        # но сам кадр не теряется - повторный вызов вернётся к нему.
        for mtime, path in reversed(candidates):
            if not self._is_file_complete(path):
                logger.warning(f"Image file not fully written yet: {path}")
                continue
            image = cv2.imread(path)
            if image is None:
                logger.warning(f"Failed to decode image: {path}")
                continue
            self.last_image_time = mtime
            return image
        return None

    @staticmethod
    def _is_file_complete(path):
        """Проверка конца файла: у дописываемого кадра нет маркера конца.

        OpenCV декодирует обрезанный JPEG и возвращает картинку (а не None),
        поэтому для JPEG проверяем EOI (FF D9), для PNG - чанк IEND.
        """
        try:
            with open(path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size < 12:
                    return False
                if path.lower().endswith('.png'):
                    f.seek(-8, os.SEEK_END)
                    return f.read(8)[:4] == b'IEND'
                f.seek(-2, os.SEEK_END)
                return f.read(2) == b'\xff\xd9'
        except OSError:
            return False

    def _generate_test_image(self):
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        img[:] = (200, 200, 200)
        x = np.random.randint(290, 350)
        cv2.rectangle(img, (x, 0), (x + 200, 480), (50, 50, 50), -1)
        return img

    def disconnect(self):
        if self.cap:
            self.cap.release()
        if self.modbus_client:
            try:
                self.modbus_client.close()
            except Exception:
                pass
            logger.info("Modbus camera connection closed")
