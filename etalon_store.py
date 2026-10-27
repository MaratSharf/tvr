import json
import os
import tempfile
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any


class EtalonStore:
    """
    Управление эталонными кадрами и их разметкой.

    Структура:
        data/etalon/<product_id>/
            index.json               — кэш-индекс группы
            ET_<YYYYMMDD_HHMMSS>_<width>mm.png   — изображение (обрезанное по ROI)
            ET_<YYYYMMDD_HHMMSS>_<width>mm.json  — sidecar с разметкой и метриками

    Sidecar JSON схема:
        file, product_id, label, defect_type, note, timestamp,
        calibration {px_per_mm, roi, threshold, use_otsu, exposure, gain},
        annotation {centerline_px, ignore_rects, step_px,
                    max_allowed_gap_mm, pos_tolerance_mm},
        metrics {mean_width_mm, min_width_mm, max_width_mm,
                 expected_length_mm, slice_widths_mm}
    """

    def __init__(self, base_folder=None, config=None):
        if base_folder is None:
            if config is not None:
                base_folder = config.get('training', {}).get('folder', 'data/etalon')
            else:
                base_folder = 'data/etalon'
        self.base_folder = base_folder
        os.makedirs(self.base_folder, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Пути
    # ------------------------------------------------------------------ #
    def product_folder(self, product_id):
        return os.path.join(self.base_folder, product_id)

    def ensure_product(self, product_id):
        folder = self.product_folder(product_id)
        os.makedirs(folder, exist_ok=True)
        return folder

    def _index_path(self, product_id):
        return os.path.join(self.product_folder(product_id), 'index.json')

    # ------------------------------------------------------------------ #
    # Индекс группы
    # ------------------------------------------------------------------ #
    def _load_index(self, product_id):
        path = self._index_path(product_id)
        if os.path.exists(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {'etalons': []}
        return {'etalons': []}

    def _save_index(self, product_id, index):
        path = self._index_path(product_id)
        folder = self.product_folder(product_id)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(index, f, indent=4, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # ------------------------------------------------------------------ #
    # Атомарное сохранение эталона
    # ------------------------------------------------------------------ #
    def save_etalon(self, product_id, image_bgr, label, config, annotation=None,
                    defect_type='none', note=''):
        """
        Сохраняет эталон и sidecar JSON атомарно.

        image_bgr — кадр уже обрезанный по ROI (в координатах ROI).
        config — dict с параметрами калибровки (px_per_mm, roi, threshold, ...).
        annotation — dict с данными разметки (centerline_px, ignore_rects, ...).
        """
        import cv2
        folder = self.ensure_product(product_id)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        width_mm = 0.0
        if annotation and 'metrics' in annotation:
            width_mm = annotation['metrics'].get('mean_width_mm', 0.0)
        base_name = 'ET_%s_%.2fmm' % (ts, width_mm)

        img_path = os.path.join(folder, base_name + '.png')
        json_path = os.path.join(folder, base_name + '.json')

        ok_img = False
        if image_bgr is not None and image_bgr.size > 0:
            ok_img = cv2.imwrite(img_path, image_bgr)

        seam_cfg = config.get('seam_control', {}) if config else {}
        training_cfg = config.get('training', {}) if config else {}
        camera_cfg = config.get('camera', {}) if config else {}

        roi_cfg = config.get('roi', {}) if config else {}
        roi_list = [0, 0, 0, 0]
        if isinstance(roi_cfg, dict):
            roi_list = [roi_cfg.get('x', 0), roi_cfg.get('y', 0),
                        roi_cfg.get('w', 0), roi_cfg.get('h', 0)]

        sidecar = {
            'file': base_name + '.png',
            'product_id': product_id,
            'label': label,
            'defect_type': defect_type,
            'note': note,
            'timestamp': ts,
            'calibration': {
                'px_per_mm': seam_cfg.get('px_per_mm', 50.0),
                'roi': roi_list,
                'threshold': seam_cfg.get('threshold', 120),
                'use_otsu': seam_cfg.get('use_otsu', True),
                'exposure': camera_cfg.get('exposure', 10000),
                'gain': camera_cfg.get('gain', 1.0),
            },
            'annotation': annotation or {},
            'metrics': (annotation or {}).get('metrics', {}),
        }

        fd, tmp = tempfile.mkstemp(dir=folder, suffix='.json.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(sidecar, f, indent=4, ensure_ascii=False)
            os.replace(tmp, json_path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

        index = self._load_index(product_id)
        entry = {
            'file': base_name + '.png',
            'json': base_name + '.json',
            'label': label,
            'defect_type': defect_type,
            'timestamp': ts,
            'mean_width_mm': width_mm,
        }
        index.setdefault('etalons', []).append(entry)
        self._save_index(product_id, index)

        return {
            'image_path': img_path if ok_img else None,
            'json_path': json_path,
            'base_name': base_name,
        }

    # ------------------------------------------------------------------ #
    # Чтение эталонов
    # ------------------------------------------------------------------ #
    def list_etalons(self, product_id):
        """Возвращает список записей эталонов для продукта."""
        index = self._load_index(product_id)
        return index.get('etalons', [])

    def load_etalon(self, product_id, file_name):
        """Загружает изображение и sidecar JSON эталона."""
        import cv2
        folder = self.product_folder(product_id)
        img_path = os.path.join(folder, file_name)
        json_name = os.path.splitext(file_name)[0] + '.json'
        json_path = os.path.join(folder, json_name)

        image = None
        if os.path.exists(img_path):
            image = cv2.imread(img_path)

        sidecar = None
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                sidecar = json.load(f)

        return image, sidecar

    # ------------------------------------------------------------------ #
    # Статистика автодопусков (медиана + MAD)
    # ------------------------------------------------------------------ #
    def compute_autotolerances(self, product_id, config=None):
        """
        По всем эталонам с label == "ok" внутри product_id рассчитывает
        рекомендуемые допуски ширины через медиану и MAD.
        """
        import numpy as np
        etalons = self.list_etalons(product_id)
        widths = []
        for entry in etalons:
            if entry.get('label') == 'ok':
                mw = entry.get('mean_width_mm')
                if mw is not None and mw > 0:
                    widths.append(float(mw))

        if not widths:
            return None

        arr = np.array(widths, dtype=np.float64)
        median = float(np.median(arr))
        mad = float(np.median(np.abs(arr - median)))
        sigma = 1.4826 * mad

        seam_cfg = config.get('seam_control', {}) if config else {}
        hard_min = float(seam_cfg.get('min_width_mm', 1.0))
        hard_max = float(seam_cfg.get('max_width_mm', 6.0))

        k = 2.0
        rec_min = max(hard_min, median - k * sigma)
        rec_max = min(hard_max, median + k * sigma)

        return {
            'count': len(widths),
            'median_mm': median,
            'mad_mm': mad,
            'sigma_mm': sigma,
            'mean_mm': float(np.mean(arr)),
            'std_mm': float(np.std(arr)),
            'recommended_min_mm': rec_min,
            'recommended_max_mm': rec_max,
            'hard_min_mm': hard_min,
            'hard_max_mm': hard_max,
        }