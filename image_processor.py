import cv2
import numpy as np
from typing import Optional, List, Tuple, Dict, Any


class SeamProcessor:
    """
    Измерение ширины шва и детекция разрывов методом поперечных секущих срезов.
    Улучшенная версия с подавлением блика на герметике, авто-маской центрального паза,
    равномерной интерполяцией криволинейной оси и защитой координат ROI.
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

        # Дополнительные опции (обратно совместимые с дефолтным config.json)
        self.auto_mask_hole = bool(config.get('auto_mask_hole', True))
        self.hole_threshold = int(config.get('hole_threshold', 28))
        self.fill_glare_holes = bool(config.get('fill_glare_holes', True))

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
    # Вспомогательные методы и защита координат
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

    def _to_local_coords(self, x, y, w_crop, h_crop, offset_x, offset_y):
        """
        Умный перевод координат в локальную систему ROI (cropped).
        Если координаты уже локальные (внутри 0..w_crop), не вычитает offset.
        Если глобальные (выходят за пределы или заданы в полном кадре) — вычитает offset.
        """
        if offset_x == 0 and offset_y == 0:
            return int(x), int(y)
        # Если точка явно задана в локальных координатах ROI (как в draw_result)
        if 0 <= x < w_crop and 0 <= y < h_crop:
            return int(x), int(y)
        return int(x) - offset_x, int(y) - offset_y

    def _apply_ignore_mask(self, thresh, offset_x, offset_y):
        """Закрасить игнорируемые зоны (блики/отверстия/сопло) нулями на бинарной маске."""
        if not self.ignore_rects:
            return thresh
        mask = thresh.copy()
        h_crop, w_crop = mask.shape[:2]
        for (rx, ry, rw, rh) in self.ignore_rects:
            lx, ly = self._to_local_coords(rx, ry, w_crop, h_crop, offset_x, offset_y)
            x0 = max(0, lx)
            y0 = max(0, ly)
            x1 = min(w_crop, lx + int(rw))
            y1 = min(h_crop, ly + int(rh))
            if x1 > x0 and y1 > y0:
                mask[y0:y1, x0:x1] = 0
        return mask

    def _mask_central_hole(self, gray, thresh):
        """
        Автоматическое маскирование глубоких сквозных отверстий (пазов) на детали.
        Сквозное отверстие имеет очень низкую яркость (< hole_threshold) и малую дисперсию.
        """
        if not self.auto_mask_hole:
            return thresh
        _, dark_mask = cv2.threshold(gray, self.hole_threshold, 255, cv2.THRESH_BINARY_INV)
        # Убираем мелкие темные точки на металле и сам узкий край герметика эрозией
        min_hole_radius = max(5, int(self.max_width * self.px_per_mm * 0.35))
        if min_hole_radius % 2 == 0:
            min_hole_radius += 1
        kernel_erode = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (min_hole_radius, min_hole_radius))
        hole_core = cv2.erode(dark_mask, kernel_erode)

        contours, _ = cv2.findContours(hole_core, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return thresh

        min_hole_area = (self.max_width * self.px_per_mm) ** 2 * 0.5
        hole_mask = np.zeros_like(thresh)
        found_hole = False
        for cnt in contours:
            if cv2.contourArea(cnt) > min_hole_area:
                cv2.drawContours(hole_mask, [cnt], -1, 255, cv2.FILLED)
                found_hole = True

        if not found_hole:
            return thresh

        # Возвращаем исходный размер отверстия + запас на темную фаску паза (6 px)
        expand_size = min_hole_radius + 12
        if expand_size % 2 == 0:
            expand_size += 1
        kernel_dilate = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (expand_size, expand_size))
        hole_mask = cv2.dilate(hole_mask, kernel_dilate)

        res = thresh.copy()
        res[hole_mask > 0] = 0
        return res

    def _fill_specular_glare(self, mask):
        """
        Заливка продольного блика внутри контура герметика.
        Если блик внутри темного валика создал черную дыру (0) посреди белого контура (255),
        анализ иерархии контуров закрашивает внутренние полости обратно в 255.
        """
        if not self.fill_glare_holes:
            return mask
        contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:
            return mask
        out = mask.copy()
        max_glare_area = (self.max_width * self.px_per_mm) ** 2 * 4.0
        # hierarchy[0][i][3] != -1 означает внутреннюю дыру (дочерний контур) внутри белого объекта
        for i, cnt in enumerate(contours):
            if hierarchy[0][i][3] != -1:
                area = cv2.contourArea(cnt)
                if area < max_glare_area:
                    cv2.drawContours(out, [cnt], 0, 255, cv2.FILLED)
        return out

    # ------------------------------------------------------------------ #
    # Ось шва (centerline) и интерполяция полилинии
    # ------------------------------------------------------------------ #
    def _resample_polyline(self, points, step_px):
        """
        Равномерная разбивка ломаной линии (из эталона) на точки с точным шагом step_px.
        Критически важно для корректного расчета длины разрывов (max_gap_mm) на U-образном шве.
        """
        if not points or len(points) < 2:
            return points
        pts = [np.array(p, dtype=np.float64) for p in points]
        step = max(1.0, float(step_px))

        resampled = [(int(round(pts[0][0])), int(round(pts[0][1])))]
        accum_dist = 0.0

        for i in range(len(pts) - 1):
            p1 = pts[i]
            p2 = pts[i + 1]
            seg_vec = p2 - p1
            seg_len = float(np.linalg.norm(seg_vec))
            if seg_len < 1e-6:
                continue
            unit_vec = seg_vec / seg_len

            dist_along_seg = step - accum_dist
            while dist_along_seg <= seg_len:
                new_pt = p1 + unit_vec * dist_along_seg
                resampled.append((int(round(new_pt[0])), int(round(new_pt[1]))))
                dist_along_seg += step

            accum_dist = seg_len - (dist_along_seg - step)

        if len(resampled) < 2:
            resampled.append((int(round(pts[-1][0])), int(round(pts[-1][1]))))
        return resampled

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

    def _build_centerline(self, thresh, offset_x=0, offset_y=0):
        """Построить осевую линию в локальных координатах ROI: по эталону или автоматически."""
        if self.centerline_px and len(self.centerline_px) >= 2:
            h_crop, w_crop = thresh.shape[:2]
            local_pts = [
                self._to_local_coords(px, py, w_crop, h_crop, offset_x, offset_y)
                for (px, py) in self.centerline_px
            ]
            return self._resample_polyline(local_pts, self.step_px)
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

    def _slice_width(self, thresh, gray, center, normal, half_length_px):
        """
        Измерить ширину шва вдоль нормали в точке center.
        Учитывает:
          1) Положение сегмента относительно центра среза (отбрасывает далекие тени/кромки).
          2) Возможное раздвоение мастики из-за продольного блика посередине валика.
        """
        cx, cy = center
        nx, ny = normal
        H, W = thresh.shape[:2]

        t_arr = np.arange(-half_length_px, half_length_px + 1, dtype=np.float32)
        xs = np.round(cx + t_arr * nx).astype(np.int32)
        ys = np.round(cy + t_arr * ny).astype(np.int32)

        valid_mask = (xs >= 0) & (xs < W) & (ys >= 0) & (ys < H)
        if not np.any(valid_mask):
            return 0.0, 0.0, False

        t_valid = t_arr[valid_mask]
        vals_arr = thresh[ys[valid_mask], xs[valid_mask]]
        gray_arr = gray[ys[valid_mask], xs[valid_mask]]

        white_mask = vals_arr > 0
        if not np.any(white_mask):
            return 0.0, 0.0, False

        # Находим все непрерывные белые отрезки (runs) на срезе
        white_indices = np.where(white_mask)[0]
        runs = []
        cur_start = white_indices[0]
        cur_end = white_indices[0]
        for idx in white_indices[1:]:
            if idx == cur_end + 1:
                cur_end = idx
            else:
                runs.append((cur_start, cur_end))
                cur_start = idx
                cur_end = idx
        runs.append((cur_start, cur_end))

        # Склеивание двух половинок шва, если продольный блик все же пробил черную дыру по центру:
        # если между двумя белыми участками яркий пик (блик), а сумма их ширин <= max_width * 1.2
        max_w_px = self.max_width * self.px_per_mm * 1.25
        max_glare_width_px = self.max_width * self.px_per_mm * 0.45
        merged_runs = [runs[0]]
        for r_start, r_end in runs[1:]:
            prev_start, prev_end = merged_runs[-1]
            gap_px = r_start - prev_end - 1
            total_span = r_end - prev_start + 1
            if gap_px <= max_glare_width_px and total_span <= max_w_px:
                gap_gray = gray_arr[prev_end + 1:r_start]
                edge_gray = 0.5 * (float(gray_arr[prev_end]) + float(gray_arr[r_start]))
                # Если в промежутке яркость выше краев (это блик на хребте мастики), объединяем
                if gap_gray.size > 0 and float(np.max(gap_gray)) >= edge_gray:
                    merged_runs[-1] = (prev_start, r_end)
                    continue
            merged_runs.append((r_start, r_end))

        # Выбираем отрезок, чей центр лежит в допустимом коридоре вокруг оси шва
        max_allowed_offset_px = (self.pos_tolerance_mm + self.max_width * 0.5) * self.px_per_mm
        best_run = None
        best_score = -1e9

        for r_start, r_end in merged_runs:
            w_px = float(r_end - r_start + 1)
            c_t = 0.5 * (float(t_valid[r_start]) + float(t_valid[r_end]))
            # Отсекаем посторонние объекты на краях секущей (кромка отверстия, тень сопла)
            if abs(c_t) > max_allowed_offset_px:
                continue
            # Приоритет — широкому участку вблизи ожидаемой центральной оси
            score = w_px - 0.5 * abs(c_t)
            if score > best_score:
                best_score = score
                best_run = (r_start, r_end, w_px, c_t)

        if best_run is None:
            return 0.0, 0.0, False

        _, _, width_px, center_offset_px = best_run
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

        # Смягченный CLAHE (clipLimit=2.5 вместо 5.0), чтобы не пересвечивать блик на мастике
        clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        blur = cv2.GaussianBlur(enhanced, (5, 5), 0)

        if self.use_otsu:
            val, thresh = cv2.threshold(blur, 0, 255,
                                         cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        else:
            val, thresh = cv2.threshold(blur, self.threshold, 255,
                                         cv2.THRESH_BINARY_INV)

        # 1. Исключаем пользовательские зоны игнорирования (с корректным учетом ROI)
        thresh = self._apply_ignore_mask(thresh, offset_x, offset_y)
        # 2. Автоматически маскируем сквозное черное отверстие в центре кадра
        thresh = self._mask_central_hole(blur_for_contrast, thresh)

        k = max(1, self.morph_kernel_size)
        if k % 2 == 0:
            k += 1
        kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel_close)

        # 3. Заливаем внутренние дыры от блика внутри контура герметика
        cleaned = self._fill_specular_glare(cleaned)

        centerline_local = self._build_centerline(cleaned, offset_x, offset_y)
        if centerline_local is None or len(centerline_local) < 2:
            detail['error_messages'].append("Seam centerline not found")
            detail['error_code'] = self.ERR_NO_SEAM
            self.last_detail = detail
            return False, 0.0, "NOK: Seam not found", detail

        # Длина полусреза рассчитывается от реальной макс. ширины шва в пикселях и допуска позиции
        physical_half_len = int((self.max_width * 0.65 + self.pos_tolerance_mm) * self.px_per_mm)
        half_length = max(25, int(self.step_px * 1.5), physical_half_len)

        slices = []
        widths_valid = []
        max_gap_mm = 0.0
        current_gap_slices = 0
        expected_slices = len(centerline_local)
        valid_slices = 0

        for i, center_loc in enumerate(centerline_local):
            # Сглаживание нормали по соседним узлам для плавного прохода U-образной дуги
            if 0 < i < len(centerline_local) - 1:
                normal = self._perpendicular(centerline_local[i - 1], centerline_local[i + 1])
            elif i < len(centerline_local) - 1:
                normal = self._perpendicular(centerline_local[i], centerline_local[i + 1])
            else:
                normal = self._perpendicular(centerline_local[i - 1], centerline_local[i])

            width_px, offset_px, has_white = self._slice_width(
                cleaned, blur_for_contrast, center_loc, normal, half_length)
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

            # Сохраняем глобальные координаты (с учетом offset_x, offset_y) для отрисовки на полном кадре
            global_center = (center_loc[0] + offset_x, center_loc[1] + offset_y)
            slices.append({
                'index': i,
                'center': global_center,
                'normal': normal,
                'width_px': width_px,
                'width_mm': width_mm,
                'offset_mm': offset_mm,
                'is_gap': is_gap,
                'status': status,
            })

        centerline_global = [(pt[0] + offset_x, pt[1] + offset_y) for pt in centerline_local]
        expected_length_mm = expected_slices * self.step_px / self.px_per_mm
        fill_ratio = (valid_slices / expected_slices) if expected_slices > 0 else 0.0

        if widths_valid:
            w_arr = np.array(widths_valid, dtype=np.float32)
            # Медианная фильтрация соседних срезов (окно 3), чтобы убрать одиночные выбросы на краях
            if len(w_arr) >= 5:
                w_filtered = np.copy(w_arr)
                for idx in range(1, len(w_arr) - 1):
                    w_filtered[idx] = np.median(w_arr[idx - 1:idx + 2])
                mean_w = float(np.mean(w_filtered))
                min_w = float(np.min(w_filtered[1:-1]))
                max_w = float(np.max(w_filtered[1:-1]))
            else:
                mean_w = float(np.mean(w_arr))
                min_w = float(np.min(w_arr))
                max_w = float(np.max(w_arr))
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
                # 90-й перцентиль вместо простого среднего, чтобы ловить локальный уход шва на дуге
                check_offset = float(np.percentile(non_gap_offsets, 90)) if len(non_gap_offsets) >= 5 else float(np.mean(non_gap_offsets))
                if check_offset > self.pos_tolerance_mm:
                    error_code |= self.ERR_POS
                    error_messages.append("I_ERR_POS: offset %.2fmm > %.2fmm" % (check_offset, self.pos_tolerance_mm))

        ok = (error_code == self.ERR_NONE)
        msg = "; ".join(error_messages) if error_messages else "OK"

        detail.update({
            'ok': ok,
            'width_mm': mean_w if widths_valid else 0.0,
            'error_code': error_code,
            'error_messages': error_messages,
            'slices': slices,
            'centerline': centerline_global,
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

        roi_x, roi_y = 0, 0
        roi_w, roi_h = image.shape[1], image.shape[0]
        if self.roi:
            rx, ry, rw, rh = self.roi
            roi_x, roi_y, roi_w, roi_h = int(rx), int(ry), int(rw), int(rh)
            cv2.rectangle(result_img, (roi_x, roi_y), (roi_x + roi_w, roi_y + roi_h), (255, 255, 0), 1)
            cv2.putText(result_img, "ROI", (roi_x + 5, roi_y + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

        if self.ignore_rects:
            for (rx, ry, rw, rh) in self.ignore_rects:
                lx, ly = self._to_local_coords(rx, ry, roi_w, roi_h, roi_x, roi_y)
                x0 = lx + roi_x
                y0 = ly + roi_y
                cv2.rectangle(result_img, (x0, y0), (x0 + int(rw), y0 + int(rh)), (128, 128, 128), -1)
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
            # Отображаем реальную измеренную ширину шва на срезе (или минимальный маркер для GAP)
            half_len = max(12, int(round(sl['width_px'] * 0.5))) if not sl['is_gap'] else max(12, int(self.step_px * 0.8))
            offset_px = sl['offset_mm'] * self.px_per_mm if not sl['is_gap'] else 0.0
            scx = cx + offset_px * nx
            scy = cy + offset_px * ny

            x1 = int(round(scx - half_len * nx))
            y1 = int(round(scy - half_len * ny))
            x2 = int(round(scx + half_len * nx))
            y2 = int(round(scy + half_len * ny))

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