# AGENTS.md

## Project Overview

Replace ISRA BEADMASTER2D seam inspection system with custom Python/OpenCV solution for KUKA KR200 robot cell.

**Target environment**: Windows 7 64-bit, Python 3.8.10 (last version supporting Windows 7), Hikrobot camera, KUKA KRC4 (KSS 8.3)
**Development note**: Project runs on Python 3.10+ locally but must remain compatible with 3.8.10 for deployment

## Repository Structure

```
tvrus/
├── main.py              # Entry point, main inspection loop
├── camera.py            # Hikrobot camera (Modbus trigger, folder read)
├── image_processor.py   # Seam width measurement (OpenCV)
├── roi_selector.py      # Interactive ROI selection
├── config.json          # Central configuration
├── requirements.txt     # Python dependencies
├── communication/
│   ├── __init__.py
│   ├── base.py          # Abstract base class RobotCommunicator
│   ├── factory.py       # Factory for backend selection
│   ├── modbus.py        # Modbus TCP (OWEN MK210)
│   ├── openshowvar.py   # OpenShowVar (KUKAVARPROXY)
│   └── simulation.py    # Simulation mode
├── data/
│   ├── images/foto/NG/  # Camera images
│   └── results/         # Saved inspection results
├── ml121_200rb_100/     # KUKA KRC4 backup - DO NOT MODIFY
└── venv/                # Python 3.8.10 virtual environment
```

## OWEN MK210 I/O Mapping

### Inputs (DI)

| DI | Function |
|----|----------|
| DI0 | Sensor D1 (conveyor entry) |
| DI1 | Sensor D2 (under camera, left) |
| DI2 | Sensor D3 (under camera, right) |
| DI3 | Sensor D4 (conveyor exit) |
| DI4 | Toggle switch (position A — start) |
| DI5 | Toggle switch (position B — end) |
| DI6 | Sensor D6 (capture / scenario B) |

### Outputs (DO)

| DO | Function |
|----|----------|
| DO0 | Conveyor (start/stop) |
| DO1 | Reject pusher (reserved) |
| DO2 | NG lamp (red) |
| DO3 | OK lamp (green) |

### Modbus Registers

- **Input register**: 51 (DI bits read)
- **Output register**: 470 (DO bits write)
- **DI bit mapping**: di04=4, di05=5, di06=6
- **DO bit mapping**: do00=0, do01=1, do02=2, do03=3

## Key Technical Facts

### Communication (Critical)

- **Test stand**: Modbus TCP with OWEN MK210 (192.168.1.99:502)
- **Camera trigger**: Modbus TCP with Hikrobot camera (192.168.1.36:502)
- **Alternative**: OpenShowVar (KUKAVARPROXY, port 7000)
- **PROFINET**: Placeholder in config/factory (not implemented)
- **ISRA I/O mapping**: `$IN[1705..1719]` (inputs), `$OUT[2793..2807]` (outputs)
- **KRC4 MUST NOT be changed** - it will communicate with Python program as if it were ISRA
- **Factory**: `communication/factory.py` selects mode from `config.json` (`modbus`, `openshowvar`, `simulation`, `profinet`)

### Hardware

- Camera: Hikrobot MV-CS series, GigE Vision, IP 192.168.1.36
- Industrial PC: Siemens SIMATIC IPC547G (i5-6500, 16GB)
- Target: FOV 100×100mm, working distance 100-120mm, seam width 1-6mm

### Dependencies

```
opencv-python
numpy
pymodbus
PyInstaller
```

Note: `pyopenshowvar` is NOT on PyPI. OpenShowVar communicator uses raw socket implementation instead.

## Inspection Cycle

1. Wait for DI04 (Toggle A — start)
2. Wait for DI06 (Sensor D6 — capture trigger)
3. Send Modbus trigger to camera
4. Camera saves JPEG to shared folder
5. Read latest image from folder
6. Process: ROI crop → CLAHE → Blur → Otsu → Morphology → Contours
7. Measure seam width: `width_mm = min(w, h) / px_per_mm`
8. OK if `min_width_mm ≤ width_mm ≤ max_width_mm`
9. Send result: DO3 (OK green) or DO2 (NG red)
10. Wait for DI05 (Toggle B — end)
11. Reset all outputs, return to step 1

## Development Commands

```bash
# Create virtual environment
python -m venv venv
venv\Scripts\activate

# PowerShell: Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned

# Install dependencies
pip install -r requirements.txt

# Run application
python main.py

# Run ROI selection mode
python main.py --roi

# Build executable
pyinstaller --onefile main.py
```

## Critical Constraints

- Python 3.8.10 (last version supporting Windows 7)
- MVS SDK 5.x (6.x doesn't support Windows 7)
- Processing cycle < 0.8 seconds (robot speed 125mm/s)
- Calibration required: `px_per_mm = pixels / real_mm`

## DO NOT

- Modify files in `ml121_200rb_100/` - this is a KUKA backup
- Change KRC4 programs (`DAI_KNI.SRC`, `HP001.src`, etc.)
- Use Python versions > 3.8.10
- Use MVS SDK > 5.x