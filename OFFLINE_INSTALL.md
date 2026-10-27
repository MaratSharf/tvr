# Установка зависимостей на ISRA ПК (без интернета)

## 1. Подготовка на dev-машине (с интернетом)

```bash
cd C:\proj\python\tvrus
mkdir wheels
pip download -r requirements.txt -d ./wheels
```

Или конкретные версии для **Python 3.8 / Windows 7 64-bit**:

```bash
pip download \
  opencv-python==4.8.1.78 \
  numpy==1.24.3 \
  pymodbus==2.5.3 \
  PyInstaller==5.13.2 \
  -d ./wheels
```

Перенесите папку `wheels` на ISRA ПК (флешка, общая папка).

---

## 2. Установка на ISRA ПК (offline)

```bash
cd C:\path\to\tvrus
python -m venv venv
venv\Scripts\activate

pip install --no-index --find-links=./wheels -r requirements.txt
```

Проверка:
```bash
python -c "import cv2, numpy, pymodbus; print(cv2.__version__, numpy.__version__)"
```

---

## 3. Совместимые версии (Windows 7 + Python 3.8.10)

| Пакет | Версия | Причина |
|-------|--------|---------|
| opencv-python | **4.8.1.78** | Последний с wheel для Python 3.8 |
| numpy | **1.24.3** | Последний для Python 3.8 |
| pymodbus | **2.5.3** | Последний для Python 3.8 |
| PyInstaller | **5.13.2** | Последний для Python 3.8 |

> **Не используйте** opencv-python ≥ 4.9, numpy ≥ 1.25 — требуют Python 3.9+.

---

## 4. Сборка .exe (на ISRA ПК)

```bash
pyinstaller --onefile --name SeamInspection main.py
```

Результат: `dist\SeamInspection.exe` — переносимый, без venv.

---

## 5. Автозапуск (production)

**Вариант А — ярлык в `shell:startup`:**
```
C:\path\to\tvrus\dist\SeamInspection.exe
```

**Вариант Б — реестр (HKLM\Software\Microsoft\Windows\CurrentVersion\Run):**
```reg
[HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Run]
"SeamInspection"="C:\\path\\to\\tvrus\\dist\\SeamInspection.exe"
```

---

## 6. Конфиг для PROFINET (config.json)

```json
{
  "communication": {
    "mode": "profinet",
    "profinet": {"device_name": "cifX0"}
  }
}
```

---

## 7. Проверка cifX на ISRA ПК

```bash
# DLL в системе
dir C:\Windows\System32\cifx32dll.dll
dir C:\Windows\System32\cifx32dll64.dll

# Панель управления cifX
cifXCPL.cpl
# Должно быть устройство cifX0, прошивка PROFINET, Communication OK
```

Имя PROFINET-устройства в SYCON.net должно совпадать:
```
h901-200rb-100-kf084.m489g020s20-121-ml-3
```

---

## 8. Запуск

```bash
# Графический интерфейс (рекомендуется для эксплуатации)
python gui.py
python main.py --gui

# Консольный режим (для отладки)
python main.py

# Выбор ROI (настройка зоны)
python main.py --roi

# Production .exe
dist\SeamInspection.exe
```

---

## 9. Логи

- Файл: `app.log` (путь в config.json)
- Уровень: `INFO` (меняется в config.json)

---

## 10. Fallback режимы (если cifX недоступен)

```json
// Тестовый стенд (Modbus + ОВЕН МК210)
"communication": {"mode": "modbus"}

// Только камера + обработка (без робота)
"communication": {"mode": "simulation"}
```