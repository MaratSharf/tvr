import cv2
import numpy as np


class SeamProcessor:
    def __init__(self, config):
        self.min_width = config['min_width_mm']
        self.max_width = config['max_width_mm']
        self.px_per_mm = config['px_per_mm']
        self.threshold = config.get('threshold', 120)
        self.roi = None

    def set_roi(self, roi):
        """Установка зоны сканирования (x, y, w, h) или None."""
        if roi and (roi[2] <= 0 or roi[3] <= 0):
            roi = None
        self.roi = roi

    def _crop_to_roi(self, image):
        """Обрезка изображения по ROI с проверкой границ кадра."""
        if not self.roi:
            return image, 0, 0
        height, width = image.shape[:2]
        x, y, w, h = self.roi
        x0 = max(0, min(int(x), width))
        y0 = max(0, min(int(y), height))
        x1 = max(0, min(int(x) + int(w), width))
        y1 = max(0, min(int(y) + int(h), height))
        if x1 <= x0 or y1 <= y0:
            return image[0:0, 0:0], 0, 0
        return image[y0:y1, x0:x1], x0, y0

    def process(self, image):
        if image is None or image.size == 0:
            return False, 0.0, "Empty image", None
        if self.px_per_mm <= 0:
            return False, 0.0, "Invalid px_per_mm calibration", None

        cropped, offset_x, offset_y = self._crop_to_roi(image)
        if cropped is None or cropped.size == 0:
            return False, 0.0, "Empty ROI", None

        if cropped.ndim == 2:
            gray = cropped
        elif cropped.shape[2] == 4:
            gray = cv2.cvtColor(cropped, cv2.COLOR_BGRA2GRAY)
        else:
            gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)

        clahe = cv2.createCLAHE(clipLimit=5.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)

        blur = cv2.GaussianBlur(enhanced, (5, 5), 0)
        val, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15))
        kernel_open = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel_close)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, kernel_open)

        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        if not contours:
            return False, 0.0, "Seam not found", None

        candidates = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            area = cv2.contourArea(c)
            if area < 100:
                continue
            if w < h:
                width_px = w
            else:
                width_px = h
            width_mm = width_px / self.px_per_mm
            if self.min_width <= width_mm <= self.max_width:
                candidates.append((width_mm, area, x, y, w, h))

        if not candidates:
            largest = max(contours, key=cv2.contourArea)
            x, y, w, h = cv2.boundingRect(largest)
            width_px = min(w, h)
            width_mm = width_px / self.px_per_mm
            bbox = (x + offset_x, y + offset_y, w, h)
            return False, width_mm, f"Width {width_mm:.2f}mm out of range", bbox

        candidates.sort(key=lambda c: c[1], reverse=True)
        best = candidates[0]
        width_mm, area, x, y, w, h = best
        bbox = (x + offset_x, y + offset_y, w, h)
        return True, width_mm, "OK", bbox

    def draw_result(self, image, bbox, ok, width_mm):
        """Рисует бокс и результат на изображении."""
        result_img = image.copy()
        color = (0, 255, 0) if ok else (0, 0, 255)
        label = f"{'OK' if ok else 'NOK'}: {width_mm:.2f}mm"

        if bbox:
            x, y, w, h = bbox
            cv2.rectangle(result_img, (x, y), (x + w, y + h), color, 2)
            font = cv2.FONT_HERSHEY_SIMPLEX
            cv2.putText(result_img, label, (x, y - 10), font, 0.6, color, 2)

        if self.roi:
            rx, ry, rw, rh = self.roi
            cv2.rectangle(result_img, (rx, ry), (rx + rw, ry + rh), (255, 255, 0), 1)

        return result_img
