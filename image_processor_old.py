import cv2
import numpy as np
from typing import Optional, List, Tuple, Dict, Any


class SeamProcessor:
    """
    Измерение ширины шва и детекция разрывов методом поперечных секущих срезов.

    Новый алгоритм:
      1. Проверка контраста внутри ROI (защита от пустого кадра / отсутствия мастики).
      2. Бинаризация (Otsu или ручной порог из config).
      3. Мелкая морфология MORPH_CLOSE (настраиваемое ядро, по умолчанию 3x3).
      4. Построение осевой линии шва (centerline): по размеченной полилинии из эталона
         или автоматически (fitLine по экстремальным точкам области шва).
      5. N поперечных срезов (нормалей) к оси с шагом step_px.
      6. Измерение ширины белого участка в каждом срезе (в мм).
      7. Детекция разрывов: пустой срез (W_i < 0.2 мм или нет белых пикселей).
      8. Критерии брака: I_ERR_GAP, I_ERR_WID, I_ERR_POS.
    """

    ERR_NONE = 0
    ERR_GAP = 1
    ERR_WID = 2
    ERR_POS = 4
    ERR_NO_SEAM = 8

    def __init__(self, config):
        self.min_width = float(config.get('min_width_mm', 1.0))
        self.max_width = float(config.get('max_width_mm', 6.0))
        self.px_per_mm = float(config.get('px_per_mm', 50.0))
        self.threshold = int(config.get('threshold', 120))
        self.use_otsu = bool(config.get('use_otsu', True))
        self.roi = None

        self.step_px = int(config.get('step_px', 15))
        self.max_allowed_gap_mm = float(config.get('max_allowed_gap_mm', 0.5))
        self.pos_tolerance_mm = float(config.get('pos_tolerance_mm', 2.5))
        self.min_fill_ratio = float(config.get('min_fill_ratio', 0.85))
        self.morph_kernel_size = int(config.get('morph_kernel_size', 3))
        self.min_contrast_std = float(config.get('min_contrast_std', 8.0))
        self.gap_min_width_mm = float(config.get('gap_min_width_mm', 0.2))

        self.centerline_px = None
        self.ignore_rects = None
        self.etalon_metrics = None

        self.last_detail = None

    # ------------------------------------------------------------------ #
    # ROI / разметка
    # ------------------------------------------------------------------ #
    def set_roi(self, roi):
        """Установка зоны сканирования (x, y, w, h) или None."""
        if roi and (roi[2] <= 0 or roi[3] <= 0):
            roi = None
        self.roi = roi

    def set_etalon_annotation(self, centerline_px=None, ignore_rects=None,
                              step_px=None, max_allowed_gap_mm=None,
                              pos_tolerance_mm=None, metrics=None):
        """Загрузить размеченную ось и параметры из эталонного файла."""
        if centerline_px is not None:
            self.centerline_px = [tuple(p) for p in centerline_px]
        if ignore_rects is not None:
            self.ignore_rects = [tuple(r) for r in ignore_rects]
        if step_px is not None:
            self.step_px = int(step_px)
        if max_allowed_gap_mm is not None:
            self.max_allowed_gap_mm = float(max_allowed_gap_mm)
        if pos_tolerance_mm is not None:
            self.pos_tolerance_mm = float(pos_tolerance_mm)
        self.etalon_metrics = metrics

    def clear_etalon_annotation(self):
        """Сбросить разметку эталона (вернуться к автоматическому режиму)."""
        self.centerline_px = None
        self.ignore_rects = None
        self.etalon_metrics = None

    # ------------------------------------------------------------------ #
    # Вспомогательные методы
    # ------------------------------------------------------------------ #
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

    def _apply_ignore_mask(self, thresh, offset_x, offset_y):
        """Закрасить игнорируемые зоны (блики/отверстия) нулями на бинарной маске."""
        if not self.ignore_rects:
            return thresh
        mask = thresh.copy()
        for (rx, ry, rw, rh) in self.ignore_rects:
            x0 = max(0, int(rx) + offset_x)
            y0 = max(0, int(ry) + offset_y)
            x1 = min(mask.shape[1], x0 + int(rw))
            y1 = min(mask.shape[0], y0 + int(rh))
            if x1 > x0 and y1 > y0:
                mask[y0:y1, x0:x1] = 0
        return mask

    # ------------------------------------------------------------------ #
    # Ось шва (centerline)
    # ------------------------------------------------------------------ #
    def _build_centerline_auto(self, thresh):
        """Автоматическая осевая линия по экстремальным точкам области шва."""
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        largest = max(contours, key=cv2.contourArea)
        pts = largest.reshape(-1, 2).astype(np.float32)
        if len(pts) < 2:
            return None
        [vx, vy, x0, y0] = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01)
        vx = float(vx[0]); vy = float(vy[0]); x0 = float(x0[0]); y0 = float(y0[0])
        length = max(1e-6, (vx * vx + vy * vy) ** 0.5)
        vx /= length; vy /= length
        ts = [(float(p[0]) - x0) * vx + (float(p[1]) - y0) * vy for p in pts]
        t_min, t_max = min(ts), max(ts)
        step = max(1.0, float(self.step_px))
        n = int((t_max - t_min) / step) + 1
        centerline = []
        for i in range(n):
            t = t_min + i * step
            cx = x0 + t * vx
            cy = y0 + t * vy
            centerline.append((int(round(cx)), int(round(cy))))
        return centerline

    def _build_centerline(self, thresh):
        """Построить осевую линию: по размеченной полилинии или автоматически."""
        if self.centerline_px and len(self.centerline_px) >= 2:
            return self.centerline_px
        return self._build_centerline_auto(thresh)

    # ------------------------------------------------------------------ #
    # Поперечные срезы
    # ------------------------------------------------------------------ #
    def _perpendicular(self, p1, p2):
        """Единичный вектор, перпендикулярный отрезку p1->p2."""
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        length = max(1e-6, (dx * dx + dy * dy) ** 0.5)
        return (-dy / length, dx / length)

    def _slice_width(self, thresh, center, normal, half_length_px):
        """
        Измерить ширину непрерывного белого участка вдоль нормали в точке center.
        """
        cx, cy = center
        nx, ny = normal
        H = thresh.shape[0]
        W = thresh.shape[1]

        samples = int(half_length_px * 2) + 1
        xs = []
        ys = []
        vals = []
        for i in range(samples):
            t = -half_length_px + i
            x = int(round(cx + t * nx))
            y = int(round(cy + t * ny))
            if 0 <= x < W and 0 <= y < H:
                xs.append(x)
                ys.append(y)
                vals.append(thresh[y, x])

        if not vals:
            return 0.0, 0.0, False

        vals_arr = np.array(vals, dtype=np.uint8)
        white_mask = vals_arr > 0
        if not np.any(white_mask):
            return 0.0, 0.0, False

        white_indices = np.where(white_mask)[0]
        best_start = white_indices[0]
        best_end = white_indices[0]
        cur_start = white_indices[0]
        cur_end = white_indices[0]
        for idx in white_indices[1:]:
            if idx == cur_end + 1:
                cur_end = idx
            else:
                if cur_end - cur_start > best_end - best_start:
                    best_start, best_end = cur_start, cur_end
                cur_start = idx
                cur_end = idx
        if cur_end - cur_start > best_end - best_start:
            best_start, best_end = cur_start, cur_end

        width_px = float(best_end - best_start + 1)
        center_t = (best_start + best_end) / 2.0 - half_length_px
        center_offset_px = center_t
        return width_px, center_offset_px, True

    # ------------------------------------------------------------------ #
    # Основной метод process()
    # ------------------------------------------------------------------ #
    def process(self, image):
        detail = {
            'ok': False,
            'width_mm': 0.0,
            'error_code': self.ERR_NONE,
            'error_messages': [],
            'slices': [],
            'centerline': [],
            'bbox': None,
            'max_gap_mm': 0.0,
            'fill_ratio': 0.0,
            'mean_width_mm': 0.0,
            'min_width_mm': 0.0,
            'max_width_mm': 0.0,
            'expected_length_mm': 0.0,
            'annotated_image': None,
        }

        if image is None or image.size == 0:
            detail['error_messages'].append("Empty image")
            self.last_detail = detail
            return False, 0.0, "Empty image", detail

        if self.px_per_mm <= 0:
            detail['error_messages'].append("Invalid px_per_mm calibration")
            self.last_detail = detail
            return False, 0.0, "Invalid px_per_mm calibration", detail

        cropped, offset_x, offset_y = self._crop_to_roi(image)
        if cropped is None or cropped.size == 0:
            detail['error_messages'].append("Empty ROI")
            self.last_detail = detail
            return False, 0.0, "Empty ROI", detail

        if cropped.ndim == 2:
            gray = cropped
        elif cropped.shape[2] == 4:
            gray = cv2.cvtColor(cropped, cv2.COLOR_BGRA2GRAY)
        else:
            gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)

        blur_for_contrast = cv2.GaussianBlur(gray, (5, 5), 0)
        contrast_std = float(np.std(blur_for_contrast))
        p5 = np.percentile(blur_for_contrast, 5)
        p95 = np.percentile(blur_for_contrast, 95)
        contrast_range = float(p95 - p5)
        if contrast_std < self.min_contrast_std and contrast_range < 2 * self.min_contrast_std:
            detail['error_messages'].append("No seam detected: low contrast")
            detail['error_code'] = self.ERR_NO_SEAM
            self.last_detail = detail
            return False, 0.0, "NOK: No seam detected (low contrast)", detail

        clahe = cv2.createCLAHE(clipLimit=5.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        blur = cv2.GaussianBlur(enhanced, (5, 5), 0)

        if self.use_otsu:
            val, thresh = cv2.threshold(blur, 0, 255,
                                         cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        else:
            val, thresh = cv2.threshold(blur, self.threshold, 255,
                                         cv2.THRESH_BINARY_INV)

        thresh = self._apply_ignore_mask(thresh, offset_x, offset_y)

        k = max(1, self.morph_kernel_size)
        if k % 2 == 0:
            k += 1
        kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel_close)

        centerline = self._build_centerline(cleaned)
        if centerline is None or len(centerline) < 2:
            detail['error_messages'].append("Seam centerline not found")
            detail['error_code'] = self.ERR_NO_SEAM
            self.last_detail = detail
            return False, 0.0, "NOK: Seam not found", detail

        half_length = max(20, int(self.step_px * 1.5))
        slices = []
        widths_valid = []
        max_gap_mm = 0.0
        current_gap_slices = 0
        expected_slices = len(centerline)
        valid_slices = 0

        for i, center in enumerate(centerline):
            if i < len(centerline) - 1:
                normal = self._perpendicular(centerline[i], centerline[i + 1])
            else:
                normal = self._perpendicular(centerline[i - 1], centerline[i])

            width_px, offset_px, has_white = self._slice_width(
                cleaned, center, normal, half_length)
            width_mm = width_px / self.px_per_mm
            offset_mm = offset_px / self.px_per_mm

            is_gap = (not has_white) or (width_mm < self.gap_min_width_mm)
            status = 'gap' if is_gap else ('ok' if self.min_width <= width_mm <= self.max_width else 'wid')

            if is_gap:
                current_gap_slices += 1
                gap_len = current_gap_slices * self.step_px / self.px_per_mm
                if gap_len > max_gap_mm:
                    max_gap_mm = gap_len
            else:
                current_gap_slices = 0
                valid_slices += 1
                widths_valid.append(width_mm)

            slices.append({
                'index': i,
                'center': center,
                'normal': normal,
                'width_px': width_px,
                'width_mm': width_mm,
                'offset_mm': offset_mm,
                'is_gap': is_gap,
                'status': status,
            })

        expected_length_mm = expected_slices * self.step_px / self.px_per_mm
        fill_ratio = (valid_slices / expected_slices) if expected_slices > 0 else 0.0

        if widths_valid:
            mean_w = float(np.mean(widths_valid))
            min_w = float(np.min(widths_valid))
            max_w = float(np.max(widths_valid))
        else:
            mean_w = 0.0; min_w = 0.0; max_w = 0.0

        error_code = self.ERR_NONE
        error_messages = []

        all_gaps = len(slices) > 0 and all(s['is_gap'] for s in slices)
        if all_gaps:
            error_code |= self.ERR_NO_SEAM
            error_messages.append("NOK: No seam detected")

        if max_gap_mm > self.max_allowed_gap_mm:
            error_code |= self.ERR_GAP
            error_messages.append("I_ERR_GAP: max gap %.2fmm > %.2fmm" % (max_gap_mm, self.max_allowed_gap_mm))

        if fill_ratio < self.min_fill_ratio:
            error_code |= self.ERR_GAP
            error_messages.append("I_ERR_GAP: fill ratio %.2f < %.2f" % (fill_ratio, self.min_fill_ratio))

        if widths_valid:
            if min_w < self.min_width:
                error_code |= self.ERR_WID
                error_messages.append("I_ERR_WID: min width %.2fmm < %.2fmm" % (min_w, self.min_width))
            if max_w > self.max_width:
                error_code |= self.ERR_WID
                error_messages.append("I_ERR_WID: max width %.2fmm > %.2fmm" % (max_w, self.max_width))

        if slices:
            non_gap_offsets = [abs(s['offset_mm']) for s in slices if not s['is_gap']]
            if non_gap_offsets:
                avg_abs_offset = float(np.mean(non_gap_offsets))
                if avg_abs_offset > self.pos_tolerance_mm:
                    error_code |= self.ERR_POS
                    error_messages.append("I_ERR_POS: offset %.2fmm > %.2fmm" % (avg_abs_offset, self.pos_tolerance_mm))

        ok = (error_code == self.ERR_NONE)
        msg = "; ".join(error_messages) if error_messages else "OK"

        detail.update({
            'ok': ok,
            'width_mm': mean_w if widths_valid else 0.0,
            'error_code': error_code,
            'error_messages': error_messages,
            'slices': slices,
            'centerline': centerline,
            'bbox': None,
            'max_gap_mm': max_gap_mm,
            'fill_ratio': fill_ratio,
            'mean_width_mm': mean_w,
            'min_width_mm': min_w,
            'max_width_mm': max_w,
            'expected_length_mm': expected_length_mm,
        })
        self.last_detail = detail

        return ok, mean_w if widths_valid else 0.0, msg, detail

    # ------------------------------------------------------------------ #
    # Визуализация
    # ------------------------------------------------------------------ #
    def draw_result(self, image, detail=None, ok=None, width_mm=None):
        """
        Рисует поперечные срезы, ось, зоны игнорирования и результат на изображении.
        """
        result_img = image.copy()

        if detail is None:
            detail = self.last_detail or {}

        if self.roi:
            rx, ry, rw, rh = self.roi
            cv2.rectangle(result_img, (rx, ry), (rx + rw, ry + rh), (255, 255, 0), 1)
            cv2.putText(result_img, "ROI", (rx + 5, ry + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

        if self.ignore_rects:
            roi_x = self.roi[0] if self.roi else 0
            roi_y = self.roi[1] if self.roi else 0
            for (rx, ry, rw, rh) in self.ignore_rects:
                x0 = rx + roi_x
                y0 = ry + roi_y
                cv2.rectangle(result_img, (x0, y0), (x0 + rw, y0 + rh), (128, 128, 128), -1)
                cv2.putText(result_img, "IGNORE", (x0 + 3, y0 + 15),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

        centerline = detail.get('centerline', [])
        slices = detail.get('slices', [])
        error_code = detail.get('error_code', self.ERR_NONE)

        if len(centerline) >= 2:
            for i in range(len(centerline) - 1):
                cv2.line(result_img, centerline[i], centerline[i + 1], (0, 255, 255), 1)

        for sl in slices:
            cx, cy = sl['center']
            nx, ny = sl['normal']
            half_len = max(10, int(self.step_px * 0.8))
            x1 = int(round(cx - half_len * nx))
            y1 = int(round(cy - half_len * ny))
            x2 = int(round(cx + half_len * nx))
            y2 = int(round(cy + half_len * ny))

            if sl['is_gap']:
                color = (0, 0, 255)
                thickness = 2
                cv2.circle(result_img, (cx, cy), 4, color, -1)
                cv2.putText(result_img, "GAP: %.1fmm" % sl['width_mm'],
                            (cx + 6, cy - 6), cv2.FONT_HERSHEY_SIMPLEX,
                            0.45, color, 1)
            elif sl['status'] == 'ok':
                color = (0, 255, 0)
                thickness = 1
            else:
                color = (0, 255, 255)
                thickness = 2

            cv2.line(result_img, (x1, y1), (x2, y2), color, thickness)

        is_ok = detail.get('ok', False) if ok is None else bool(ok)
        w_text = detail.get('width_mm', 0.0) if width_mm is None else float(width_mm)
        color = (0, 255, 0) if is_ok else (0, 0, 255)
        cv2.putText(result_img, "%s: %.2fmm" % ("OK" if is_ok else "NOK", w_text),
                    (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

        if error_code & self.ERR_GAP:
            cv2.putText(result_img, "I_ERR_GAP", (10, 55),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        if error_code & self.ERR_WID:
            cv2.putText(result_img, "I_ERR_WID", (10, 80),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        if error_code & self.ERR_POS:
            cv2.putText(result_img, "I_ERR_POS", (10, 105),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        if error_code & self.ERR_NO_SEAM:
            cv2.putText(result_img, "NO SEAM", (10, 130),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        detail['annotated_image'] = result_img
        return result_img

    def draw_result_backward_compat(self, image, bbox, ok, width_mm):
        """Старая сигнатура для обратной совместимости."""
        return self.draw_result(image, ok=ok, width_mm=width_mm)
