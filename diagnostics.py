"""Диагностический режим: сохранение полного следа инспекции на диск.

Комплект сохраняется в папку data/diagnostics/<timestamp>/:
    00_original.png   — исходный кадр камеры;
    01_roi.png        — обрезанный по ROI серый кадр;
    02_seg_raw.png    — бинарная маска сразу после бинаризации;
    03_seg_masked.png — маска после ignore-зон и маскирования отверстия;
    04_seg_final.png  — маска, по которой шла измерительная обработка;
    05_centerline.png — центральная линия поверх ROI;
    06_boundaries.png — обе границы каждого среза разными цветами;
    07_annotated.png  — итоговая разметка (как отправляется в GUI/дисплей);
    report.json       — все измерения по срезам и агрегаты (сырые данные).
"""

import json
import os
import shutil
import tempfile
from datetime import datetime

import cv2
import numpy as np


_STATUS_COLORS = {
    'ok': (0, 255, 0),
    'wid': (0, 0, 255),
    'gap': (0, 0, 255),
    'invalid': (0, 128, 255),
}

# отбираемые поля среза, попадающие в report.json (сырые измерения)
_SLICE_FIELDS = (
    'index', 'status', 'valid', 'is_gap', 'invalid', 'excluded',
    'width_px', 'width_mm', 'offset_px', 'offset_mm',
    'confidence', 'reason', 'left_t', 'right_t',
    'bg', 'inside_mean', 'darkness', 'edge_step_left', 'edge_step_right',
)


def _make_overlay(roi_bgr, centerline_local, slices, draw_edges=False, offsets=(0, 0)):
    """Наложение оси/границ на ROI-кадр (локальные координаты)."""
    canvas = roi_bgr.copy()
    ox, oy = offsets
    if centerline_local:
        pts = [(int(round(p[0] + ox)), int(round(p[1] + oy))) for p in centerline_local]
        for i in range(len(pts) - 1):
            cv2.line(canvas, pts[i], pts[i + 1], (0, 255, 255), 1)
    for sl in slices:
        color = _STATUS_COLORS.get(sl.get('status', 'invalid'), (0, 128, 255))
        center = sl.get('center')
        if not (isinstance(center, (list, tuple)) and len(center) >= 2):
            continue
        cx, cy = int(round(center[0])), int(round(center[1]))
        nx, ny = sl.get('normal', (1.0, 0.0))
        half = max(8, int(round((sl.get('width_px') or 20.0) * 0.75)))
        t1 = (float(sl.get('left_t', 0.0)), float(sl.get('right_t', 0.0)))
        p1 = (int(round(cx + t1[0] * nx)), int(round(cy + t1[0] * ny)))
        p2 = (int(round(cx + t1[1] * nx)), int(round(cy + t1[1] * ny)))
        cv2.line(canvas, p1, p2, color, 1)
        if draw_edges and sl.get('valid') and sl.get('edge1') and sl.get('edge2'):
            cv2.circle(canvas, p1, 2, (255, 0, 255), -1)
            cv2.circle(canvas, p2, 2, (255, 255, 255), -1)
    return canvas


def _round_all(obj):
    if isinstance(obj, float):
        return round(obj, 4)
    if isinstance(obj, dict):
        return {k: _round_all(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_all(v) for v in obj]
    return obj


def save_diagnostics(image, processor, detail, base_dir=None, keep=None):
    """
    Сохранить диагностиеский комплект инспекции.

    image     — исходный полный кадр;
    processor — SeamProcessor (используются save_diagnostics/diagnostics_dir);
    detail    — результат process(); должен содержать 'debug' при включённом
                диагностическом режиме.
    Возвращает путь к папке комплекта или None.
    """
    if detail is None:
        detail = processor.last_detail or {}
    if not processor.save_diagnostics:
        return None

    if base_dir is None:
        base_dir = processor.diagnostics_dir or 'data/diagnostics'
    if not os.path.isabs(base_dir):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        base_dir = os.path.join(script_dir, base_dir)
    os.makedirs(base_dir, exist_ok=True)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
    folder = os.path.join(base_dir, 'diag_%s_%s' % (
        ts, detail.get('state', 'NONE')))
    os.makedirs(folder, exist_ok=True)

    debug = detail.get('debug') or {}

    def _write(name, img):
        if img is None or img.size == 0:
            return
        cv2.imwrite(os.path.join(folder, name), img)

    _write('00_original.png', image)
    _write('01_roi.png', debug.get('roi_bgr'))
    _write('02_seg_raw.png', debug.get('seg_raw'))
    _write('03_seg_masked.png', debug.get('seg_masked'))
    _write('04_seg_final.png', debug.get('seg_final'))
    if debug.get('roi_bgr') is not None:
        offsets = (debug.get('offset_x', 0), debug.get('offset_y', 0))
        _write('05_centerline.png', _make_overlay(
            debug['roi_bgr'], debug.get('centerline_local'),
            debug.get('slices') or detail.get('slices', []), False,
            offsets=(0, 0)))
        _write('06_boundaries.png', _make_overlay(
            debug['roi_bgr'], None,
            debug.get('slices') or detail.get('slices', []), True, offsets=(0, 0)))
    try:
        annotated = processor.draw_result(image, detail=detail)
    except Exception:
        annotated = None
    _write('07_annotated.png', annotated)

    allowable = detail.get('allowable') or {}
    calib = detail.get('calibration') or {}
    slices_out = []
    for sl in detail.get('slices', []):
        rec = {k: sl.get(k) for k in _SLICE_FIELDS}
        rec['center_global_xy'] = list(sl.get('center') or [])
        rec['normal_xy'] = list(sl.get('normal') or [])
        rec['edge1_global_xy'] = (list(sl['edge1']) if sl.get('edge1') else None)
        rec['edge2_global_xy'] = (list(sl['edge2']) if sl.get('edge2') else None)
        slices_out.append(rec)
    report = {
        'timestamp': ts,
        'state': detail.get('state'),
        'ok': detail.get('ok'),
        'error_code': detail.get('error_code'),
        'error_messages': detail.get('error_messages', []),
        'aggregates': {
            'mean_width_mm': detail.get('mean_width_mm'),
            'median_width_mm': detail.get('median_width_mm'),
            'min_width_mm': detail.get('min_width_mm'),
            'max_width_mm': detail.get('max_width_mm'),
            'std_width_mm': detail.get('std_width_mm'),
            'mean_filtered_mm': detail.get('mean_filtered_mm'),
            'max_gap_mm': detail.get('max_gap_mm'),
            'fill_ratio': detail.get('fill_ratio'),
            'checked_slices': detail.get('checked_slices'),
            'valid_slices': detail.get('valid_slices'),
            'gap_slices': detail.get('gap_slices'),
            'excluded_slices': detail.get('excluded_slices'),
            'expected_length_mm': detail.get('expected_length_mm'),
        },
        'widths_raw_mm': detail.get('widths_raw_mm', []),
        'allowable': allowable,
        'calibration': calib,
        'slices': slices_out,
    }
    report_path = os.path.join(folder, 'report.json')
    fd, tmp = tempfile.mkstemp(dir=folder, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(_round_all(report), f, indent=2, ensure_ascii=False)
        os.replace(tmp, report_path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    _rotate_folders(base_dir, keep=keep if keep is not None else processor.diagnostics_keep)
    return folder


def _rotate_folders(base_dir, keep):
    if not keep or keep <= 0 or not os.path.isdir(base_dir):
        return
    entries = sorted(
        (os.path.join(base_dir, e) for e in os.listdir(base_dir)
         if e.startswith('diag_') and os.path.isdir(os.path.join(base_dir, e))),
    )
    for path in entries[:-keep]:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
