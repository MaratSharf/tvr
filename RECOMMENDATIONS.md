# Рекомендации по связи с роботом KUKA KR200

## Структура проекта ml121_200rb_100

```
ml121_200rb_100/
├── KRC/R1/TP/DAI_KNI/          ← Интерфейс ISRA BEADMASTER2D
├── KRC/R1/TP/DAI_KLB_INTEC/    ← Управление клеевым дозатором Intec
├── KRC/R1/TP/EthernetKRL/      ← Библиотека EKI (установлена, но НЕ настроена)
├── KRC/R1/TP/DAI_CheckTool/    ← Проверка/калибровка инструмента
├── KRC/R1/TP/BrakeTest/        ← Тест тормозов
├── KRC/R1/System/              ← Базовые системные модули
├── KRC/STEU/Mada/              ← Машинные данные (I/O mapping)
└── C/KRC/Roboter/Config/       ← Конфигурация KRC4
```

## Ключевые находки по связи

### 1. ISRA BEADMASTER2D — текущая реализация (DAI_KNI.SRC)

**Входы ISRA → KUKA:** `$IN[1705..1719]` (Tech_Array[11])
**Выходы KUKA → ISRA:** `$OUT[2793..2807]` (Tech_Array[12])

#### Таблица сигналов

| Сигнал | Направление | Функция |
|--------|-------------|---------|
| `I_READY` | ISRA→KUKA | ISRA готова |
| `I_ACK` | ISRA→KUKA | Подтверждение команды |
| `I_ERROR` | ISRA→KUKA | Ошибка ISRA |
| `I_NOK` | ISRA→KUKA | Результат NOK |
| `I_OK` | ISRA→KUKA | Результат OK |
| `I_ERR_WID` | ISRA→KUKA | Ошибка ширины |
| `I_ERR_POS` | ISRA→KUKA | Ошибка позиции |
| `I_ERR_GAP` | ISRA→KUKA | Ошибка зазора |
| `I_ERR_EXTCHK` | ISRA→KUKA | Ошибка доп. проверки |
| `I_ERR_DETECT` | ISRA→KUKA | Ошибка детектирования шва |
| `I_LIFE` | ISRA→KUKA | Life-bit (watchdog) |
| `I_WARN` | ISRA→KUKA | Предупреждение |
| `I_Error_Length` | ISRA→KUKA | Ошибка длины шва |
| `O_TYPE` | KUKA→ISRA | Тип программы (7 бит) |
| `O_RESET` | KUKA→ISRA | Сброс ошибки |
| `O_ACQ` | KUKA→ISRA | Запрос захвата |
| `O_START` | KUKA→ISRA | Старт измерения |

#### Протокол обмена (из DAI_KNI.SRC:130-190)

```
1. IS_Reset()            → сброс всех выходов
2. Ждать I_READY=TRUE    → ISRA готова
3. Записать O_TYPE       → номер программы (7 бит)
4. O_START=TRUE          → старт
5. Ждать I_ACK=TRUE      → подтверждение
6. ANOUT ON O_ANVELOCITY = $VEL_ACT  → синхронизация скорости
7. O_ACQ=TRUE            → захват кадра
8. Ждать результат: I_OK / I_NOK / I_ERROR / I_WARN / таймаут
9. O_ACQ=FALSE, O_START=FALSE → конец
```

### 2. EthernetKRL — НЕ настроен

Файл `EthernetKRL.xml` содержит пустой конфигурационный блок:

```xml
<EthernetKRL>
    <Interface><MemSize>1048576</MemSize></Interface>
    <NetDependency><NetConfig/></NetDependency>
</EthernetKRL>
```

Нет ни одного канала. EKI установлена, но не сконфигурирована.

### 3. I/O Mapping ($machine.dat)

Системные сигналы привязаны:

```
$EXT_START   = $IN[3204]    внешний старт
$MOVE_ENABLE = $IN[3203]    разрешение движения
$RC_RDY1     = $OUT[1092]   RC готов
$DRIVES_ON   = $IN[3202]    приводы включены
$DRIVES_OFF  = $IN[3201]    приводы выключены
$IN_HOME     = $OUT[3216]   робот в home
```

### 4. KSS версия

`$custom.dat`:
```
$V_CUSTOM[]="V1.0.0/KUKA8.3"
```

KSS 8.3 — поддерживает OpenShowVar, EthernetKRL, PROFINET.

---

## Варианты связи с ПК

### Вариант A: EthernetKRL (рекомендуется для production)

**Преимущества:**
- Типизированные данные (REAL, INT, BOOL) — не нужно битовое кодирование
- Встроенные функции EKI_Get*/EKI_Set* — нет ctypes
- Стандартная библиотека KUKA — не требует стороннего ПО
- Уже установлена в проекте

**Недостатки:**
- Требуется лицензия EthernetKRL (опционально, зависит от KRC4)
- Задержка ~10-50 мс (не RT)

#### Настройка EthernetKRL.xml

```xml
<?xml version="1.0" encoding="UTF-8"?>
<EthernetKRL xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
             xsi:noNamespaceSchemaLocation="/Roboter/Config/System/Common/Schemes/EthernetKRL.xsd">
  <Interface>
    <MemSize>1048576</MemSize>
    <DevChannel>
      <Name>SeamControl</Name>
      <VarDefIn>
        <Structure>
          <Element name="trigger" type="BOOL"/>
          <Element name="cmd" type="INT"/>
          <Element name="param1" type="REAL"/>
        </Structure>
      </VarDefIn>
      <VarDefOut>
        <Structure>
          <Element name="status" type="INT"/>
          <Element name="width_mm" type="REAL"/>
          <Element name="error_code" type="INT"/>
          <Element name="life" type="BOOL"/>
        </Structure>
      </VarDefOut>
      <IPAddress>192.168.1.100</IPAddress>
      <Port>50100</Port>
      <OpenTimeout>5000</OpenTimeout>
    </DevChannel>
  </Interface>
  <NetDependency>
    <NetConfig/>
  </NetDependency>
</EthernetKRL>
```

#### KRL-программа (EthernetKRL)

```krl
; === Добавить переменные в $config.dat ===
DECL EKI_STATUS EKI_Ret
DECL INT V_SEAM_STATUS
DECL REAL V_SEAM_WIDTH
DECL INT V_SEAM_ERROR
DECL BOOL V_SEAM_LIFE

; === Основная программа ===
DEF SEAM_CONTROL()
  ; Инициализация
  EKI_Ret = EKI_Init("SeamControl")
  EKI_Ret = EKI_Open("SeamControl")
  WAIT SEC 0.5

  ; Основной цикл
  REPEAT
    ; Читаем команду от ПК
    DECL BOOL trigger
    DECL INT cmd
    EKI_Ret = EKI_GetBool("SeamControl", "trigger", trigger)
    EKI_Ret = EKI_GetInt("SeamControl", "cmd", cmd)

    IF trigger THEN
      ; ПК выполняет измерение и записывает результат
      ; Читаем результат
      EKI_Ret = EKI_GetReal("SeamControl", "width_mm", V_SEAM_WIDTH)
      EKI_Ret = EKI_GetInt("SeamControl", "status", V_SEAM_STATUS)

      ; Обработка результата
      SWITCH V_SEAM_STATUS
      CASE 1  ; OK
        ; Продолжаем
      CASE 0  ; NOK
        ; Остановка / коррекция
      ENDSWITCH
    ENDIF
  UNTIL FALSE

  EKI_Ret = EKI_Close("SeamControl")
END
```

#### Python-клиент (EthernetKRL)

```python
import socket
import struct
import time
import threading

class EKI_Client:
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.ip, self.port))

    def send_bool(self, name, value):
        # Протокол EKI: тип + имя + значение
        pass  # Реализация зависит от формата EKI

    def send_int(self, name, value):
        pass

    def send_real(self, name, value):
        pass

    def read_int(self, name):
        pass

    def read_real(self, name):
        pass

    def close(self):
        if self.sock:
            self.sock.close()
```

---

### Вариант B: OpenShowVar (рекомендуется для быстрого прототипа)

**Преимущества:**
- Не требует лицензий
- TCP на порту 7000
- Доступ ко всем переменным KRL
- Простая установка

**Недостатки:**
- Все данные передаются как строки — нет типизации
- Задержка ~5-10 мс на запрос
- Нет шифрования

#### Установка на KRC4

1. Скопировать папку `OpenShowVar` на KRC4 (диск C:)
2. Запустить `KUKAVARPROXY.exe`
3. Добавить в Autostart
4. Открыть порт 7000 в брандмауэре

#### Изменения в KRL

```krl
; === Добавить переменные в $config.dat ===
DECL BOOL V_SEAM_TRIGGER
DECL INT  V_SEAM_CMD
DECL REAL V_SEAM_WIDTH
DECL INT  V_SEAM_STATUS
DECL INT  V_SEAM_ERROR
DECL BOOL V_SEAM_LIFE

; === В main.src ===
DEF MAIN()
  ; Инициализация
  V_SEAM_TRIGGER = FALSE
  V_SEAM_CMD = 0
  V_SEAM_STATUS = 0
  V_SEAM_WIDTH = 0.0
  V_SEAM_ERROR = 0
  V_SEAM_LIFE = FALSE

  LOOP
    ; Ждём команду от ПК
    WAIT FOR V_SEAM_TRIGGER == TRUE

    ; ПК читает V_SEAM_CMD и выполняет измерение
    ; ПК записывает результат обратно

    ; Сброс триггера
    V_SEAM_TRIGGER = FALSE

    ; Обработка
    IF V_SEAM_STATUS == 1 THEN
      ; OK
    ELSE
      ; NOK
    ENDIF
  ENDLOOP
END
```

#### Python-клиент (OpenShowVar)

```python
from pyopenshowvar import OpenShowVar
import time

class OpenShowVarClient:
    def __init__(self, ip='192.168.1.1', port=7000):
        self.robot = OpenShowVar(ip, port)

    def wait_trigger(self, timeout=30):
        start = time.time()
        while time.time() - start < timeout:
            val = self.robot.read('V_SEAM_TRIGGER')
            if val and val.strip() == 'TRUE':
                return True
            time.sleep(0.01)
        return False

    def send_result(self, status, width, error=0):
        self.robot.write('V_SEAM_STATUS', status)
        self.robot.write('V_SEAM_WIDTH', round(width, 2))
        self.robot.write('V_SEAM_ERROR', error)
        self.robot.write('V_SEAM_TRIGGER', 'FALSE')

    def read_cmd(self):
        val = self.robot.read('V_SEAM_CMD')
        return int(val) if val else 0

    def close(self):
        pass

# Использование
client = OpenShowVarClient('192.168.1.1', 7000)
while True:
    if client.wait_trigger():
        cmd = client.read_cmd()
        if cmd == 1:
            width = measure_seam()  # Функция измерения
            status = 1 if 2.0 <= width <= 6.0 else 0
            client.send_result(status, width)
```

---

### Вариант C: Modbus TCP (для тестового стенда)

Для тестирования без робота используется ОВЕН МК210 (192.168.1.99:502). Не требует изменений в KRL.

---

### Вариант D: PROFINET через cifX (рекомендуется для production — reuse конфигурации)

**Ключевая находка:** Плата Hilscher cifX уже настроена для работы ISRA с роботом. Конфигурация PROFINET содержится в KRC4-бэкапе и может быть reuse-ана.

#### Что найдено в проекте

PROFINET-конфигурация в `PNIODriver.xml`:

```xml
<PROFINET PNIODeviceName="h901-200rb-100-kf084.m489g020s20-121-ml-3" />

<ControllerInstance Name="PNIO-CTRL" busCycleTime="8" busTimeout="20000">
  <Device Name="x-200grp101-kf015.m489g020-021-ml1plc3"
           ARUserID="15" InputDataLength="16" OutputDataLength="15" />
  <Device Name="h209-200klb101-ta160.m489g020ml1plc3"
           ARUserID="160" InputDataLength="44" OutputDataLength="44" />
  <Device Name="h209-200msr101-ta162.m489g020ml1plc3"
           ARUserID="162" InputDataLength="11" OutputDataLength="13" />
</ControllerInstance>
```

ISRA I/O в `KRC_IO.xml`:

```xml
<!-- Outputs: KUKA → ISRA -->
<Out krcStartIndex="1705" segmentID="27">
  <Start segmentByteOffset="0" segmentBitOffset="0" />
  <End segmentByteOffset="3" segmentBitOffset="7" />
</Out>

<!-- Inputs: ISRA → KUKA -->
<In krcStartIndex="1705" segmentID="26">
  <Start segmentByteOffset="0" segmentBitOffset="0" />
  <End segmentByteOffset="3" segmentBitOffset="7" />
</In>
```

#### Текущая архитектура

```
┌─────────────────────────────────────────────────────────┐
│                    PROFINET сеть                         │
├─────────────┬──────────────┬──────────────┬─────────────┤
│    PLC      │  KRC4        │  cifX (IPC)  │  ISRA PC    │
│             │  (Device)    │  (Device)    │             │
│  IP: x.x.x │  PNIO-DEV    │  PNIO-CTRL   │  192.168.1.x│
└──────┬──────┴──────┬───────┴──────┬───────┴──────┬──────┘
       │             │              │              │
       │        $IN[1705..1719]     │         ISRA Camera
       │        $OUT[2793..2807]    │         (192.168.1.36)
       │             │              │              │
       │             │         cifX board      ISRA Software
       │             │         (Hilscher)     (BEADMASTER2D)
       │             │              │
       └─────────────┴──────────────┘
```

#### Новая архитектура (reuse конфигурации)

```
┌─────────────────────────────────────────────────────────┐
│                    PROFINET сеть                         │
├─────────────┬──────────────┬─────────────────────────────┤
│    PLC      │  KRC4        │  IPC (Python + cifX)        │
│             │  (Device)    │  (Device)                   │
│             │  PNIO-DEV    │  PNIO-CTRL                  │
└──────┬──────┴──────┬───────┴──────────────┬──────────────┘
       │             │                      │
       │        $IN[1705..1719]         Python
       │        $OUT[2793..2807]     ┌─────┴─────┐
       │             │               │ OpenCV     │
       │             │               │ Hikrobot   │
       │             │               │ + cifX API │
       │             │               └───────────┘
```

#### Что можно reuse-ить

| Файл | Использовать? | Почему |
|------|--------------|--------|
| `PNIODriver.xml` | **Да** | Имя устройства, тайминги, структура слотов |
| `KRC_IO.xml` | **Да** | Маппинг $IN/$OUT → PROFINET сегменты |
| `IPPNIO.xml` | **Да** | Слоты, модули, параметры обмена |
| GSDML-файл | **Нужно найти** | Описание устройства для PROFINET |
| SYCON.net проект | **Нужно найти** | Конфигурация cifX на IPC |

#### Что НЕЛЬЗЯ reuse-ить

| Что | Почему |
|-----|--------|
| Драйвер ISRA | Проприетарный, привязан к BEADMASTER2D |
| API ISRA | Не публичный |
| Конфигурация ISRA в SYCON.net | Содержит настройки только для ISRA |

#### Ключевой момент

**KRC4 не меняется.** Он продолжает читать `$IN[1705..1719]` и писать `$OUT[2793..2807]`. KRC4 будет думать, что общается с ISRA, но на самом деле будет общаться с Python-программой.

#### Шаги по реализации

1. **Найти на IPC файлы конфигурации cifX:**
   ```
   C:\Program Files\Hilscher\SYCON.net\  (или similar)
   ├── *.xrf  — проект конфигурации
   ├── *.xml  — GSDML/ширма
   └── *.bin  — прошивка
   ```

2. **Убедиться что имя PROFINET-устройства совпадает:**
   ```
   PNIODriver.xml → PNIODeviceName="h901-200rb-100-kf084.m489g020s20-121-ml-3"
   ```
   Это имя должно совпадать с тем, что настроено в SYCON.net для cifX.

3. **Использовать те же слоты и I/O:**
   ```python
   # Входы ISRA (от KUKA): $IN[1705..1719]
   # = 15 дискретных сигналов = 2 байта (segmentID=26)

   # Выходы ISRA (к KUKA): $OUT[2793..2807]
   # = 15 дискретных сигналов = 2 байта (segmentID=27)
   ```

4. **Не менять KRC4-программу.** KRC4 продолжает работать как раньше.

#### Преимущества

- **Минимум изменений в KRC4** — не нужно менять DAI_KNI.SRC
- **Нет задержек** — PROFINET RT, цикл 8 мс
- **Надёжность** — watchdog встроен в PROFINET
- **Тот же протокол** — KRC4 не заменит подмену

#### Недостатки

- Требуется доступ к SYCON.net и cifX API
- Нужно найти GSDML-файл для cifX
- Требуется ctypes/C++ для работы с cifX API

#### Python-клиент (cifX)

> **Важно:** Пример ниже упрощён. Для Production-версии с учётом Endianness, Heartbeat и timer resolution см. раздел **«Критические замечания по реализации cifX»** в конце документа.

```python
import ctypes
import struct
import time

class CifXCommunicator:
    def __init__(self):
        self.cifx = None
        self.channel = None

    def connect(self):
        # Загружаем cifX API
        self.cifx = ctypes.WinDLL("cifXAPI.dll")

        # Инициализация
        result = self.cifx.cifXOpen(
            b"cifX0",  # имя устройства
            None, None, None
        )
        self.channel = self.cifx.cifXChannelOpen(
            result, 0, 0
        )

    def read_inputs(self):
        """Читаем $IN[1705..1719] (2 байта)"""
        buf = (ctypes.c_char * 2)()
        self.cifx.cifXRead(
            self.channel,
            buf,
            2,
            ctypes.byref(ctypes.c_int(0))
        )
        return struct.unpack('<H', buf)[0]

    def write_outputs(self, value):
        """Пишем $OUT[2793..2807] (2 байта)"""
        buf = struct.pack('<H', value)
        self.cifx.cifXWrite(
            self.channel,
            buf,
            2,
            ctypes.byref(ctypes.c_int(0))
        )

    def disconnect(self):
        if self.channel:
            self.cifx.cifXChannelClose(self.channel)
```

#### Использование в Python-программе

> **Упрощённый пример.** Полная версия с Endianness, Heartbeat и precision sleep — в разделе «Критические замечания по реализации cifX».

```python
comm = CifXCommunicator()
comm.connect()

while True:
    # Читаем команду от KRC4
    inputs = comm.read_inputs()

    trigger = inputs & 0x01       # бит 0 = O_ACQ
    start = (inputs >> 1) & 0x01  # бит 1 = O_START

    if trigger and start:
        # Выполняем измерение
        img = camera.capture()
        status, width = process_seam(img)

        # Записываем результат в PROFINET
        outputs = 0
        if status == "OK":
            outputs |= 0x01  # I_OK
        else:
            outputs |= 0x02  # I_NOK

        comm.write_outputs(outputs)

    time.sleep(0.001)  # 1 мс цикл
```

---

## Что НЕЛЬЗЯ трогать

1. **DAI_KLB_INTEC** — управление клеевым дозатором Intec. Не связана с ISRA напрямую.
2. **DAI_CheckTool** — калибровка инструмента. Не связана с ISRA.
3. **BrakeTest** — тест тормозов. Не связана.
4. **P00** — внешний интерфейс PLC (программный номер). Не трогать.

## Что нужно изменить

### Если выбран PROFINET через cifX (рекомендуется)

1. **KRC4: НЕ ИЗМЕНЯТЬ** — продолжает работать как раньше
2. **IPC: Заменить ISRA на Python-программу:**
   - Установить cifX API
   - Реализовать CifXCommunicator
   - Подключить камеру Hikrobot
   - Реализовать обработку изображений
3. **Настроить PROFINET:**
   - Убедиться что имя устройства совпадает
   - Проверить слоты и I/O

### Если выбран OpenShowVar

1. **Заменить DAI_KNI.SRC** — вместо вызовов ISRA использовать чтение/запись переменных
2. **Добавить переменные** в `$config.dat` или отдельный DAT-файл
3. **Установить KUKAVARPROXY** на KRC4
4. **Модифицировать main.src** — заменить вызовы DAI_KNI на чтение/запись переменных

### Если выбран EthernetKRL

1. **Настроить EthernetKRL.xml** — добавить канал
2. **Добавить переменные** в KRL
3. **Модифицировать main.src** — использовать EKI_Get*/EKI_Set*
4. **Написать Python-клиент** — TCP соединение

---

## Критические замечания по реализации cifX

### 1. Endianness (порядок байтов)

KUKA использует **Big-Endian (Motorola)** для передачи данных по PROFINET. x86 IPC и Python используют **Little-Endian**. Неправильная распаковка приведёт к перепутыванию битов.

**Решение:**
```python
import struct

# Чтение из PROFINET (Big-Endian) → Python (Little-Endian)
inputs_raw = comm.read_raw(2)  # 2 байта
inputs = struct.unpack('>H', inputs_raw)[0]  # Big-Endian → int

# Запись в PROFINET (Big-Endian) ← Python (Little-Endian)
outputs = 0x01 | (life_bit << 10)
packed = struct.pack('>H', outputs)  # int → Big-Endian bytes
comm.write_raw(packed)
```

**Или ручной swap:**
```python
def swap_bytes(val):
    return ((val & 0xFF) << 8) | ((val >> 8) & 0xFF)
```

### 2. Life-bit (Heartbeat / Watchdog)

KUKA ожидает, что бит `I_LIFE` будет инвертироваться с определённой частотой (обычно 100–500 мс). Если бит не «мигает», KRC4 уходит в ошибку связи.

**Решение:**
```python
import time

HEARTBEAT_INTERVAL = 0.25  # 250 мс
life_bit = 0
last_heartbeat = time.time()

while True:
    current_time = time.time()
    if current_time - last_heartbeat >= HEARTBEAT_INTERVAL:
        life_bit = 1 - life_bit  # инвертируем 0→1→0→1
        last_heartbeat = current_time

    # Формируем выходные данные
    outputs = 0
    outputs |= (life_bit << 10)  # I_LIFE на бите 10

    comm.write_outputs(outputs)
    time.sleep(0.01)
```

### 3. Тайминги Windows (GIL и timer resolution)

Стандартный `time.sleep()` в Windows имеет разрешение **~15.6 мс** (системный таймер). Для PROFINET с циклом 8 мс это критично.

**Решения:**

| Метод | Точность | Сложность |
|-------|----------|-----------|
| `time.sleep(0.001)` | ~15 мс | Низкая |
| `time.perf_counter()` + busy-wait | ~0.1 мс | Средняя |
| `ctypes.windll.kernel32.QueryPerformanceCounter` | ~0.01 мс | Средняя |
| Вынос поллинга в C++ поток | ~1 мс | Высокая |
| Использование `multimedia` таймера | ~1 мс | Средняя |

**Busy-wait с high-precision таймером:**
```python
import time

def precise_sleep(seconds):
    """Busy-wait с точностью ~0.1 мс"""
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        pass

# Использование
while True:
    # ... обработка ...
    precise_sleep(0.008)  # 8 мс с высокой точностью
```

**Или установка timer resolution (однократно):**
```python
import ctypes

# Установить разрешение таймера на 1 мс
timeBeginPeriod = ctypes.windll.winmm.timeBeginPeriod
timeEndPeriod = ctypes.windll.winmm.timeEndPeriod

timeBeginPeriod(1)  # 1 мс разрешение
try:
    while True:
        time.sleep(0.008)  # теперь ~8 мс
finally:
    timeEndPeriod(1)  # восстановить
```

### 4. Обновлённый пример cifX с учётом всех замечаний

```python
import ctypes
import struct
import time
import logging

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 0.25  # 250 мс
POLL_INTERVAL = 0.008      # 8 мс (PROFINET cycle)

# Bit positions (ISRA mapping)
I_READY    = 0
I_ACK      = 1
I_OK       = 2
I_NOK      = 3
I_ERROR    = 4
I_LIFE     = 10
I_WARN     = 11

O_ACQ      = 0
O_START    = 1
O_RESET    = 2
O_TYPE_0   = 3  # 7 бит: O_TYPE[0..6]
O_TYPE_6   = 9


class CifXCommunicator:
    def __init__(self):
        self.cifx = None
        self.handle = None
        self.channel = None
        self.life_bit = 0
        self.last_heartbeat = time.time()

    def connect(self):
        self.cifx = ctypes.WinDLL("cifXAPI.dll")

        result = self.cifx.cifXOpen(b"cifX0", None, None, None)
        if result != 0:
            logger.error(f"cifXOpen failed: {result}")
            return False

        self.channel = self.cifx.cifXChannelOpen(result, 0, 0)
        if not self.channel:
            logger.error("cifXChannelOpen failed")
            return False

        logger.info("cifX подключено")
        return True

    def _swap_bytes(self, val):
        """Big-Endian ↔ Little-Endian swap"""
        return ((val & 0xFF) << 8) | ((val >> 8) & 0xFF)

    def read_inputs(self):
        """Читаем $IN[1705..1719] с правильным endianness"""
        buf = (ctypes.c_char * 2)()
        self.cifx.cifXRead(self.channel, buf, 2, ctypes.byref(ctypes.c_int(0)))
        raw = struct.unpack('<H', buf)[0]
        return self._swap_bytes(raw)  # Big-Endian → int

    def write_outputs(self, value):
        """Пишем $OUT[2793..2807] с правильным endianness"""
        swapped = self._swap_bytes(value)  # int → Big-Endian
        buf = struct.pack('<H', swapped)
        self.cifx.cifXWrite(self.channel, buf, 2, ctypes.byref(ctypes.c_int(0)))

    def update_heartbeat(self):
        """Инвертируем life_bit каждые 250 мс"""
        now = time.time()
        if now - self.last_heartbeat >= HEARTBEAT_INTERVAL:
            self.life_bit = 1 - self.life_bit
            self.last_heartbeat = now

    def disconnect(self):
        if self.channel:
            self.cifx.cifXChannelClose(self.channel)
        logger.info("cifX отключено")


# === Основной цикл ===

def main():
    comm = CifXCommunicator()
    if not comm.connect():
        return

    # Установить timer resolution на 1 мс
    ctypes.windll.winmm.timeBeginPeriod(1)

    try:
        while True:
            # 1. Обновить heartbeat
            comm.update_heartbeat()

            # 2. Прочитать входы от KUKA
            inputs = comm.read_inputs()

            o_acq   = (inputs >> O_ACQ) & 1
            o_start = (inputs >> O_START) & 1

            # 3. Сформировать базовый ответ
            outputs = 0
            outputs |= (1 << I_READY)         # I_READY = TRUE
            outputs |= (comm.life_bit << I_LIFE)  # Heartbeat

            # 4. Обработка команды
            if o_acq and o_start:
                outputs |= (1 << I_ACK)  # Подтверждение

                img = camera.capture()
                if img is not None:
                    ok, width, msg, bbox = processor.process(img)

                    if ok:
                        outputs |= (1 << I_OK)
                    else:
                        outputs |= (1 << I_NOK)

            # 5. Записать выходы
            comm.write_outputs(outputs)

            # 6. Точный сон (8 мс)
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        pass
    finally:
        ctypes.windll.winmm.timeEndPeriod(1)
        comm.disconnect()
```

### 5. Порядок битов (сверить с KRC_IO.xml)

Перед запуском **обязательно** сверить маппинг битов с файлами конфигурации:

```
PNIODriver.xml  → InputDataLength, OutputDataLength (размер данных)
KRC_IO.xml      → segmentBitOffset для каждого сигнала
$machine.dat    → привязка $IN[1705..1719] / $OUT[2793..2807]
DAI_KNI.SRC     → реальное использование сигналов в KRL
```

---

## Итоговая рекомендация

| Сценарий | Вариант | Причина |
|----------|---------|---------|
| **Production (рекомендуется)** | **PROFINET через cifX** | **Reuse конфигурации, минимум изменений в KRC4, RT 8 мс** |
| Быстрый прототип | OpenShowVar | Минимум изменений, работает за час |
| Production (без лицензии) | OpenShowVar | Простота, доступность |
| Production (с лицензией) | EthernetKRL | Типизация, встроенная библиотека |
| Тестовый стенд | Modbus TCP | Уже настроен на ОВЕН МК210 |

---

## Следующие шаги

### Если выбран PROFINET через cifX (рекомендуется)

1. **Найти на IPC:**
   - SYCON.net проект (*.xrf)
   - GSDML-файл для cifX
   - cifX API (DLL)

2. **Проверить совпадение имён:**
   - `PNIODriver.xml` → `PNIODeviceName`
   - SYCON.net → Device Name

3. **Установить Python-обёртку:**
   - `cifx` (если есть Python SDK)
   - Или ctypes + cifXAPI.dll

4. **Реализовать CifXCommunicator:**
   - `connect()` — инициализация
   - `read_inputs()` — чтение $IN[1705..1719]
   - `write_outputs()` — запись $OUT[2793..2807]
   - `disconnect()` — отключение

5. **Не менять KRC4** — продолжает работать как раньше

6. **Протестировать на стенде**

### Если выбран OpenShowVar (быстрый прототип)

1. Установить KUKAVARPROXY на KRC4
2. Добавить переменные в $config.dat
3. Модифицировать main.src
4. Написать Python-клиент
5. Протестировать
