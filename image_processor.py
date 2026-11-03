import cv2
import numpy as np
import time
from typing import Optional, List, Tuple, Dict, Any


class SeamProcessor:
    """
    Измерение ширины шва и детекция разрывов методом поперечных секущих срезов.
    Улучшенная версия: устойчивый поиск границ по профилю яркости, защита от
    бликов/теней без затирки реальных разрывов, равномерная интерполяция
    криволинейной оси эталона, раздельные состояния OK / NOK / INVALID.

    Координатный контракт (единый для всего модуля):
      * ROI задаётся в координатах ИСХОДНОГО кадра (x, y, w, h).
      * centerline_px / ignore_rects эталона хранятся в ЛОКАЛЬНЫХ координатах
        кадра, обрезанного по annotation_roi (поле calibration.roi в sidecar
        эталона). Если annotation_roi неизвестен (старые эталоны), считается,
        что разметка уже локальна относительно текущего ROI.
      * Внутри process() все маски, ось и срезы — локальные координаты
        обрезанного ROI (начало (0,0) — левый верхний угол ROI).
      * detail['centerline'] и slice['center'] — ГЛОБАЛЬНЫЕ координаты
        исходного кадра (для отрисовки на полном кадре и передачи в GUI).
    """

    ERR_NONE = 0
    ERR_GAP = 1          # разрыв шва / недозаполнение
    ERR_WID = 2          # ширина вне допуска
    ERR_POS = 4          # смещение шва вне допуска
    ERR_NO_SEAM = 8      # шов не найден vůbec
    ERR_INVALID = 16     # недостаточно данных для уверенного решения (не дефект!)

    STATE_OK = 'OK'
    STATE_NOK = 'NOK'
    STATE_INVALID = 'INVALID'

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

        # Дополнительные опции (обратно совместимы с дефолтным config.json)
        self.auto_mask_hole = bool(config.get('auto_mask_hole', True))
        self.hole_threshold = int(config.get('hole_threshold', 28))
        self.fill_glare_holes = bool(config.get('fill_glare_holes', True))

        # --- новые опции (сохраняют старое поведение при дефолтных значениях) ---
        self.annotation_roi = None          # (x, y, w, h) глобальные координаты кадра,
                                              # относительно которого дана разметка эталона
        self.skip_centerline_align = bool(config.get('skip_centerline_align', False))
        self.edge_contrast_min = float(config.get('edge_contrast_min', 25.0))
        self.min_valid_slices = int(config.get('min_valid_slices', 3))
        self.max_align_shift_mm = float(config.get('max_align_shift_mm', 15.0))
        self.perf_timing = bool(config.get('perf_timing', False))
        self.auto_centerline = bool(config.get('auto_centerline', True))
        self.use_shadow_opening = bool(config.get('use_shadow_opening', True))
        self.min_confidence = float(config.get('min_confidence', 0.5))

        # Диагностический режим: сохраняет исходные кадры/маски/измерения
        self.save_diagnostics = bool(config.get('save_diagnostics', False))
        self.diagnostics_dir = config.get('diagnostics_dir', 'data/diagnostics')
        self.diagnostics_keep = int(config.get('diagnostics_keep', 20))

        # Источник нормативов ширины (для отчётности; не меняется автоматически)
        self.limits_source = str(config.get('limits_source', 'config.json'))

        self.centerline_px = None
        self.ignore_rects = None
        self.etalon_metrics = None

        self.last_detail = None
        self.last_timings = {}


    # ------------------------------------------------------------------ #
    # ROI / разметка эталона
    # ------------------------------------------------------------------ #
    def set_roi(self, roi):
        """Установка зоны сканирования (x, y, w, h) в координатах исходного кадра или None."""
        if roi is None:
            self.roi = None
            return
        x, y, w, h = [int(v) for v in roi]
        if w <= 0 or h <= 0:
            self.roi = None
        else:
            self.roi = (x, y, w, h)

    def set_etalon_annotation(self, centerline_px=None, ignore_rects=None,
                              step_px=None, max_allowed_gap_mm=None,
                              pos_tolerance_mm=None, metrics=None,
                              annotation_roi=None, skip_align=None):
        """
        Загрузить размещенную ось и параметры из эталонного файла.

        centerline_px / ignore_rects — координаты в ЛОКАЛЬНОЙ системе кадра,
        обрезанного по annotation_roi (начало этого кадра в глобальных
        координатах исходного изображения). Если annotation_roi None,
        разметка считается уже локальной относительно текущего ROI
        (сохраняет поведение старых эталонов).
        """
        if centerline_px is not None:
            self.centerline_px = [(float(p[0]), float(p[1])) for p in centerline_px]
        if ignore_rects is not None:
            self.ignore_rects = [tuple(float(v) for v in r) for r in ignore_rects]
        if step_px is not None:
            self.step_px = int(step_px)
        if max_allowed_gap_mm is not None and float(max_allowed_gap_mm) > 0:
            self.max_allowed_gap_mm = float(max_allowed_gap_mm)
        if pos_tolerance_mm is not None:
            self.pos_tolerance_mm = float(pos_tolerance_mm)
        self.etalon_metrics = metrics
        if annotation_roi is not None:
            x, y, w, h = [int(v) for v in annotation_roi]
            self.annotation_roi = (x, y, w, h) if w > 0 and h > 0 else None
        if skip_align is not None:
            self.skip_centerline_align = bool(skip_align)
        # Допуски и калибровка активного эталона применяются ТОЛЬКО если они
        # явно присутствуют в sidecar (эталоны со старой схемой не меняют
        # нормативы из config.json). Источник норм фиксируется для отчёта.
        if isinstance(metrics, dict) and 'limits' in metrics and \
                isinstance(metrics['limits'], dict):
            lim = metrics['limits']
            if lim.get('min_width_mm') is not None:
                self.min_width = float(lim['min_width_mm'])
            if lim.get('max_width_mm') is not None:
                self.max_width = float(lim['max_width_mm'])
            self.limits_source = str(lim.get('source') or 'etalon metrics.limits')
        elif metrics:
            self.limits_source = 'config.json (etalon has no own limits)'

    def clear_etalon_annotation(self):
        """Сбросить разметку эталона (вернуться к автоматическому режиму)."""
        self.centerline_px = None
        self.ignore_rects = None
        self.annotation_roi = None
        self.etalon_metrics = None

    def _etalon_mean_width(self):
        """Средняя ширина шва из метрик активного эталона (или 0, если нет)."""
        m = self.etalon_metrics or {}
        try:
            return float(m.get('mean_width_mm', 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------ #
    # Вспомогательные методы и защита координат
    # ------------------------------------------------------------------ #
    def _timer(self, timings, name):
        if self.perf_timing:
            timings[name] = time.perf_counter()

    def _crop_to_roi(self, image):
        """Обрезка изображения по ROI с защитой от выхода за границы кадра.

        Возвращает (cropped, offset_x, offset_y) — локальный кадр и смещение
        его начала относительно исходного кадра.
        """
        height, width = image.shape[:2]
        if not self.roi:
            return image, 0, 0
        x, y, w, h = self.roi
        x0 = max(0, min(x, width))
        y0 = max(0, min(y, height))
        x1 = max(x0, min(x + w, width))
        y1 = max(y0, min(y + h, height))
        if x1 <= x0 or y1 <= y0:
            return image[0:0, 0:0], 0, 0
        return image[y0:y1, x0:x1], x0, y0

    def _annotation_to_global(self, pt):
        """Точка из локальных координат разметки эталона -> глобальные координаты кадра."""
        if self.annotation_roi is not None:
            return pt[0] + self.annotation_roi[0], pt[1] + self.annotation_roi[1]
        # Старые эталоны без calibration.roi: разметка уже локальна относительно
        # текущего кадра (предполагаем совпадение с текущим ROI).
        return pt[0], pt[1]

    def _global_to_local(self, pt, offset_x, offset_y):
        """Глобальные координаты кадра -> локальные координаты текущего обрезанного ROI."""
        return pt[0] - offset_x, pt[1] - offset_y

    def _to_local_coords(self, x, y, w_crop, h_crop, offset_x, offset_y):
        """Обратная совместимость: точка уже в локальных координатах текущего ROI."""
        return int(x), int(y)

    def _clip_point_to_crop(self, pt, w_crop, h_crop):
        return (max(0, min(int(pt[0]), w_crop - 1)), max(0, min(int(pt[1]), h_crop - 1)))

    def _rect_local_from_annotation(self, rect, offset_x, offset_y, w_crop, h_crop):
        """Прямоугольник игнорирования (локально эталона) -> локальные координаты текущего ROI."""
        rx, ry, rw, rh = rect
        gx, gy = self._annotation_to_global((rx, ry))
        lx, ly = self._global_to_local((gx, gy), offset_x, offset_y)
        return lx, ly, rw, rh

    def _apply_ignore_mask(self, thresh, offset_x, offset_y):
        """Закрыть зоны игнорирования в бинарной маске (локальные координаты ROI)."""
        if not self.ignore_rects:
            return thresh
        mask = thresh.copy()
        h_crop, w_crop = mask.shape[:2]
        for rect in self.ignore_rects:
            lx, ly, rw, rh = self._rect_local_from_annotation(rect, offset_x, offset_y, w_crop, h_crop)
            x0 = max(0, int(lx))
            y0 = max(0, int(ly))
            x1 = min(w_crop, int(lx) + max(0, int(rw)))
            y1 = min(h_crop, int(ly) + max(0, int(rh)))
            if x1 > x0 and y1 > y0:
                mask[y0:y1, x0:x1] = 0
        return mask

    def _point_in_ignore_rects(self, x, y, offset_x, offset_y):
        """Является ли точка (локальные координаты текущего ROI) частью зоны игнорирования."""
        if not self.ignore_rects:
            return False
        gx, gy = x + offset_x, y + offset_y
        for rect in self.ignore_rects:
            arx, ary = self._annotation_to_global((rect[0], rect[1]))
            if arx <= gx < arx + rect[2] and ary <= gy < ary + rect[3]:
                return True
    # ------------------------------------------------------------------ #
    # Сегментация: бинарная маска тёмного шва ( dark = white )
    # ------------------------------------------------------------------ #
    def _segment(self, gray):
        """Бинаризация: шов должен быть ТЬМЕЕ фона (THRESH_BINARY_INV).

        Возвращает (seg_raw, seg_processed, blur):
          * seg_raw — маска ДО морфологической обработки, источник достоверности
            о непрерывности шва (разрывы);
          * seg_processed — маска после опционального морфологического открытия
            (удаление тонких теней/шума);
          * blur — размытое изображение для оценки контраста.
        """
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        if self.use_otsu:
            otsu_val, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            tight_val = min(200, int(otsu_val * 1.08))
            _, seg_raw = cv2.threshold(blur, tight_val, 255, cv2.THRESH_BINARY_INV)
        else:
            _, seg_raw = cv2.threshold(blur, self.threshold, 255, cv2.THRESH_BINARY_INV)

        # Морфологическое открытие: удаляет тонкие тени и шум,
        # оставляя только реальный объёмный шов.
        seg_processed = seg_raw
        if self.use_shadow_opening:
            kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            seg_processed = cv2.morphologyEx(seg_raw, cv2.MORPH_OPEN, kernel_open, iterations=1)

        return seg_raw, seg_processed, blur

    def _mask_central_hole(self, gray, seg):
        """
        Маска центрального сквозного отверстия.

        Отверстие ищется как большой тёмный регион. Перед маскировкой проверяется:
        площадь в допустимых пределах, centroid недалеко от центра ROI и форма
        не вырождённая. Если подтверждённое отверстие не найдено — маска не
        накладывается (лучше измерить с дырой, чем скрыть часть шва).
        """
        if not self.auto_mask_hole:
            return seg
        h_crop, w_crop = seg.shape[:2]
        _, dark = cv2.threshold(gray, self.hole_threshold, 255, cv2.THRESH_BINARY_INV)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
        dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return seg

        min_hole_radius = max(5, int(self.max_width * self.px_per_mm * 0.35))
        min_hole_area = (self.max_width * self.px_per_mm) ** 2 * 0.5
        max_hole_area = float(w_crop * h_crop) * 0.45
        cx_img, cy_img = w_crop / 2.0, h_crop / 2.0
        max_center_offset = max(w_crop, h_crop) * 0.45

        best = None
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_hole_area or area > max_hole_area:
                continue
            M = cv2.moments(cnt)
            if M['m00'] <= 0:
                continue
            cx = M['m10'] / M['m00']
            cy = M['m01'] / M['m00']
            if abs(cx - cx_img) > max_center_offset or abs(cy - cy_img) > max_center_offset:
                continue
            (rx_, ry_), (rw_, rh_), _ = cv2.minAreaRect(cnt)
            if max(rw_, rh_) < 1e-6:
                continue
            if min(rw_, rh_) / max(rw_, rh_) < 0.15:
                continue
            if best is None or area > best[0]:
                best = (area, cnt, (cx, cy))

        if best is None:
            return seg
        _, hole_cnt, hole_center = best
        hole_mask = np.zeros((h_crop, w_crop), np.uint8)
        cv2.drawContours(hole_mask, [hole_cnt], -1, 255, cv2.FILLED)
        # Небольшой отступ, чтобы не зацепить края шва, прилегающего к отверстию
        pad = max(3, int(self.max_width * self.px_per_mm * 0.10))
        if pad > 0:
            hole_mask = cv2.dilate(hole_mask, np.ones((pad, pad), np.uint8))
        res = seg.copy()
        res[hole_mask > 0] = 0
        return res

    def _mask_shadow_zones(self, gray, seg):
        """
        Автоматическое маскирование зон с плавным градиентом (тени).
        Тени имеют низкий локальный контраст и плавный переход яркости,
        в то время как шов — тёмный объект с резкими границами.
        """
        if not self.use_shadow_opening:
            return seg

        h_crop, w_crop = seg.shape[:2]

        # Локальный градиент яркости (разница между соседними пикселями).
        grad_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        gradient_magnitude = cv2.magnitude(grad_x, grad_y)

        # Зоны с очень низким градиентом — потенциальные тени/плавные переходы.
        _, shadow_mask = cv2.threshold(gradient_magnitude, 8.0, 255, cv2.THRESH_BINARY_INV)
        shadow_mask = shadow_mask.astype(np.uint8)

        # Объединяем близкие зоны в области.
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        shadow_mask = cv2.morphologyEx(shadow_mask, cv2.MORPH_CLOSE, kernel)

        # Маскируем тень только если она не покрывает основную часть шва
        # (иначе это ложное срабатывание — весь кадр в тени).
        intersection = cv2.bitwise_and(seg, shadow_mask)
        intersection_ratio = cv2.countNonZero(intersection) / max(1, cv2.countNonZero(seg))

        if intersection_ratio < 0.7:
            seg = cv2.bitwise_and(seg, cv2.bitwise_not(shadow_mask))

        return seg

    def _local_threshold_slice(self, gray, center, normal, half_length_px):
        """
        Локальная бинаризация вокруг среза для случаев, когда глобальный порог
        не работает (изгибы, тени, неровности). Использует адаптивный порог
        по зоне вокруг среза.
        """
        cx, cy = center
        nx, ny = normal
        h, w = gray.shape[:2]

        # Извлечь зону вокруг среза (прямоугольник вдоль нормали)
        margin = int(half_length_px * 1.5)
        x1 = max(0, int(cx - margin * abs(ny) - margin * abs(nx)))
        y1 = max(0, int(cy - margin * abs(nx) - margin * abs(ny)))
        x2 = min(w, int(cx + margin * abs(ny) + margin * abs(nx)))
        y2 = min(h, int(cy + margin * abs(nx) + margin * abs(ny)))

        if x2 - x1 < 10 or y2 - y1 < 10:
            return None

        local_gray = gray[y1:y2, x1:x2]

        # Адаптивный порог по локальной зоне
        local_blur = cv2.GaussianBlur(local_gray, (5, 5), 0)
        otsu_val, _ = cv2.threshold(local_blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        tight_val = min(200, int(otsu_val * 1.08))
        _, local_seg = cv2.threshold(local_blur, tight_val, 255, cv2.THRESH_BINARY_INV)

        # Морфологическое открытие
        kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        local_seg = cv2.morphologyEx(local_seg, cv2.MORPH_OPEN, kernel_open)

        # Вернуть маску полного размера (как seg), где обработана только зона вокруг среза
        full_seg = np.zeros((h, w), np.uint8)
        full_seg[y1:y2, x1:x2] = local_seg

        return full_seg

    def _fill_specular_glare(self, seg, centerline_local=None):
        """
        Заливка только тех внутренних полостей, которые обоснованно являются
        бликом внутри валика шва: полость должна быть полностью окружена швом
        (не касаться границ изображения) и быть не шире одного поперечного размера.

        Реальные разрывы/пустоты не заливаются. Не используется как единственный
        источник информации о непрерывности шва.
        """
        if not self.fill_glare_holes:
            return seg
        contours, hierarchy = cv2.findContours(seg, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:
            return seg
        out = seg.copy()
        h_crop, w_crop = seg.shape[:2]
        max_glare_radius = self.max_width * self.px_per_mm * 0.6
        max_glare_area = np.pi * max_glare_radius * max_glare_radius
        # коридор вокруг оси: блик возможен только там, где ожидается шов
        corridor = np.zeros((h_crop, w_crop), np.uint8)
        if centerline_local and len(centerline_local) >= 2:
            half_w = max(8.0, self.max_width * self.px_per_mm * 0.75)
            for i in range(len(centerline_local) - 1):
                p1 = centerline_local[i]
                p2 = centerline_local[i + 1]
                cv2.line(corridor, (int(p1[0]), int(p1[1])), (int(p2[0]), int(p2[1])),
                         255, thickness=int(half_w * 2))
            corridor = cv2.dilate(corridor, np.ones((5, 5), np.uint8))
        else:
            corridor[:] = 255

        for i, cnt in enumerate(contours):
            parent = hierarchy[0][i][3]
            if parent == -1:
                continue  # только внутренние контуры (полости)
            area = cv2.contourArea(cnt)
            if area <= 0 or area > max_glare_area:
                continue
            M = cv2.moments(cnt)
            if M['m00'] <= 0:
                continue
            cx = M['m10'] / M['m00']
            cy = M['m01'] / M['m00']
            if cx <= 1 or cy <= 1 or cx >= w_crop - 2 or cy >= h_crop - 2:
                continue  # полость у границы — это не блик внутри валика
            if centerline_local is not None and corridor[int(round(cy)), int(round(cx))] == 0:
                continue  # вне ожидаемого шва — не заливаем
            cv2.drawContours(out, [cnt], 0, 255, cv2.FILLED)
        return out

    # ------------------------------------------------------------------ #
    # Осевая линия шва (centerline)
    # ------------------------------------------------------------------ #
    def _resample_polyline(self, points, step_px):
        """Равномерная интерполяция полилинии с учётом длины каждого сегмента.

        Включает первую и последнюю точки; соседние точки — на расстоянии
        step_px (последний отрезок может быть короче). Слишком близкие
        входные точки игнорируются.
        """
        if not points or len(points) < 2:
            return [tuple(p) for p in points]
        pts = [np.array(p, dtype=np.float64) for p in points]
        step = max(1.0, float(step_px))

        resampled = [(float(pts[0][0]), float(pts[0][1]))]
        accum_dist = 0.0

        for i in range(len(pts) - 1):
            p1 = pts[i]
            p2 = pts[i + 1]
            seg_vec = p2 - p1
            seg_len = float(np.hypot(seg_vec[0], seg_vec[1]))
            if seg_len < 1e-6:
                continue
            unit_vec = seg_vec / seg_len
            dist = step - accum_dist
            while dist <= seg_len + 1e-9:
                new_pt = p1 + unit_vec * dist
                resampled.append((float(new_pt[0]), float(new_pt[1])))
                dist += step
            accum_dist = seg_len - (dist - step)
            if accum_dist < 0:
                accum_dist = 0.0

        last = (float(pts[-1][0]), float(pts[-1][1]))
        d_last = float(np.hypot(last[0] - resampled[-1][0], last[1] - resampled[-1][1]))
        if d_last > max(1.0, step * 0.5):
            resampled.append(last)
        elif d_last < 1e-6 and len(resampled) > 1:
            resampled[-1] = last

        cleaned = [resampled[0]]
        for pt in resampled[1:]:
            if float(np.hypot(pt[0] - cleaned[-1][0], pt[1] - cleaned[-1][1])) > 1e-3:
                cleaned.append(pt)
        return [(float(p[0]), float(p[1])) for p in cleaned]

    def _build_centerline_from_annotation(self, w_crop, h_crop, offset_x, offset_y):
        """Центральная линия из размеченного эталона (предпочтительный путь)."""
        if not self.centerline_px or len(self.centerline_px) < 2:
            return None
        local_pts = []
        for (px, py) in self.centerline_px:
            gx, gy = self._annotation_to_global((px, py))
            lx, ly = self._global_to_local((gx, gy), offset_x, offset_y)
            # точки, попавшие за границы текущего ROI, не зажимаем к краю
            # (исключаем — шов за зоной контроля измеряться не должен)
            if 0 <= lx < w_crop and 0 <= ly < h_crop:
                local_pts.append((lx, ly))
        uniq = [local_pts[0]]
        for pt in local_pts[1:]:
            if abs(pt[0] - uniq[-1][0]) > 1 or abs(pt[1] - uniq[-1][1]) > 1:
                uniq.append(pt)
        if len(uniq) < 2:
            return None
        return self._resample_polyline(uniq, self.step_px)

    @staticmethod
    def _skeletonize_ridge(mask, max_iterations=200):
        """
        Скелетин на основе гребня преобразования расстояния (без contrib-модулей).
        Возвращает бинарную маску оси шва.
        """
        dist = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
        if dist.max() <= 0:
            return np.zeros_like(mask)
        min_radius = max(2.0, dist.max() * 0.25)
        kernel3 = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        dilated = cv2.dilate(dist, kernel3)
        ridge = ((dist == dilated) & (dist >= min_radius)).astype(np.uint8)
        if cv2.countNonZero(ridge) == 0:
            return np.zeros_like(mask)
        # оставить только наибольшую компоненту гребня (основная ось, а не шум)
        n, labels, stats, cents = cv2.connectedComponentsWithStats(ridge, 8)
        if n <= 1:
            return np.zeros_like(mask)
        best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        skeleton = (labels == best).astype(np.uint8)
        return skeleton

    def _build_centerline_auto(self, seg):
        """Автоматическая центральная линия для криволинейного шва (без fitLine).

        fitLine строит одну прямую — непригодно для U-образной траектории.
        Используется гребень преобразования расстояния внутри маски шва;
        ось восстанавливается как путь между двумя концевыми точками скелета.
        Если ось надёжно построить невозможно — возвращается None (INVALID,
        а не заведомо неверная прямая).
        """
        if seg is None or seg.size == 0 or cv2.countNonZero(seg) == 0:
            return None
        skeleton = self._skeletonize_ridge(seg)
        if cv2.countNonZero(skeleton) == 0:
            return None
        ys, xs = np.nonzero(skeleton)
        points = list(zip(xs.tolist(), ys.tolist()))
        pts_set = set(points)

        def neighbors(p):
            x, y = p
            res = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    q = (x + dx, y + dy)
                    if q in pts_set:
                        res.append(q)
            return res

        degrees = {p: len(neighbors(p)) for p in pts_set}
        endpoints = [p for p, d in degrees.items() if d == 1]
        if len(endpoints) < 2:
            return None
        # концы оси — две наиболее удалённые концевые точки
        start = endpoints[0]
        end = max(endpoints, key=lambda p: float(np.hypot(p[0] - start[0], p[1] - start[1])))

        # greedy обход от start к end без повторного посещения вершин
        path = [start]
        visited = {start}
        cur = start
        max_steps = len(pts_set) + 10
        for _ in range(max_steps):
            if cur == end:
                break
            nbs = [q for q in neighbors(cur) if q not in visited]
            if not nbs:
                break
            nxt = min(nbs, key=lambda q: float(np.hypot(q[0] - end[0], q[1] - end[1])))
            visited.add(nxt)
            path.append(nxt)
            cur = nxt
        if cur != end or len(path) < 2:
            return None
        return [(float(p[0]), float(p[1])) for p in path]

    def _build_centerline(self, seg, gray, offset_x, offset_y):
        """Построение центральной линии: эталон приоритетен, иначе автоматика."""
        h_crop, w_crop = seg.shape[:2]
        if self.centerline_px and len(self.centerline_px) >= 2:
            line = self._build_centerline_from_annotation(w_crop, h_crop, offset_x, offset_y)
            if line is None or len(line) < 2:
                return None
            return line
        if self.auto_centerline:
            seg_for_axis = self._fill_specular_glare(seg, None)
            line = self._build_centerline_auto(seg_for_axis)
            if line is None or len(line) < 2:
                return None
            return line
        return None

    def _normals_along(self, centerline_local):
        """Нормали к оси: от соседних точек; на концах — от ближайшего сегмента."""
        n = len(centerline_local)
        normals = []
        pts = [np.array(p, dtype=np.float64) for p in centerline_local]
        last_good = None
        for i in range(n):
            if 0 < i < n - 1:
                p1 = pts[i - 1]
                p2 = pts[i + 1]
            elif i < n - 1:
                p1 = pts[i]
                p2 = pts[i + 1]
            else:
                p1 = pts[i - 1]
                p2 = pts[i]
            dx = p2[0] - p1[0]
            dy = p2[1] - p1[1]
            length = float(np.hypot(dx, dy))
            if length < 1e-3:
                nx_, ny_ = last_good if last_good is not None else (1.0, 0.0)
            else:
                nx_, ny_ = -dy / length, dx / length
                last_good = (nx_, ny_)
            normals.append((nx_, ny_))
        return normals

    def _align_centerline_by_hole(self, gray, seg, centerline_local, offset_x, offset_y):
        """
        Совмещение оси эталона с деталью.

        Сначала пытаемся привязаться к реальному сквозному отверстию (тёмное
        пятно в центре валика). Если отверстия нет — центр светлый, это
        замкнутый контур жгута/кольца — выравниваем ось по центру
        ограничивающего прямоугольника самой бинарной маски шва seg.
        Сдвиг оси ограничивается max_align_shift_mm; при превышении — ось не
        сдвигается и возвращается флаг отклонения (деталь не совместима с
        эталоном -> INVALID).
        """
        if self.centerline_px is None or not centerline_local or len(centerline_local) < 2:
            return centerline_local, False
        h_crop, w_crop = gray.shape[:2]

        # --- Путь 1: выравнивание по реальному сквозному отверстию ---
        hole_center = None
        _, dark = cv2.threshold(gray, self.hole_threshold, 255, cv2.THRESH_BINARY_INV)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
        dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, kernel)
        contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_hole_area = (self.max_width * self.px_per_mm) ** 2 * 0.5
        max_hole_area = float(w_crop * h_crop) * 0.45
        cx_img, cy_img = w_crop / 2.0, h_crop / 2.0
        max_center_offset = max(w_crop, h_crop) * 0.45

        best = None
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_hole_area or area > max_hole_area:
                continue
            M = cv2.moments(cnt)
            if M['m00'] <= 0:
                continue
            cx = M['m10'] / M['m00']
            cy = M['m01'] / M['m00']
            if abs(cx - cx_img) > max_center_offset or abs(cy - cy_img) > max_center_offset:
                continue
            if best is None or area > best[0]:
                best = (area, (cx, cy))

        use_hole = False
        if best is not None:
            cur_cx, cur_cy = best[1]
            arr_pts = np.array(centerline_local, dtype=np.float32)
            ref_cx = float(np.mean(arr_pts[:, 0]))
            ref_cy = float(np.mean(arr_pts[:, 1]))
            # В центре замкнутого жгута — светлый фон, а не отверстие:
            # найденное тёмное пятно — часть самого валика, к нему не привязываемся.
            rcx_i = max(0, min(w_crop - 1, int(round(ref_cx))))
            rcy_i = max(0, min(h_crop - 1, int(round(ref_cy))))
            center_patch = gray[max(0, rcy_i - 5):min(h_crop, rcy_i + 6),
                                max(0, rcx_i - 5):min(w_crop, rcx_i + 6)]
            if not (center_patch.size > 0 and
                    float(np.mean(center_patch)) > self.hole_threshold * 2.0):
                use_hole = True

        if use_hole:
            dx = cur_cx - ref_cx
            dy = cur_cy - ref_cy
            max_shift_px = self.max_align_shift_mm * self.px_per_mm
            if abs(dx) > max_shift_px or abs(dy) > max_shift_px:
                return centerline_local, True  # rejected: деталь не совместима с эталоном
            return [(int(round(p[0] + dx)), int(round(p[1] + dy))) for p in centerline_local], False

        # --- Путь 2: нет сквозного отверстия (светлый центр, замкнутый жгут) ---
        # Привязываем ось к центру bbox крупных контуров маски шва seg.
        if seg is None or seg.size == 0 or cv2.countNonZero(seg) == 0:
            return centerline_local, False
        seam_contours, _ = cv2.findContours(seg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_seam_area = (self.min_width * self.px_per_mm) ** 2 * 2.0
        valid_contours = [c for c in seam_contours if cv2.contourArea(c) > min_seam_area]
        if not valid_contours:
            return centerline_local, False
        all_pts = np.vstack([c.reshape(-1, 2).astype(np.float32) for c in valid_contours])
        cur_cx = 0.5 * (float(np.min(all_pts[:, 0])) + float(np.max(all_pts[:, 0])))
        cur_cy = 0.5 * (float(np.min(all_pts[:, 1])) + float(np.max(all_pts[:, 1])))
        arr_pts = np.array(centerline_local, dtype=np.float32)
        ref_cx = 0.5 * (float(np.min(arr_pts[:, 0])) + float(np.max(arr_pts[:, 0])))
        ref_cy = 0.5 * (float(np.min(arr_pts[:, 1])) + float(np.max(arr_pts[:, 1])))
        dx = cur_cx - ref_cx
        dy = cur_cy - ref_cy
        max_shift_px = self.max_align_shift_mm * self.px_per_mm
        if abs(dx) > max_shift_px or abs(dy) > max_shift_px:
            return centerline_local, True  # rejected: деталь не совместима с эталоном
        return [(int(round(p[0] + dx)), int(round(p[1] + dy))) for p in centerline_local], False

    # ------------------------------------------------------------------ #
    # Измерение одного поперечного среза
    # ------------------------------------------------------------------ #
    def _measure_slice(self, gray, seg, center, normal, half_length_px,
                       local_seg_override=None, slice_index=None):
        """
        Измерение ширины шва вдоль нормали к центральной линии.

        seg — бинарная маска (белый = шов), уже с ignore/hole/glare fill.
        gray — для оценки контраста и темноты профиля.
        local_seg_override — опциональная локальная маска (фолбэк для проблемных зон).
        slice_index — индекс среза для адаптивных порогов на изгибах.

        Возвращает словарь:
          width_px, offset_px, width_mm, offset_mm — измерение в пикселях/мм;
          valid — достоверно ли измерение (обе границы найдены и подтверждены);
          is_gap — нет достоверного шва в этом срезе (разрыв / конец шва);
          invalid — измерение недостоверно (не считается разрывом, но не валидно);
          confidence — показатель достоверности 0..1;
          left_t, right_t — позиции границ вдоль нормали (относительно центра);
          reason — текстовая причина неудачи.
        """
        cx, cy = center
        nx, ny = normal
        h, w = gray.shape[:2]

        # Адаптивные пороги для изгибов (кривизна сохраняется в _current_curvatures)
        curvatures = getattr(self, '_current_curvatures', {}) or {}
        curvature = float(curvatures.get(slice_index, 0.0)) if slice_index is not None else 0.0
        is_curve = curvature > 0.1
        curve_factor = 0.7 if is_curve else 1.0

        result = {
            'width_px': 0.0, 'offset_px': 0.0,
            'width_mm': 0.0, 'offset_mm': 0.0,
            'valid': False, 'is_gap': True, 'invalid': False,
            'confidence': 0.0, 'left_t': 0.0, 'right_t': 0.0,
            'reason': 'ok',
            'bg': 0.0, 'inside_mean': 0.0, 'darkness': 0.0,
            'edge_step_left': 0.0, 'edge_step_right': 0.0,
            'edge_sharpness_left': 0.0, 'edge_sharpness_right': 0.0,
            'profile_depth': 0.0, 'sharp_edges': False, 'depth_ok': False,
            'boundary1': None, 'boundary2': None,
        }

        half = max(1, int(half_length_px))
        t_arr = np.arange(-half, half + 1, dtype=np.float32)
        xs = np.round(cx + t_arr * nx).astype(np.int32)
        ys = np.round(cy + t_arr * ny).astype(np.int32)
        valid_mask = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
        if not np.any(valid_mask):
            result['reason'] = 'slice out of image'
            result['edge_sharpness_left'] = 0.0
            result['edge_sharpness_right'] = 0.0
            result['profile_depth'] = 0.0
            result['sharp_edges'] = False
            result['depth_ok'] = False
            result['confidence'] = 0.0
            return result
        t_arr = t_arr[valid_mask]
        xs = xs[valid_mask]
        ys = ys[valid_mask]
        if t_arr.size < 5:
            result['reason'] = 'slice too short'
            result['edge_sharpness_left'] = 0.0
            result['edge_sharpness_right'] = 0.0
            result['profile_depth'] = 0.0
            result['sharp_edges'] = False
            result['depth_ok'] = False
            result['confidence'] = 0.0
            return result

        prof_gray = gray[ys, xs].astype(np.float32)
        if local_seg_override is not None:
            prof_seg = local_seg_override[ys, xs].astype(np.uint8)
        else:
            prof_seg = seg[ys, xs].astype(np.uint8)

        # Сглаживание серого для устойчивой оценки фона и темноты
        k3 = np.array([0.25, 0.5, 0.25], dtype=np.float32)
        prof_smooth = np.convolve(prof_gray, k3, mode='same')

        bg = float(np.percentile(prof_smooth, 75))  # яркость металла по срезу
        white_idx = np.where(prof_seg > 127)[0]

        if white_idx.size == 0:
            result['reason'] = 'no white run in seg'
            result['edge_sharpness_left'] = 0.0
            result['edge_sharpness_right'] = 0.0
            result['profile_depth'] = 0.0
            result['sharp_edges'] = False
            result['depth_ok'] = False
            result['confidence'] = 0.0
            return result

        # Runs белых пикселей в seg
        runs = []
        start = int(white_idx[0])
        prev = int(white_idx[0])
        for idx in white_idx[1:]:
            idx = int(idx)
            if idx == prev + 1:
                prev = idx
            else:
                runs.append((start, prev))
                start = idx
                prev = idx
        runs.append((start, prev))

        # Склейка split-от-блика: узкий разрыв, внутри ярче краёв
        max_glare_gap_px = max(3.0, self.max_width * self.px_per_mm * 0.45)
        max_total_seam_px = self.max_width * self.px_per_mm * 1.15
        if len(runs) > 1:
            glued = [runs[0]]
            for (s, e) in runs[1:]:
                ps, pe = glued[-1]
                gap = s - pe - 1
                total_w = float(t_arr[e] - t_arr[ps])
                if 0 < gap <= max_glare_gap_px and total_w <= max_total_seam_px:
                    gap_vals = prof_gray[pe + 1:s]
                    # Склеиваем, если просвет внутри шва не является чистым фоном металла
                    # или является локальным бликом между двумя темными стенками валика
                    if gap_vals.size > 0 and float(np.min(gap_vals)) < bg + 10.0:
                        glued[-1] = (ps, e)
                        continue
                glued.append((s, e))
            runs = glued

        # Оценка темноты шва по белым пикселям сегментации
        seam_dark = float(np.percentile(prof_gray[white_idx], 10))
        darkness = bg - seam_dark

        # Скоринг run
        min_width_px = max(2.0, self.gap_min_width_mm * self.px_per_mm * 0.75)
        max_width_px = self.max_width * self.px_per_mm * 2.0
        search_half = max(self.max_width * self.px_per_mm * 1.1,
                          self.pos_tolerance_mm * self.px_per_mm + 10.0)
        expected_mid_px = 0.5 * (self.min_width + self.max_width) * self.px_per_mm
        expected_range_px = max(1.0, (self.max_width - self.min_width) * self.px_per_mm)

        best = None
        best_score = -1e9
        for (s, e) in runs:
            s_t = float(t_arr[s])
            e_t = float(t_arr[e])
            width_t = e_t - s_t
            if width_t < min_width_px or width_t > max_width_px:
                continue
            mid_t = 0.5 * (s_t + e_t)
            if abs(mid_t) > search_half:
                continue
            contains_axis = (s_t <= 0.0 <= e_t)
            inside_mean = float(np.mean(prof_gray[s:e + 1]))
            run_darkness = max(0.0, bg - inside_mean)
            width_pen = min(15.0, abs(width_t - expected_mid_px) / expected_range_px * 15.0)
            score = (1000.0 if contains_axis else 0.0) \
                    - 1.5 * abs(mid_t) \
                    + 0.5 * run_darkness \
                    + (15.0 - width_pen)
            if score > best_score:
                best_score = score
                best = (s, e, mid_t, width_t, inside_mean)

        if best is None:
            result['reason'] = 'no credible run in seg'
            result['edge_sharpness_left'] = 0.0
            result['edge_sharpness_right'] = 0.0
            result['profile_depth'] = 0.0
            result['sharp_edges'] = False
            result['depth_ok'] = False
            result['confidence'] = 0.0
            return result

        s, e, mid_t, width_t, inside_mean = best

        # Проверки достоверности
        min_edge = max(6.0, self.edge_contrast_min * 0.5)
        contrast_ok = (darkness >= min_edge) and (bg - inside_mean) >= min_edge

        # Диагностика границ: перепад яркости «внутри run -> фон за границей».
        # Плавный перепад (мала разница с фоном) указывает, что граница села
        # на тень/переходное размытие, а не на реальный край герметика.
        out_left = float(np.mean(prof_gray[max(0, s - 3):s])) if s > 0 else 0.0
        out_right = float(np.mean(prof_gray[e + 1:e + 4])) if e < prof_gray.size - 1 else 0.0
        edge_step_left = float(out_left - inside_mean)
        edge_step_right = float(out_right - inside_mean)
        width_plausible = (self.gap_min_width_mm * self.px_per_mm * 0.75
                           <= width_t
                           <= self.max_width * self.px_per_mm * 2.0)
        max_allowed_offset_px = self.pos_tolerance_mm * self.px_per_mm + self.max_width * self.px_per_mm * 0.5
        offset_ok = abs(mid_t) <= max_allowed_offset_px

        # Проверка резкости границ (тень имеет плавный градиент, шов - резкий)
        edge_sharpness_left = abs(edge_step_left)
        edge_sharpness_right = abs(edge_step_right)

        min_edge_sharpness = max(10.0, self.edge_contrast_min * 0.4)

        # sharp_edges: обе границы резкие (используется для confidence)
        sharp_edges = (edge_sharpness_left >= min_edge_sharpness and
                       edge_sharpness_right >= min_edge_sharpness)

        # one_edge_sharp: хотя бы одна граница различима (используется для credibility)
        one_edge_sharp = (edge_sharpness_left >= min_edge_sharpness or
                          edge_sharpness_right >= min_edge_sharpness)

        # Проверка глубины "колодца" профиля (у шва центр темнее краев, у тени - плавный спад)
        depth_ok = False
        profile_depth = 0.0
        if s > 3 and e < len(prof_gray) - 3:
            left_bg = float(np.mean(prof_gray[max(0, s - 5):s]))
            right_bg = float(np.mean(prof_gray[e + 1:min(len(prof_gray), e + 6)]))
            profile_depth = min(left_bg, right_bg) - inside_mean
            # Порог глубины depends on локального контраста
            depth_threshold = max(10.0, self.edge_contrast_min * 0.6)
            depth_ok = profile_depth >= depth_threshold
        else:
            depth_ok = False

        # Достоверность: хотя бы одна граница должна быть различима,
        # но не требуем одинаковой резкости обеих.
        credibility_check = (contrast_ok and width_plausible and offset_ok and
                             depth_ok and one_edge_sharp)

        if not credibility_check:
            result['invalid'] = True
            result['is_gap'] = False
            result['edge_sharpness_left'] = float(edge_sharpness_left)
            result['edge_sharpness_right'] = float(edge_sharpness_right)
            result['profile_depth'] = float(profile_depth)
            result['sharp_edges'] = bool(sharp_edges)
            result['depth_ok'] = bool(depth_ok)
            result['confidence'] = 0.0
            result['reason'] = 'measurement not credible (contrast=%s, width=%s, offset=%s, sharp=%s, depth=%s)' % (
                contrast_ok, width_plausible, offset_ok, sharp_edges, depth_ok)
            return result

        # Confidence: контраст до 0.30, ширина до 0.20, положение до 0.20,
        # резкость границ до 0.15, глубина профиля до 0.15.
        conf = 0.0
        if darkness >= self.edge_contrast_min:
            conf += 0.30
        elif darkness >= min_edge:
            conf += 0.15
        if width_plausible:
            conf += 0.20
        if offset_ok:
            conf += 0.20
        if sharp_edges:
            conf += 0.15
        if depth_ok:
            conf += 0.15
        result['confidence'] = float(min(1.0, conf))

        # Порог достоверности: измерение с низким confidence считается невалидным,
        # но не автоматически разрывом — значение ширины сохраняется для диагностики.
        if result['confidence'] < self.min_confidence:
            result['invalid'] = True
            result['is_gap'] = False
            result['edge_sharpness_left'] = float(edge_sharpness_left)
            result['edge_sharpness_right'] = float(edge_sharpness_right)
            result['profile_depth'] = float(profile_depth)
            result['sharp_edges'] = bool(sharp_edges)
            result['depth_ok'] = bool(depth_ok)
            result['reason'] = 'low confidence %.2f < %.2f' % (result['confidence'], self.min_confidence)
            return result

        result['width_px'] = float(width_t)
        result['offset_px'] = float(mid_t)
        result['width_mm'] = float(width_t / self.px_per_mm)
        result['offset_mm'] = float(mid_t / self.px_per_mm)
        result['left_t'] = float(t_arr[s])
        result['right_t'] = float(t_arr[e])
        result['bg'] = float(bg)
        result['inside_mean'] = float(inside_mean)
        result['darkness'] = float(darkness)
        result['edge_step_left'] = edge_step_left
        result['edge_step_right'] = edge_step_right
        result['edge_sharpness_left'] = float(edge_sharpness_left)
        result['edge_sharpness_right'] = float(edge_sharpness_right)
        result['profile_depth'] = float(profile_depth if s > 3 and e < len(prof_gray) - 3 else 0.0)
        result['sharp_edges'] = bool(sharp_edges)
        result['depth_ok'] = bool(depth_ok)
        result['boundary1'] = (float(cx + t_arr[s] * nx), float(cy + t_arr[s] * ny))
        result['boundary2'] = (float(cx + t_arr[e] * nx), float(cy + t_arr[e] * ny))

        result['valid'] = True
        result['is_gap'] = False
        result['reason'] = 'ok'
        return result
    # ------------------------------------------------------------------ #
    # Основной цикл обработки
    # ------------------------------------------------------------------ #
    def process(self, image):
        detail = {
            'ok': False,
            'width_mm': 0.0,
            'error_code': self.ERR_NONE,
            'error_messages': [],
            'state': self.STATE_INVALID,
            'invalid': True,
            'slices': [],
            'centerline': [],
            'bbox': None,
            'max_gap_mm': 0.0,
            'fill_ratio': 0.0,
            'fill_ratio_total': 0.0,
            'mean_width_mm': 0.0,
            'median_width_mm': 0.0,
            'min_width_mm': 0.0,
            'max_width_mm': 0.0,
            'std_width_mm': 0.0,
            'expected_length_mm': 0.0,
            'checked_slices': 0,
            'valid_slices': 0,
            'gap_slices': 0,
            'excluded_slices': 0,
            'annotated_image': None,
            'timings': {},
        }
        timings = detail['timings']

        if image is None or image.size == 0:
            detail['error_messages'].append('INVALID: Empty image')
            detail['error_code'] = self.ERR_INVALID
            self.last_detail = detail
            return False, 0.0, 'INVALID: Empty image', detail

        if self.px_per_mm <= 0:
            detail['error_messages'].append('INVALID: Invalid px_per_mm calibration')
            detail['error_code'] = self.ERR_INVALID
            self.last_detail = detail
            return False, 0.0, 'INVALID: Invalid px_per_mm calibration', detail

        self._timer(timings, 't0')
        cropped, offset_x, offset_y = self._crop_to_roi(image)
        if cropped is None or cropped.size == 0:
            detail['error_messages'].append('INVALID: Empty ROI')
            detail['error_code'] = self.ERR_INVALID
            self.last_detail = detail
            return False, 0.0, 'INVALID: Empty ROI', detail

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

        # Дополнительная проверка локального контраста: тени имеют низкий
        # локальный контраст, тогда как шов — тёмный объект на более ярком фоне.
        local_contrast = cv2.adaptiveThreshold(
            blur_for_contrast, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 15, 10
        )
        local_contrast_ratio = float(cv2.countNonZero(local_contrast) / local_contrast.size)

        if (contrast_std < self.min_contrast_std and
                contrast_range < 2 * self.min_contrast_std) or \
                local_contrast_ratio < 0.05:
            detail['error_messages'].append('INVALID: low contrast, no reliable data')
            detail['error_code'] = self.ERR_INVALID
            self.last_detail = detail
            return False, 0.0, 'INVALID: low contrast', detail

        self._timer(timings, 't_prepare')

        # CLAHE + бинаризация (шов тёмнее фона -> белый на маске)
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        blur = cv2.GaussianBlur(enhanced, (5, 5), 0)
        if self.use_otsu:
            otsu_val, _ = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            tight_val = min(220, int(otsu_val * 1.05))
            _, seg_raw = cv2.threshold(blur, tight_val, 255, cv2.THRESH_BINARY_INV)
        else:
            _, seg_raw = cv2.threshold(blur, self.threshold, 255, cv2.THRESH_BINARY_INV)

        # Морфологическое открытие удаляет тонкие тени и шум без замыкания разрывов.
        # seg_raw остаётся исходной маской для анализа разрывов;
        # seg_processed используется для измерений.
        seg_processed = seg_raw.copy()
        if self.use_shadow_opening:
            kernel_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
            seg_processed = cv2.morphologyEx(seg_processed, cv2.MORPH_OPEN, kernel_open, iterations=1)

        # маски игнорирования и отверстия наносятся на обработанную сегментацию;
        # seg_raw остаётся эталоном для анализа разрывов (разрывы не замазываются)
        seg = self._apply_ignore_mask(seg_processed, offset_x, offset_y)
        seg = self._mask_central_hole(gray, seg)
        seg = self._mask_shadow_zones(gray, seg)  # маскирование зон плавной тени

        # Дополнительное замыкание для восстановления шва на неровных участках
        kernel_close_small = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        seg = cv2.morphologyEx(seg, cv2.MORPH_CLOSE, kernel_close_small, iterations=1)

        self._timer(timings, 't_segmentation')

        # центрная линия (эталон приоритетен, иначе автоматика для кривой)
        centerline_local = self._build_centerline(seg, gray, offset_x, offset_y)
        align_rejected = False
        if self.centerline_px is not None and centerline_local is not None and not self.skip_centerline_align:
            centerline_local, align_rejected = self._align_centerline_by_hole(
                gray, seg, centerline_local, offset_x, offset_y)
        if centerline_local is None or len(centerline_local) < 2:
            msg = 'INVALID: Seam centerline not found'
            detail['error_messages'].append(msg)
            detail['error_code'] = self.ERR_NO_SEAM | self.ERR_INVALID
            self.last_detail = detail
            return False, 0.0, msg, detail

        if align_rejected:
            detail['error_messages'].append('INVALID: part misaligned vs etalon (shift exceeds tolerance)')
            detail['error_code'] = self.ERR_INVALID
            self.last_detail = detail
            return False, 0.0, 'INVALID: part misaligned vs etalon', detail

        # Сегментация для измерения: заливаем только обоснованные дыры от блика
        k = max(1, self.morph_kernel_size)
        if k % 2 == 0:
            k += 1
        kernel_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        seg_closed = cv2.morphologyEx(seg, cv2.MORPH_CLOSE, kernel_close)
        seg_measure = self._fill_specular_glare(seg_closed, centerline_local)

        # нормали к оси
        normals = self._normals_along(centerline_local)

        # Вычисляем кривизну для адаптивной длины среза
        curvatures = []
        for i in range(len(centerline_local)):
            if 0 < i < len(centerline_local) - 1:
                p1 = np.array(centerline_local[i - 1])
                p2 = np.array(centerline_local[i])
                p3 = np.array(centerline_local[i + 1])
                v1 = p2 - p1
                v2 = p3 - p2
                cross = abs(v1[0] * v2[1] - v1[1] * v2[0])
                curvature = cross / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-6)
                curvatures.append(curvature)
            else:
                curvatures.append(0.0)

        # Сохраняем кривизну для адаптивных порогов в _measure_slice
        self._current_curvatures = {i: curvatures[i] for i in range(len(curvatures))}

        # накопленное расстояние вдоль оси (для измерения разрывов в мм)
        cum_dist = [0.0]
        for i in range(1, len(centerline_local)):
            p1 = centerline_local[i - 1]
            p2 = centerline_local[i]
            cum_dist.append(cum_dist[-1] + float(np.hypot(p2[0] - p1[0], p2[1] - p1[1])))
        expected_length_mm = cum_dist[-1] / self.px_per_mm

        # параметры среза
        search_margin_mm = min(self.pos_tolerance_mm, 1.2)
        physical_half_len = int((self.max_width * 0.55 + search_margin_mm) * self.px_per_mm)
        base_half_length = max(25, int(self.step_px * 1.5), physical_half_len)

        self._timer(timings, 't_centerline')

        slices = []
        widths_valid = []
        max_gap_mm = 0.0
        valid_slices = 0
        gap_slices = 0
        excluded_slices = 0
        gap_start_idx = None  # индекс среза, с которого начался текущий разрыв

        for i, center_loc in enumerate(centerline_local):
            # срез под зоной игнорирования — исключается из оценки качества
            if self._point_in_ignore_rects(center_loc[0], center_loc[1], offset_x, offset_y):
                excluded_slices += 1
                slices.append({
                    'index': i, 'center': (center_loc[0] + offset_x, center_loc[1] + offset_y),
                    'normal': normals[i], 'width_px': 0.0, 'width_mm': 0.0,
                    'offset_px': 0.0, 'offset_mm': 0.0, 'is_gap': False,
                    'invalid': True, 'excluded': True, 'valid': False,
                    'status': 'excluded', 'confidence': 0.0, 'reason': 'ignore zone',
                    'left_t': 0.0, 'right_t': 0.0, 'edge1': None, 'edge2': None,
                    'bg': 0.0, 'inside_mean': 0.0, 'darkness': 0.0,
                    'edge_step_left': 0.0, 'edge_step_right': 0.0,
                })
                # разрыв не продолжается через ignored зону: закрываем текущий
                if gap_start_idx is not None:
                    gap_len_px = cum_dist[i - 1] - cum_dist[gap_start_idx]
                    max_gap_mm = max(max_gap_mm, gap_len_px / self.px_per_mm)
                    gap_start_idx = None
                continue

            # Адаптивная длина среза: на изгибах увеличиваем до 50%
            curvature_factor = 1.0 + min(0.5, curvatures[i] * 8.0) if i < len(curvatures) else 1.0
            adaptive_half_length = int(base_half_length * curvature_factor)

            # Первая попытка измерения
            m = self._measure_slice(gray, seg_measure, center_loc, normals[i],
                                      adaptive_half_length, slice_index=i)

            # Если измерение недостоверно — итеративное уточнение
            if not m['valid'] and m['is_gap']:
                # Попытка 2: увеличить длину среза на 50%
                m2 = self._measure_slice(gray, seg_measure, center_loc, normals[i],
                                         int(adaptive_half_length * 1.5), slice_index=i)
                if m2['valid']:
                    m = m2
                else:
                    # Попытка 3: если нашли частичные границы — увеличить допуск позиции
                    if m.get('boundary1') is not None or m.get('boundary2') is not None:
                        old_tolerance = self.pos_tolerance_mm
                        self.pos_tolerance_mm *= 1.5
                        m3 = self._measure_slice(gray, seg_measure, center_loc, normals[i],
                                                 int(adaptive_half_length * 1.3), slice_index=i)
                        self.pos_tolerance_mm = old_tolerance
                        if m3['valid']:
                            m = m3
                    # Попытка 4: локальная бинаризация (фолбэк для проблемных зон)
                    if not m['valid']:
                        local_seg = self._local_threshold_slice(gray, center_loc, normals[i],
                                                                int(adaptive_half_length * 1.3))
                        if local_seg is not None:
                            m4 = self._measure_slice(gray, local_seg, center_loc, normals[i],
                                                     int(adaptive_half_length * 1.3),
                                                     local_seg_override=local_seg, slice_index=i)
                            if m4['valid']:
                                m = m4

            width_mm = m['width_mm']
            offset_mm = m['offset_mm']
            is_gap = m['is_gap']
            status = 'gap' if is_gap else ('wid' if m['valid'] and not
                       (self.min_width <= width_mm <= self.max_width) else 'ok')

            def _boundary_glob(bnd):
                if bnd is None:
                    return None
                return (bnd[0] + offset_x, bnd[1] + offset_y)

            slices.append({
                'index': i, 'center': (center_loc[0] + offset_x, center_loc[1] + offset_y),
                'normal': normals[i], 'width_px': m['width_px'], 'width_mm': width_mm,
                'offset_px': m['offset_px'], 'offset_mm': offset_mm,
                'is_gap': is_gap, 'invalid': m['invalid'], 'excluded': False,
                'valid': m['valid'], 'status': status, 'confidence': m['confidence'],
                'reason': m['reason'],
                'left_t': m['left_t'], 'right_t': m['right_t'],
                'edge1': _boundary_glob(m['boundary1']),
                'edge2': _boundary_glob(m['boundary2']),
                'bg': m['bg'], 'inside_mean': m['inside_mean'],
                'darkness': m['darkness'],
                'edge_step_left': m['edge_step_left'],
                'edge_step_right': m['edge_step_right'],
            })

            if m['valid']:
                valid_slices += 1
                widths_valid.append(width_mm)
                if gap_start_idx is not None:
                    gap_len_px = cum_dist[i - 1] - cum_dist[gap_start_idx]
                    max_gap_mm = max(max_gap_mm, gap_len_px / self.px_per_mm)
                    gap_start_idx = None
            elif is_gap:
                gap_slices += 1
                if gap_start_idx is None:
                    gap_start_idx = i
            # invalid (!is_gap) — недостоверно: не валидно, не считается разрывом по длине

        # закрыть разрыв, дотянувшийся до конца оси
        if gap_start_idx is not None:
            gap_len_px = cum_dist[-1] - cum_dist[gap_start_idx]
            max_gap_mm = max(max_gap_mm, gap_len_px / self.px_per_mm)

        # Интерполяция недостоверных срезов между валидными
        # (не более 3 пропущенных срезов подряд, не в зонах игнорирования)
        for i in range(len(slices)):
            sl = slices[i]
            if sl.get('invalid') and not sl.get('excluded') and not sl.get('valid'):
                left_valid = None
                right_valid = None
                for j in range(i - 1, -1, -1):
                    if slices[j].get('valid') and not slices[j].get('excluded'):
                        left_valid = slices[j]
                        break
                for j in range(i + 1, len(slices)):
                    if slices[j].get('valid') and not slices[j].get('excluded'):
                        right_valid = slices[j]
                        break
                if left_valid is not None and right_valid is not None:
                    gap_idx = i - left_valid['index']
                    if gap_idx <= 3:
                        interpolated_width = (left_valid['width_mm'] + right_valid['width_mm']) / 2.0
                        sl['width_mm'] = interpolated_width
                        sl['width_px'] = interpolated_width * self.px_per_mm
                        sl['offset_mm'] = 0.0
                        sl['offset_px'] = 0.0
                        sl['valid'] = True
                        sl['is_gap'] = False
                        sl['invalid'] = False
                        sl['status'] = 'ok'
                        sl['reason'] = 'interpolated'
                        sl['confidence'] = 0.5

        # Пересчитать статистику после интерполяции
        widths_valid = []
        valid_slices = 0
        gap_slices = 0
        max_gap_mm = 0.0
        gap_start_idx = None
        for i, sl in enumerate(slices):
            if sl.get('excluded'):
                continue
            if sl.get('valid'):
                valid_slices += 1
                widths_valid.append(sl['width_mm'])
                if gap_start_idx is not None:
                    gap_len_px = cum_dist[i - 1] - cum_dist[gap_start_idx]
                    max_gap_mm = max(max_gap_mm, gap_len_px / self.px_per_mm)
                    gap_start_idx = None
            elif sl.get('is_gap'):
                gap_slices += 1
                if gap_start_idx is None:
                    gap_start_idx = i
        if gap_start_idx is not None:
            gap_len_px = cum_dist[-1] - cum_dist[gap_start_idx]
            max_gap_mm = max(max_gap_mm, gap_len_px / self.px_per_mm)

        checked_slices = len(centerline_local) - excluded_slices
        fill_ratio = (valid_slices / checked_slices) if checked_slices > 0 else 0.0
        fill_ratio_total = (valid_slices / len(centerline_local)) if len(centerline_local) > 0 else 0.0

        self._timer(timings, 't_slices')

        # --- статистика ширины: ИСХОДНЫЕ измерения, без сглаживания.
        # Допуски и агрегаты считаются по сырым значениям каждого среза;
        # медианный фильтр (окно 3) используется только как отдельный тренд.
        all_gaps = checked_slices > 0 and valid_slices == 0 and gap_slices > 0
        w_filtered = None
        if widths_valid:
            w_arr = np.array(widths_valid, dtype=np.float64)
            if len(w_arr) >= 3:
                w_filtered = np.copy(w_arr)
                for idx in range(1, len(w_arr) - 1):
                    w_filtered[idx] = np.median(w_arr[idx - 1:idx + 2])
            mean_w = float(np.mean(w_arr))
            median_w = float(np.median(w_arr))
            min_w = float(np.min(w_arr))
            max_w = float(np.max(w_arr))
            std_w = float(np.std(w_arr))
        else:
            mean_w = median_w = min_w = max_w = std_w = 0.0

        # --- позиции: локальный контроль смещения на каждом срезе ---
        offsets_valid = [abs(s['offset_mm']) for s in slices if s['valid']]
        max_offset_mm = float(np.max(offsets_valid)) if offsets_valid else 0.0
        if len(offsets_valid) >= 5:
            p90_offset = float(np.percentile(offsets_valid, 90))
        elif offsets_valid:
            p90_offset = float(np.mean(offsets_valid))
        else:
            p90_offset = 0.0

        # --- критерии брака ---
        error_code = self.ERR_NONE
        error_messages = []
        invalid = False

        # Адаптивный порог: для U-образных швов с десятками срезов 3 — слишком строго
        min_valid = max(3, int(0.1 * len(centerline_local)))

        if checked_slices == 0:
            invalid = True
            error_code |= self.ERR_INVALID
            error_messages.append('INVALID: no measurable slices')
        elif valid_slices < min_valid:
            invalid = True
            error_code |= self.ERR_INVALID
            error_messages.append(
                'INVALID: insufficient valid measurements (%d < %d)' % (valid_slices, min_valid))
        elif all_gaps:
            error_code |= self.ERR_NO_SEAM
            error_messages.append('NOK: no seam detected in any slice')
        else:
            if max_gap_mm > self.max_allowed_gap_mm:
                error_code |= self.ERR_GAP
                error_messages.append('ERR_GAP: max gap %.2fmm > %.2fmm' %
                                      (max_gap_mm, self.max_allowed_gap_mm))
            if fill_ratio < self.min_fill_ratio:
                error_code |= self.ERR_GAP
                error_messages.append('ERR_GAP: fill ratio %.2f < %.2f' %
                                      (fill_ratio, self.min_fill_ratio))
            if widths_valid:
                # Сырые мин/макс — для честной диагностики (ошибка не выставляется
                # по ним автоматически, см. проверку ниже).
                detail['min_width_mm_raw'] = float(min_w)
                detail['max_width_mm_raw'] = float(max_w)
                # ERR_WID-брак подтверждается только при нарушении на >= 3 срезах
                # (по медианно-сглаженному массиву), чтобы одиночный пик в месте
                # замка/стыка замкнутого шва не браковал всю деталь.
                viol_count = 0
                if w_filtered is not None:
                    viol_mask = (w_filtered < self.min_width) | (w_filtered > self.max_width)
                    viol_count = int(np.sum(viol_mask))
                else:
                    viol_count = (1 if min_w < self.min_width else 0) + \
                                 (1 if max_w > self.max_width else 0)
                if viol_count >= 3:
                    error_code |= self.ERR_WID
                    if min_w < self.min_width:
                        error_messages.append('ERR_WID: min width %.2fmm < %.2fmm' %
                                              (min_w, self.min_width))
                    if max_w > self.max_width:
                        error_messages.append('ERR_WID: max width %.2fmm > %.2fmm' %
                                              (max_w, self.max_width))
            if p90_offset > self.pos_tolerance_mm:
                error_code |= self.ERR_POS
                error_messages.append('ERR_POS: offset p90 %.2fmm > %.2fmm' %
                                      (p90_offset, self.pos_tolerance_mm))
            if max_offset_mm > self.pos_tolerance_mm * 2.0:
                error_messages.append('WARN: max local offset %.2fmm' % max_offset_mm)

        # Проверка согласованности эталона и действующих допусков (без подгонки)
        etalon_mean = self._etalon_mean_width()
        if etalon_mean and widths_valid and not invalid:
            if etalon_mean < self.min_width or etalon_mean > self.max_width:
                error_messages.append(
                    'WARN: etalon mean width %.2fmm outside current limits %.2f-%.2fmm '
                    '(check limits source %s / calibration)' %
                    (etalon_mean, self.min_width, self.max_width, self.limits_source))

        ok = (error_code == self.ERR_NONE) and not invalid
        state = self.STATE_INVALID if invalid else (self.STATE_OK if ok else self.STATE_NOK)
        msg = '; '.join(error_messages) if error_messages else ('OK' if ok else state)

        # Диагностические кадры — только при включённом режиме (экономия памяти)
        debug_images = None
        if self.save_diagnostics:
            roi_bgr = gray
            if roi_bgr.ndim == 2:
                roi_bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
            debug_images = {
                'roi_bgr': roi_bgr,
                'seg_raw': seg_raw,
                'seg_masked': seg,
                'seg_final': seg_measure,
                'offset_x': offset_x,
                'offset_y': offset_y,
                'centerline_local': centerline_local,
                'slices': slices,
            }

        detail.update({
            'ok': ok,
            'state': state,
            'invalid': invalid,
            'width_mm': mean_w if widths_valid else 0.0,
            'median_width_mm': median_w,
            'error_code': error_code,
            'error_messages': error_messages,
            'slices': slices,
            'centerline': [(pt[0] + offset_x, pt[1] + offset_y) for pt in centerline_local],
            'bbox': None,
            'max_gap_mm': float(max_gap_mm),
            'fill_ratio': float(fill_ratio),
            'fill_ratio_total': float(fill_ratio_total),
            'mean_width_mm': float(mean_w),
            'median_width_mm': float(median_w),
            'min_width_mm': float(min_w),
            'max_width_mm': float(max_w),
            'std_width_mm': float(std_w),
            'mean_filtered_mm': float(np.mean(w_filtered)) if w_filtered is not None else 0.0,
            'widths_raw_mm': [float(w) for w in widths_valid],
            'widths_filtered_mm': ([float(w) for w in w_filtered]
                                   if w_filtered is not None else []),
            'expected_length_mm': float(expected_length_mm),
            'checked_slices': int(checked_slices),
            'valid_slices': int(valid_slices),
            'gap_slices': int(gap_slices),
            'excluded_slices': int(excluded_slices),
            'allowable': {
                'min_width_mm': float(self.min_width),
                'max_width_mm': float(self.max_width),
                'max_allowed_gap_mm': float(self.max_allowed_gap_mm),
                'pos_tolerance_mm': float(self.pos_tolerance_mm),
                'limits_source': self.limits_source,
            },
            'calibration': {
                'px_per_mm': float(self.px_per_mm),
                'roi_px': [int(cropped.shape[1]), int(cropped.shape[0])],
                'roi_mm': [float(cropped.shape[1] / self.px_per_mm),
                           float(cropped.shape[0] / self.px_per_mm)],
            },
        })
        if debug_images is not None:
            detail['debug'] = debug_images
        self.last_detail = detail
        self.last_timings = timings
        self._timer(timings, 't1')
        timings['total_ms'] = (timings.get('t1', 0) - timings.get('t0', 0)) * 1000.0
        keys = ['t0', 't_prepare', 't_segmentation', 't_centerline', 't_slices', 't1']
        staged = []
        prev = None
        for k in keys:
            if k in timings:
                cur = timings[k]
                if prev is not None:
                    staged.append((k, (cur - prev) * 1000.0))
                prev = cur
        detail['timings_staged_ms'] = staged
        return ok, mean_w if widths_valid else 0.0, msg, detail

    # ------------------------------------------------------------------ #
    # Отрисовка результата
    # ------------------------------------------------------------------ #
    def draw_result(self, image, detail=None, ok=None, width_mm=None):
        """Отрисовка диагностической разметки на полном исходном кадре."""
        result_img = image.copy()
        if detail is None:
            detail = self.last_detail or {}

        if self.roi:
            rx, ry, rw, rh = self.roi
            cv2.rectangle(result_img, (rx, ry), (rx + rw, ry + rh), (255, 255, 0), 1)
            cv2.putText(result_img, 'ROI', (rx + 5, ry + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

        # зоны игнорирования: локальные координаты эталона -> глобальные кадра
        if self.ignore_rects:
            if self.annotation_roi is not None:
                ox, oy = self.annotation_roi[0], self.annotation_roi[1]
            elif self.roi is not None:
                ox, oy = self.roi[0], self.roi[1]
            else:
                ox, oy = 0, 0
            for (rx, ry, rw, rh) in self.ignore_rects:
                x0 = max(0, ox + int(rx))
                y0 = max(0, oy + int(ry))
                x1 = min(result_img.shape[1], x0 + max(0, int(rw)))
                y1 = min(result_img.shape[0], y0 + max(0, int(rh)))
                if x1 > x0 and y1 > y0:
                    cv2.rectangle(result_img, (x0, y0), (x1, y1), (128, 128, 128), -1)
                    cv2.putText(result_img, 'IGNORE', (x0 + 3, y0 + 15),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

        centerline = detail.get('centerline', [])
        slices = detail.get('slices', [])
        error_code = detail.get('error_code', self.ERR_NONE)
        state = detail.get('state', self.STATE_NOK)

        if len(centerline) >= 2:
            for i in range(len(centerline) - 1):
                cv2.line(result_img, (int(centerline[i][0]), int(centerline[i][1])),
                         (int(centerline[i + 1][0]), int(centerline[i + 1][1])),
                         (0, 255, 255), 1)

        for sl in slices:
            center = sl['center']
            if isinstance(center, (list, tuple)) and len(center) >= 2:
                cx, cy = int(center[0]), int(center[1])
            elif isinstance(center, (int, float)):
                cx, cy = int(center), 0
            else:
                cx, cy = 0, 0
            nx, ny = sl['normal']

            # Цвет статуса среза:
            #   зелёный   — достоверное измерение в допуске;
            #   красный   — подтверждённое нарушение минимума;
            #   жёлтый    — подтверждённое нарушение максимума;
            #   оранжевый — недостоверное измерение (не дефект);
            #   серый     — срез в зоне игнорирования; красный круг — разрыв.
            if sl.get('excluded'):
                color = (128, 128, 128)
                thickness = 1
            elif sl['is_gap'] and not sl.get('valid'):
                color = (0, 0, 255)
                thickness = 2
            elif not sl.get('valid') or sl.get('invalid'):
                color = (0, 128, 255)
                thickness = 2
            elif sl.get('status') == 'ok':
                color = (0, 255, 0)
                thickness = 1
            else:
                color = (0, 0, 255) if sl.get('width_mm', 0.0) < self.min_width \
                    else (0, 255, 255)
                thickness = 2

            e1 = sl.get('edge1')
            e2 = sl.get('edge2')
            has_edges = (isinstance(e1, (list, tuple)) and len(e1) >= 2 and
                         isinstance(e2, (list, tuple)) and len(e2) >= 2 and
                         sl.get('valid'))
            if has_edges:
                # измеренный отрезок между реальными границами шва;
                # маркеры границ — разных цветов (чтобы не путать):
                #   magenta — левая граница (edge1), белая — правая (edge2)
                x1, y1 = int(round(e1[0])), int(round(e1[1]))
                x2, y2 = int(round(e2[0])), int(round(e2[1]))
                cv2.line(result_img, (x1, y1), (x2, y2), color, thickness)
                cv2.circle(result_img, (x1, y1), 2, (255, 0, 255), -1)
                cv2.circle(result_img, (x2, y2), 2, (255, 255, 255), -1)
            else:
                # без достоверных границ рисуем индикатор вокруг центра среза
                half_len = max(12, int(self.step_px * 0.8))
                if sl['is_gap']:
                    cv2.circle(result_img, (cx, cy), 4, color, -1)
                    cv2.putText(result_img, 'GAP: %.1fmm' % sl['width_mm'],
                                (cx + 6, cy - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                                color, 1)
                x1 = int(round(cx - half_len * nx))
                y1 = int(round(cy - half_len * ny))
                x2 = int(round(cx + half_len * nx))
                y2 = int(round(cy + half_len * ny))
                cv2.line(result_img, (x1, y1), (x2, y2), color, thickness)

            # Диагностический текст по срезу (только в режиме сохранения диагностики)
            if self.save_diagnostics:
                conf = sl.get('confidence', 0.0)
                esl = sl.get('edge_sharpness_left', 0.0)
                esr = sl.get('edge_sharpness_right', 0.0)
                pd = sl.get('profile_depth', 0.0)
                reason = sl.get('reason', '')
                invalid = sl.get('invalid', False)
                is_gap = sl.get('is_gap', False)
                valid = sl.get('valid', False)

                # Форматирование показателей: N/A если не рассчитывались (нули по умолчанию)
                esl_text = '%.1f' % esl if esl > 0 else 'N/A'
                esr_text = '%.1f' % esr if esr > 0 else 'N/A'
                pd_text = '%.1f' % pd if pd > 0 else 'N/A'
                diag_text = 'c=%.2f sL=%s sR=%s d=%s' % (conf, esl_text, esr_text, pd_text)

                # Цвет по статусу среза
                if invalid or (is_gap and not valid):
                    diag_color = (0, 128, 255)  # синий/серый — INVALID
                elif conf >= self.min_confidence and valid:
                    diag_color = (0, 255, 0)  # зелёный — достоверно
                elif conf >= self.min_confidence * 0.5:
                    diag_color = (0, 255, 255)  # жёлтый — погранично
                else:
                    diag_color = (0, 0, 255)  # красный — дефект

                cv2.putText(result_img, diag_text, (cx + 6, cy + 12),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, diag_color, 1)
                if reason and reason != 'ok':
                    cv2.putText(result_img, reason[:22], (cx + 6, cy + 26),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.35, diag_color, 1)

        is_ok = detail.get('ok', False) if ok is None else bool(ok)
        w_text = detail.get('width_mm', 0.0) if width_mm is None else float(width_mm)
        state_text = detail.get('state', 'NOK' if not is_ok else 'OK')
        color = (0, 255, 0) if state_text == self.STATE_OK else \
            ((0, 255, 255) if state_text == self.STATE_INVALID else (0, 0, 255))
        allowable = detail.get('allowable') or {}
        lim_min = allowable.get('min_width_mm', self.min_width)
        lim_max = allowable.get('max_width_mm', self.max_width)
        head_line = '%s: mean=%.2fmm' % (state_text, w_text)
        stats_line = 'min=%.2f max=%.2f mm (limits %.2f-%.2f)' % (
            detail.get('min_width_mm', 0.0), detail.get('max_width_mm', 0.0),
            lim_min, lim_max)
        gap_line = 'gap=%.2fmm' % detail.get('max_gap_mm', 0.0)
        cv2.putText(result_img, head_line, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        cv2.putText(result_img, stats_line, (10, 52),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(result_img, gap_line, (10, 70),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        if error_code & self.ERR_GAP:
            cv2.putText(result_img, 'I_ERR_GAP', (10, 92),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        if error_code & self.ERR_WID:
            cv2.putText(result_img, 'I_ERR_WID', (10, 117),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        if error_code & self.ERR_POS:
            cv2.putText(result_img, 'I_ERR_POS', (10, 142),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
        if error_code & self.ERR_NO_SEAM:
            cv2.putText(result_img, 'NO SEAM', (10, 167),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        if error_code & self.ERR_INVALID:
            cv2.putText(result_img, 'INVALID', (10, 192),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)

        detail['annotated_image'] = result_img
        return result_img

    def draw_result_backward_compat(self, image, bbox, ok, width_mm):
        """Старая сигнатура для обратной совместимости."""
        return self.draw_result(image, ok=ok, width_mm=width_mm)

        return False
