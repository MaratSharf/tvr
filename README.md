# Инструкция по запуску системы контроля шва

## 1. Установка зависимостей

### 1.1. Установка Python 3.8.10

Скачать Python 3.8.10 с официального сайта:
https://www.python.org/downloads/release/python-3810/

При установке отметить галочку "Add Python to PATH".

Проверить установку:
```cmd
python --version
```

### 1.2. Виртуальное окружение

Создать виртуальное окружение:
```cmd
cd C:\proj\python\tvrus
python -m venv venv
```

Активировать окружение (PowerShell):
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
venv\Scripts\Activate.ps1
```

Или через cmd:
```cmd
venv\Scripts\activate
```

В начале строки появится `(venv)`.

### 1.3. Установка библиотек

В активированном окружении выполнить:
```cmd
pip install -r requirements.txt
```

Деактивация окружения:
```cmd
deactivate
```

### 1.3. Установка MVS SDK (для камеры Hikrobot)

1. Скачать MVS 5.x с сайта Hikrobot
2. Установить MVS (включая драйверы камеры)
3. Подключить камеру по Ethernet
4. Проверить обнаружение камеры в клиенте MVS

## 2. Настройка оборудования

### 2.1. Камера Hikrobot

- IP камеры: 192.168.1.36
- Подключение: GigE Vision (Ethernet)
- Убедиться, что ПК и камера в одной подсети

### 2.2. Тестовый стенд (ОВЕН МК210)

- IP контроллера: 192.168.1.99
- Порт Modbus: 502
- Убедиться, что контроллер включён и доступен

### 2.3. Схема подключений ОВЕН

**Входы (DI):**

| DI | Назначение |
|----|------------|
| DI0 | Датчик Д1 (вход конвейера) |
| DI1 | Датчик Д2 (под камерой левый) |
| DI2 | Датчик Д3 (под камерой правый) |
| DI3 | Датчик Д4 (выход конвейера) |
| DI4 | Тумблер (положение A — старт) |
| DI5 | Тумблер (положение B — конец) |
| DI6 | Датчик Д6 (захват / сценарий B) |

**Выходы (DO):**

| DO | Назначение |
|----|------------|
| DO0 | Конвейер (пуск/стоп) |
| DO1 | Толкатель брака (резерв) |
| DO2 | Лампа NG (красная) |
| DO3 | Лампа OK (зелёная) |

### 2.4. Проверка сети

```cmd
ping 192.168.1.36
ping 192.168.1.99
```

## 3. Конфигурация

Файл `config.json` содержит настройки по умолчанию:

```json
{
    "camera": {
        "ip": "192.168.1.36",
        "exposure": 10000,
        "gain": 1.0,
        "simulation": false,
        "image_folder": "data/images/foto/NG",
        "modbus": {
            "enabled": true,
            "ip": "192.168.1.36",
            "port": 502,
            "unit": 1,
            "control_offset": 0,
            "status_offset": 1,
            "result_offset": 2,
            "command_offset": 500,
            "byte_order": "little",
            "command_terminator": "",
            "trigger_timeout": 10
        }
    },
    "seam_control": {
        "min_width_mm": 1.0,
        "max_width_mm": 6.0,
        "px_per_mm": 50.0,
        "threshold": 120
    },
    "communication": {
        "mode": "modbus",
        "modbus": {
            "ip": "192.168.1.99",
            "port": 502,
            "unit": 1,
            "input_reg": 51,
            "output_write_reg": 470,
            "di_map": { "di04": 4, "di05": 5, "di06": 6 },
            "do_map": { "do00": 0, "do01": 1, "do02": 2, "do03": 3 }
        }
    }
}
```

При необходимости изменить параметры под ваше оборудование.

## 4. Запуск

### 4.1. Запуск

```cmd
cd C:\proj\python\tvrus
venv\Scripts\activate
python main.py
```

Для ROI-режима (выделение зоны сканирования):
```cmd
python main.py --roi
```

При запуске программа:
1. Подключается к камере Hikrobot (Modbus 192.168.1.36:502)
2. Подключается к ОВЕН МК210 (Modbus 192.168.1.99:502)
3. Сбрасывает все выходы (DO0-DO3 = 0)
4. Ожидает DI04 (Тумблер A — старт)
5. Ожидает DI06 (Датчик Д6 — захват)
6. Отправляет триггер камере через Modbus
7. Камера сохраняет фото в папку `data/images/`
8. Программа читает последнее фото и обрабатывает:
   - Обрезка по ROI
   - CLAHE + GaussianBlur + Otsu threshold
   - Морфологическая очистка
   - Поиск контуров и измерение ширины шва
9. Ширина в мм: `width_mm = width_px / px_per_mm`
10. OK если ширина в диапазоне [min_width_mm, max_width_mm]
11. Отправка результата: DO3 (зелёная) или DO2 (красная)
12. Ожидание DI05 (Тумблер B — конец цикла)
13. Сброс всех DO, возврат к шагу 4

### 4.2. Остановка

Нажать `Ctrl+C` в командной строке.

## 5. Логирование

Все измерения записываются в файл `app.log`:

```
2026-09-03 10:30:15 Width: 4.25mm - OK
2026-09-03 10:30:18 Width: 1.80mm - Width out of range
```

## 6. Калибровка

### 6.1. Определение px_per_mm

1. Сфотографировать объект известного размера (линейку)
2. Измерить ширину объекта в пикселях (OpenCV)
3. Вычислить: `px_per_mm = пиксели / миллиметры`

### 6.2. Настройка порога

Параметр `threshold` в config.json определяет чувствительность бинаризации:
- Меньше значение = больше деталей (но больше шума)
- Больше значение = меньше деталей (но чище изображение)

## 7. Добавление PROFINET

Для работы с KUKA KR200 через PROFINET:

1. Установить драйверы Hilscher cifX
2. Создать `communication/profinet.py`
3. Изменить `config.json`:
   ```json
   "communication": {
       "mode": "profinet",
       "profinet": {
           "device_name": "cifX0"
       }
   }
   ```

## 8. Сборка .exe

Для создания исполняемого файла:

```cmd
cd C:\proj\python\tvrus
venv\Scripts\activate
pyinstaller --onefile main.py
```

Для PowerShell:
```powershell
cd C:\proj\python\tvrus
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
venv\Scripts\Activate.ps1
pyinstaller --onefile main.py
```

Файл будет создан в папке `dist/main.exe`.

## 9. Автозапуск

### 9.1. Через планировщик задач

1. Открыть Планировщик задач Windows
2. Создать задачу
3. Указать путь к `main.exe`
4. Настроить запуск при входе в систему

### 9.2. Через автозагрузку

Создать ярлык `main.exe` и поместить в:
```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

## 10. Устранение неполадок

### Камера не определяется

- Проверить IP-адрес камеры
- Убедиться, что MVS установлен
- Проверить брандмауэр Windows

### Modbus не подключается

- Проверить IP контроллера
- Убедиться, что порт 502 открыт
- Проверить настройки Modbus в контроллере

### Ошибки в логах

Проверить файл `app.log` для диагностики.

## 11. Структура проекта

```
tvrus/
├── main.py              # точка входа, главный цикл
├── camera.py            # камера Hikrobot (Modbus-триггер, чтение папки)
├── image_processor.py   # обработка изображений (замер ширины шва)
├── roi_selector.py      # интерактивное выделение ROI
├── config.json          # конфигурация
├── requirements.txt     # зависимости
├── communication/
│   ├── __init__.py      # экспорт пакета
│   ├── base.py          # абстрактный класс RobotCommunicator
│   ├── factory.py       # фабрика коммуникаторов
│   ├── modbus.py        # Modbus TCP (OWEN MK210)
│   ├── openshowvar.py   # OpenShowVar (KUKAVARPROXY)
│   └── simulation.py    # симуляция (тестирование без оборудования)
├── data/
│   ├── images/foto/NG/  # папка для изображений с камеры
│   └── results/         # сохранённые результаты inspection
├── venv/                # виртуальное окружение
├── app.log              # логи (создаётся при запуске)
└── ml121_200rb_100/     # бэкап KUKA (НЕ ИЗМЕНЯТЬ)
```