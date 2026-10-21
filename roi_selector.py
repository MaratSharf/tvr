import cv2
import numpy as np


class ROISelector:
    """Выбор зоны сканирования (ROI) мышью на изображении."""

    def __init__(self, window_name="Select ROI"):
        self.window_name = window_name
        self.roi = None
        self._drawing = False
        self._start = None
        self._end = None
        self._img = None
        self._overlay = None

    def _mouse_callback(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self._drawing = True
            self._start = (x, y)
            self._end = (x, y)

        elif event == cv2.EVENT_MOUSEMOVE and self._drawing:
            self._end = (x, y)
            self._draw_overlay()

        elif event == cv2.EVENT_LBUTTONUP:
            self._drawing = False
            self._end = (x, y)
            self._normalize()
            self._draw_overlay()

    def _normalize(self):
        if self._start and self._end:
            x1 = min(self._start[0], self._end[0])
            y1 = min(self._start[1], self._end[1])
            x2 = max(self._start[0], self._end[0])
            y2 = max(self._start[1], self._end[1])
            self._start = (x1, y1)
            self._end = (x2, y2)

    def _draw_overlay(self):
        self._overlay = self._img.copy()
        if self._start and self._end:
            cv2.rectangle(self._overlay, self._start, self._end, (0, 255, 0), 2)
            w = abs(self._end[0] - self._start[0])
            h = abs(self._end[1] - self._start[1])
            label = f"ROI: {w}x{h}px"
            cv2.putText(self._overlay, label,
                        (self._start[0], self._start[1] - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
        cv2.imshow(self.window_name, self._overlay)

    def select(self, image):
        """
        Показывает изображение и позволяет выбрать прямоугольную зону.
        Возвращает (x, y, w, h) или None при отмене (Escape).
        """
        self._img = image.copy()
        self.roi = None
        self._start = None
        self._end = None
        self._drawing = False

        cv2.namedWindow(self.window_name, cv2.WINDOW_NORMAL)
        cv2.setMouseCallback(self.window_name, self._mouse_callback)
        cv2.imshow(self.window_name, self._img)

        print("Draw ROI with mouse. Press ENTER to confirm, ESC to cancel.")

        while True:
            key = cv2.waitKey(30) & 0xFF
            if key == 27:  # ESC - отмена
                cv2.destroyWindow(self.window_name)
                return None
            elif key == 13 or key == 10:  # ENTER - подтверждение
                if self._start and self._end:
                    x1, y1 = self._start
                    x2, y2 = self._end
                    if x2 - x1 < 2 or y2 - y1 < 2:
                        print("ROI too small - drag a rectangle, ENTER to confirm, ESC to cancel")
                    else:
                        self.roi = (x1, y1, x2 - x1, y2 - y1)
                        cv2.destroyWindow(self.window_name)
                        return self.roi
                else:
                    cv2.destroyWindow(self.window_name)
                    return None

    def draw_roi(self, image, roi, color=(0, 255, 0), label=None):
        """Рисует ROI на изображении."""
        result = image.copy()
        if roi:
            x, y, w, h = roi
            cv2.rectangle(result, (x, y), (x + w, y + h), color, 2)
            if label:
                cv2.putText(result, label, (x, y - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        return result


class ROIManager:
    """Управление ROI: загрузка/сохранение, отображение."""

    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        self.roi = None

    def load_from_config(self, config):
        roi_cfg = config.get('roi', {})
        if roi_cfg.get('enabled', False):
            roi = (
                roi_cfg.get('x', 0),
                roi_cfg.get('y', 0),
                roi_cfg.get('w', 0),
                roi_cfg.get('h', 0)
            )
            self.roi = roi if roi[2] > 0 and roi[3] > 0 else None
        else:
            self.roi = None

    def save_to_config(self, roi):
        import json
        import os
        import tempfile
        with open(self.config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        config['roi'] = {
            'enabled': True,
            'x': roi[0],
            'y': roi[1],
            'w': roi[2],
            'h': roi[3]
        }

        # Атомарная запись: сбой в процессе не оставляет обрезанный config.json
        directory = os.path.dirname(os.path.abspath(self.config_path)) or '.'
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4)
            os.replace(tmp_path, self.config_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

        self.roi = roi
        print(f"ROI saved: x={roi[0]}, y={roi[1]}, w={roi[2]}, h={roi[3]}")

    def apply_to_image(self, image):
        """Обрезает изображение по ROI. Если ROI не задан, возвращает оригинал."""
        if not self.roi:
            return image
        x, y, w, h = self.roi
        return image[y:y+h, x:x+w]
