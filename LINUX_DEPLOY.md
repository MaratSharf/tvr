# Запуск на ALT Linux — рекомендации

> ⚠️ **Проект заточен под Windows 7**: cifX DLL, WinMM таймеры, Hikrobot MVS SDK для Windows. На Linux потребуются изменения.

---

## 1. Что НЕ заработает без изменений

| Компонент | Windows | Linux (ALT) | Что делать |
|-----------|---------|-------------|------------|
| `cifx32dll.dll` | ✅ | ❌ | Нужен **cifX Linux SDK** (Hilscher) |
| `timeBeginPeriod` (winmm) | ✅ | ❌ | Заменить на `timerfd` / `clock_nanosleep` |
| Hikrobot MVS SDK | Windows only | Linux SDK есть | Скачать **MVS_Linux_x64** с сайта Hikrobot |
| PyInstaller `--onefile` | ✅ | ❌ (ELF) | Сборка под Linux отдельно |
| OWEN MK210 Modbus TCP | ✅ | ✅ | Работает (TCP) |
| OpenShowVar / EthernetKRL | ✅ | ✅ | Работает (TCP) |

---

## 2. Варианты архитектуры на Linux

### Вариант А: PROFINET через cifX (production) — сложно
- Нужен **Hilscher cifX Linux Driver + SDK** (платный, запрос у Hilscher)
- Python: `ctypes.CDLL("libcifx.so")` + другие ioctl
- Есть open-source обёртка: `pycifx` (проверьте совместимость)

### Вариант Б: OpenShowVar (рекомендую для Linux) — просто
- KUKAVARPROXY на KRC4 (порт 7000)
- Python: чистый TCP сокет — работает везде
- Не нужны проприетарные драйверы

### Вариант В: EthernetKRL — средне
- Настроить `EthernetKRL.xml` на KRC4
- Python TCP клиент (EKI протокол)

---

## 3. Подготовка ALT Linux

```bash
# ALT Linux 10.x / Sisyphus
sudo apt-get update
sudo apt-get install python3.11 python3.11-venv python3.11-devel \
    gcc gcc-c++ make cmake \
    libopencv-dev libopencv-core libopencv-imgproc libopencv-highgui \
    v4l-utils usbutils

# Python 3.11 (в ALT по умолчанию) — OK, код совместим с 3.8+
```

---

## 4. Hikrobot камера на Linux

1. Скачайте **MVS_Linux_x64_<version>.tar.gz** с сайта Hikrobot (нужен аккаунт)
2. Установите:
```bash
tar -xzf MVS_Linux_x64_*.tar.gz
cd MVS_Linux_x64
sudo ./install.sh
# Установит /opt/MVS/lib/libMvCameraControl.so
```
3. Python wrapper:
```bash
pip install git+https://github.com/hikrobot/MVS-Python.git
# или руками: ctypes.CDLL("/opt/MVS/lib/libMvCameraControl.so")
```
4. Правила udev для USB/GigE:
```bash
# /etc/udev/rules.d/99-hikrobot.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="0da3", MODE="0666"
```
5. Настройка IP камеры (GigE):
```bash
# В camera.py: использовать MVS SDK вместо Modbus триггера + чтение папки
# Либо оставить Modbus триггер к камере (если камера поддерживает Modbus TCP)
```

---

## 5. Изменения в коде для Linux

### `communication/profinet.py` → убрать или заменить на OpenShowVar
```python
# УДАЛИТЬ winmm вызовы:
# ctypes.windll.winmm.timeBeginPeriod(1)
# ctypes.windll.winmm.timeEndPeriod(1)

# ЗАМЕНИТЬ precise_sleep на:
import time
def precise_sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.0001)  # 100 µs
```

### `camera.py` — Hikrobot MVS Linux SDK
```python
# Вместо Modbus триггера + чтение папки:
from mvs import MVSCamera  # гипотетический wrapper
cam = MVSCamera(ip="192.168.1.36")
cam.trigger_software()
img = cam.get_frame()
```

### `config.json` — режим связи
```json
{
  "communication": {
    "mode": "openshowvar",
    "openshowvar": {"ip": "192.168.1.1", "port": 7000}
  },
  "camera": {
    "ip": "192.168.1.36",
    "simulation": false,
    "linux_mvs": true
  }
}
```

---

## 6. Сборка на ALT Linux

```bash
cd /opt/tvrus
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements_linux.txt

# requirements_linux.txt:
# opencv-python-headless
# numpy
# pymodbus
# pyinstaller

# Сборка (создаст ELF бинарник):
pyinstaller --onefile --name SeamInspection main.py
# dist/SeamInspection — запускается на любой ALT Linux с glibc
```

---

## 7. Systemd сервис (автозапуск)

```ini
# /etc/systemd/system/seam-inspection.service
[Unit]
Description=Seam Inspection System
After=network.target

[Service]
Type=simple
User=operator
WorkingDirectory=/opt/tvrus
ExecStart=/opt/tvrus/dist/SeamInspection
Restart=on-failure
RestartSec=5
Environment=DISPLAY=:0

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now seam-inspection
```

---

## 8. Чек-лист миграции

| Задача | Статус |
|--------|--------|
| [ ] Установить ALT Linux на IPC (SIMATIC IPC547G — x64, UEFI) | |
| [ ] Установить Python 3.11 + devel пакеты | |
| [ ] Установить Hikrobot MVS Linux SDK | |
| [ ] Настроить udev для камеры | |
| [ ] Переписать `camera.py` под MVS Linux SDK | |
| [ ] Убрать `profinet.py` / winmm, переключиться на `openshowvar` | |
| [ ] Установить KUKAVARPROXY на KRC4 (порт 7000) | |
| [ ] Обновить `config.json` → `mode: openshowvar` | |
| [ ] Собрать `pyinstaller --onefile` на ALT Linux | |
| [ ] Настроить systemd сервис | |
| [ ] Протестировать цикл: KRC4 → OpenShowVar → Python → камера → результат | |

---

## 9. Полезные ссылки

- **Hilscher cifX Linux SDK** — запрос через support.hilscher.com (нужен серийный номер карты)
- **Hikrobot MVS Linux SDK** — личный кабинет на hikrobotics.com
- **OpenShowVar** — https://github.com/roboticslab-uc3m/OpenShowVar (Python клиент внутри)
- **pycifx** — https://github.com/... (проверьте актуальность)

---

## Резюме

| Путь | Сложность | Надёжность | Рекомендация |
|------|-----------|------------|--------------|
| PROFINET/cifX на Linux | Очень высокая | Высокая (RT) | Только если обязателен PROFINET |
| **OpenShowVar на Linux** | **Низкая** | **Хорошая** | **Рекомендую для старта** |
| EthernetKRL на Linux | Средняя | Хорошая | Если есть лицензия EKI |

**Начните с OpenShowVar** — заработает за час, без проприетарных драйверов. PROFINET/cifX оставьте на Windows 7 стенде.