"""
Калибровка px_per_mm по шахматной доске.

Запуск:
    python calibrate.py                        # триггер камеры, свежий кадр
    python calibrate.py path/to/image.jpg      # существующий файл
    python calibrate.py --square-mm 10 --pattern 9x6

Находит шахматный узор, измеряет средний размер клетки в пикселях и
записывает px_per_mm в config.json (атомарно).
"""
import sys
import os
import json
import glob
import time
import argparse
import logging

import cv2
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger("calibrate")


def load_config(path=os.path.join(BASE_DIR, 'config.json')):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def save_config_atomically(config, path):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)


def latest_folder_image(camera_cfg):
    folder = camera_cfg.get('image_folder', '')
    if not folder or not os.path.isdir(folder):
        return None
    candidates = []
    for ext in ('*.jpg', '*.jpeg', '*.png', '*.bmp'):
        candidates.extend(glob.glob(os.path.join(folder, ext)))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def grab_fresh_image(camera_cfg, timeout_s=10.0):
    """Триггер камеры + ожидание нового файла в папке."""
    from camera import HikrobotCamera
    cfg = dict(camera_cfg)
    cfg['image_folder'] = camera_cfg.get('image_folder', '')
    cam = HikrobotCamera(cfg)
    if not cam.connect():
        logger.error("Camera connect failed")
        return None
    try:
        before = set()
        folder = cfg.get('image_folder', '')
        if os.path.isdir(folder):
            before = set(os.listdir(folder))
        if not cam.trigger_capture():
            logger.error("Camera trigger failed")
            return None
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(0.1)
            new = [f for f in os.listdir(folder)
                   if f not in before and f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp'))]
            if new:
                path = os.path.join(folder, max(new, key=lambda f: os.path.getmtime(os.path.join(folder, f))))
                return cv2.imread(path)
        logger.error("No new image within %.1fs", timeout_s)
        return None
    finally:
        try:
            cam.disconnect()
        except Exception:
            pass


def detect_chessboard(gray, rng=(4, 12)):
    """Перебор типоразмеров узора; возвращает (w, h, corners) лучшего найденного."""
    flags = (cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE +
             cv2.CALIB_CB_FILTER_QUADS)
    found = []
    for w in range(rng[0], rng[1] + 1):
        for h in range(w, rng[1] + 1):
            ok, corners = cv2.findChessboardCorners(gray, (w, h), flags)
            if ok:
                crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 60, 1e-3)
                corners = cv2.cornerSubPix(gray, corners, (5, 5), (-1, -1), crit)
                c = corners.reshape(w * h, 2).reshape(h, w, 2)
                d_h = np.linalg.norm(np.diff(c, axis=1), axis=2)
                d_v = np.linalg.norm(np.diff(c, axis=0), axis=2)
                spacing = float(np.median(np.concatenate([d_h.ravel(), d_v.ravel()])))
                # чем больше узор и ровнее шаг, тем достовернее
                jitter = float(np.median(np.abs(
                    np.concatenate([d_h.ravel(), d_v.ravel()]) - spacing)))
                found.append((jitter, (w, h), corners, spacing))
    if not found:
        return None
    found.sort(key=lambda t: t[0])
    best = found[0]
    return best[1], best[2], best[3]


def calibrate_from_image(image, square_mm, annotated_path=None):
    """Вернуть (px_per_mm, метаданные) или (None, причина)."""
    if image is None or image.size == 0:
        return None, 'empty image'
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    det = detect_chessboard(gray)
    if det is None:
        return None, 'chessboard not found'
    (pw, ph), corners, spacing = det
    if spacing <= 0:
        return None, 'degenerate corner spacing'
    px_per_mm = spacing / square_mm

    meta = {
        'pattern': [pw, ph],
        'corners': int(len(corners)),
        'square_px': round(spacing, 3),
        'square_mm': square_mm,
        'px_per_mm': round(px_per_mm, 4),
    }

    if annotated_path:
        vis = image.copy()
        cv2.drawChessboardCorners(vis, (pw, ph), corners, True)
        cv2.putText(vis, 'px_per_mm=%.3f (%.1fpx = %.1fmm)' % (px_per_mm, spacing, square_mm),
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        try:
            cv2.imwrite(annotated_path, vis)
        except OSError as e:
            logger.warning("Cannot save annotated image: %s", e)
    return px_per_mm, meta


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(description='Калибровка px_per_mm по шахматной доске')
    parser.add_argument('image', nargs='?', default=None, help='путь к изображению (по умолчанию: свежий кадр камеры)')
    parser.add_argument('--square-mm', type=float, default=10.0, help='физический размер клетки, мм (по умолчанию 10)')
    parser.add_argument('--no-save', action='store_true', help='не изменять config.json')
    parser.add_argument('--annotated', default=os.path.join(BASE_DIR, 'data', 'results', 'calibration.png'),
                        help='куда сохранить кадр с разметкой')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

    config_path = os.path.join(BASE_DIR, 'config.json')
    config = load_config(config_path)
    camera_cfg = config.get('camera', {})

    if args.image:
        image = cv2.imread(args.image)
        if image is None:
            print(f"FATAL: cannot read image: {args.image}")
            return 1
    else:
        print("Capturing fresh image from camera (trigger sent)...")
        image = grab_fresh_image(camera_cfg)
        if image is None:
            # фолбэк на последний файл в папке камеры
            fallback = latest_folder_image(camera_cfg)
            if fallback is None:
                print("FATAL: no image available (no trigger, no folder files)")
                return 1
            print(f"Trigger failed or no capture - falling back to latest folder image: {fallback}")
            image = cv2.imread(fallback)
            if image is None:
                print(f"FATAL: cannot read image: {fallback}")
                return 1

    px_per_mm, meta = calibrate_from_image(image, args.square_mm, args.annotated)
    if px_per_mm is None:
        print(f"FATAL: calibration failed: {meta}")
        return 1

    print(f"Pattern           : {meta['pattern'][0]}x{meta['pattern'][1]} inner corners "
          f"({meta['corners']} points)")
    print(f"Square size       : {meta['square_px']:.2f} px = {meta['square_mm']:.2f} mm")
    print(f"px_per_mm         : {meta['px_per_mm']}")

    if args.no_save:
        print("config.json not modified (--no-save)")
        return 0

    old = float(config.get('seam_control', {}).get('px_per_mm', 0.0) or 0.0)
    if old and abs(old - px_per_mm) / px_per_mm > 0.25:
        print(f"WARNING: previous value {old} differs significantly - check pattern setup")

    config.setdefault('seam_control', {})['px_per_mm'] = meta['px_per_mm']
    save_config_atomically(config, config_path)
    print(f"Saved: {config_path} seam_control.px_per_mm = {meta['px_per_mm']}")
    print(f"Annotated image : {args.annotated}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
