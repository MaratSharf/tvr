# AGENTS.md

## Project Overview

Replace ISRA BEADMASTER2D seam inspection system with custom Python/OpenCV solution for KUKA KR200 robot cell.

**Target environment**: Windows 7 64-bit, Python 3.8.10 (last version supporting Windows 7), Hikrobot camera, KUKA KRC4 (KSS 8.3)
**Development note**: Project runs on Python 3.10+ locally but must remain compatible with 3.8.10 for deployment

## Repository Structure

```
tvrus/
├── main.py              # Entry point, main inspection loop (--gui launches GUI)
├── gui.py               # Tkinter GUI: start/stop, video, ROI editor, settings, log
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
│   ├── profinet.py      # PROFINET/cifX (KRC4, ISRA protocol)
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
- **PROFINET**: `communication/profinet.py` (cifX/ISRA protocol), requires Hilscher cifX DLL
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
6. Process image (detailed algorithm in `ALGORITHM_DESCRIPTION.md`):
   - ROI crop → grayscale → contrast check (reject empty frames)
   - CLAHE (clipLimit=2.5) → GaussianBlur → binarization (Otsu or manual threshold, INV)
   - Ignore-mask masking → auto-mask of central through-hole → MORPH_CLOSE (3×3) → fill glare holes
   - Centerline: annotated polyline from etalon (uniformly resampled at `step_px`) OR automatic `cv2.fitLine` on largest contour
   - N cross-slices (smoothed normals) at `step_px` intervals along centerline
   - Measure width per slice: white runs → merge glare-split runs → best run by corridor+score → `width_mm = width_px / px_per_mm`
   - Median filtering of widths (window 3) before aggregation
   - Gap detection: empty slice or width < `gap_min_width_mm`
7. Defect criteria (bitmask, see `SeamProcessor`):
   - `ERR_GAP` (1): max gap > `max_allowed_gap_mm` or fill_ratio < `min_fill_ratio`
   - `ERR_WID` (2): min width < `min_width_mm` or max width > `max_width_mm`
   - `ERR_POS` (4): 90th percentile |offset_mm| > `pos_tolerance_mm`
   - `ERR_NO_SEAM` (8): all slices are gaps, centerline not found, or low contrast
8. Send result: DO3 (OK green) or DO2 (NG red) if `error_code == ERR_NONE`
9. Wait for DI05 (Toggle B — end)
10. Reset all outputs, return to step 1

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

# Run GUI (recommended)
python gui.py
python main.py --gui

# Run ROI selection mode
python main.py --roi

# Build executable
pyinstaller --onefile main.py
```

## GUI (gui.py)

- Tkinter (Python standard library, no extra dependencies). Launch: `python gui.py` or `python main.py --gui`.
- Inspection loop runs in a worker thread; frames/status/results reach Tk via queues (`GuiReporter`, `QueueLogHandler`).
- `run_*_mode` functions in `main.py` accept optional `stop_event` (DI/trigger waits chunked at 0.5 s) and `reporter`; without them behavior is unchanged (console).
- Stop button: conveyor OFF, `reset_outputs`, no result sent (cycle abort, not a failure).
- ROI and settings are written to config.json atomically and applied to the live `SeamProcessor`.

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