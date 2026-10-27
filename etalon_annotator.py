import cv2
import numpy as np
import os
import tkinter as tk
from tkinter import messagebox, ttk

from image_processor import SeamProcessor
from etalon_store import EtalonStore


class EtalonAnnotator(tk.Toplevel):
    def __init__(self, parent, image_bgr, config, etalon_store, product_id,
                 on_saved=None, existing_sidecar=None):
        super().__init__(parent)
        self.title('Разметка эталона — %s' % product_id)
        self.resizable(True, True)
        self.config_data = config
        self.etalon_store = etalon_store
        self.product_id = product_id
        self.on_saved = on_saved
        self.existing_sidecar = existing_sidecar or {}
        self.base_image = image_bgr.copy() if image_bgr is not None else None
        if self.base_image is None:
            messagebox.showerror('Ошибка', 'Нет изображения для разметки')
            self.destroy()
            return
        self.h, self.w = self.base_image.shape[:2]
        self.scale = 1.0
        self.max_canvas_w = 900
        self.max_canvas_h = 700
        self._calc_scale()
        self.centerline_points = []
        self.ignore_rects = []
        self._drawing_rect = False
        self._rect_start = None
        self._rect_end = None
        self._tool = 'centerline'
        self._slices_preview = []
        ann = self.existing_sidecar.get('annotation', {}) if self.existing_sidecar else {}
        if ann.get('centerline_px'):
            self.centerline_points = [tuple(p) for p in ann['centerline_px']]
        if ann.get('ignore_rects'):
            self.ignore_rects = [list(r) for r in ann['ignore_rects']]
        self.step_px = ann.get('step_px', config.get('training', {}).get('step_px', 15))
        self.max_allowed_gap_mm = ann.get('max_allowed_gap_mm',
                                          config.get('training', {}).get('max_allowed_gap_mm', 0.0))
        self.pos_tolerance_mm = ann.get('pos_tolerance_mm',
                                        config.get('training', {}).get('pos_tolerance_mm', 2.5))
        self._build_ui()
        self._redraw()

    def _calc_scale(self):
        if self.w <= 0 or self.h <= 0:
            self.scale = 1.0
            return
        scale_w = self.max_canvas_w / float(self.w)
        scale_h = self.max_canvas_h / float(self.h)
        self.scale = min(1.0, scale_w, scale_h)
        self.canvas_w = max(1, int(self.w * self.scale))
        self.canvas_h = max(1, int(self.h * self.scale))

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        ttk.Label(toolbar, text='Инструмент:').pack(side=tk.LEFT, padx=(0, 5))
        self.tool_var = tk.StringVar(value='centerline')
        ttk.Radiobutton(toolbar, text='Траектория шва (Centerline)',
                        variable=self.tool_var, value='centerline',
                        command=self._on_tool_change).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Radiobutton(toolbar, text='Маска бликов (Ignore Zone)',
                        variable=self.tool_var, value='ignore',
                        command=self._on_tool_change).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(toolbar, text='Очистить ось', command=self._clear_centerline).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(toolbar, text='Очистить маски', command=self._clear_ignore).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Button(toolbar, text='Завершить ось', command=self._finalize_centerline).pack(side=tk.LEFT)

        canvas_frame = ttk.Frame(self)
        canvas_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.canvas = tk.Canvas(canvas_frame, width=self.canvas_w, height=self.canvas_h,
                                bg='gray', cursor='crosshair')
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        v_scroll = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL, command=self.canvas.yview)
        v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        h_scroll = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
        h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)
        self.canvas.bind('<Button-1>', self._on_canvas_click)
        self.canvas.bind('<B1-Motion>', self._on_canvas_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_canvas_release)
        self.canvas.bind('<Button-3>', self._on_canvas_right_click)

        param_frame = ttk.LabelFrame(self, text='Предпросмотр и тест разметки')
        param_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        ttk.Label(param_frame, text='Порог бинаризации:').grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.threshold_var = tk.IntVar(value=int(self.config_data.get('seam_control', {}).get('threshold', 120)))
        self.threshold_scale = tk.Scale(param_frame, from_=0, to=255, orient=tk.HORIZONTAL,
                                        variable=self.threshold_var, length=200)
        self.threshold_scale.grid(row=0, column=1, padx=5, pady=2)
        ttk.Label(param_frame, text='Макс. разрыв (мм):').grid(row=0, column=2, sticky=tk.W, padx=5, pady=2)
        self.gap_var = tk.DoubleVar(value=self.max_allowed_gap_mm)
        ttk.Entry(param_frame, textvariable=self.gap_var, width=8).grid(row=0, column=3, padx=5, pady=2)
        ttk.Label(param_frame, text='Допуск позиции (мм):').grid(row=1, column=0, sticky=tk.W, padx=5, pady=2)
        self.pos_var = tk.DoubleVar(value=self.pos_tolerance_mm)
        ttk.Entry(param_frame, textvariable=self.pos_var, width=8).grid(row=1, column=1, padx=5, pady=2)
        ttk.Label(param_frame, text='Шаг срезов (px):').grid(row=1, column=2, sticky=tk.W, padx=5, pady=2)
        self.step_var = tk.IntVar(value=self.step_px)
        ttk.Entry(param_frame, textvariable=self.step_var, width=8).grid(row=1, column=3, padx=5, pady=2)
        ttk.Button(param_frame, text='Проверить разметку', command=self._preview_check).grid(
            row=0, column=4, rowspan=2, padx=10, pady=2)
        self.metrics_var = tk.StringVar(value='Метрики: —')
        ttk.Label(param_frame, textvariable=self.metrics_var).grid(row=2, column=0, columnspan=4,
                                                                    sticky=tk.W, padx=5, pady=2)

        passport_frame = ttk.LabelFrame(self, text='Паспорт эталона')
        passport_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)
        ttk.Label(passport_frame, text='Статус:').grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.label_var = tk.StringVar(value='ok')
        ttk.Radiobutton(passport_frame, text='OK (Эталон)', variable=self.label_var, value='ok').grid(
            row=0, column=1, sticky=tk.W, padx=5, pady=2)
        ttk.Radiobutton(passport_frame, text='NG (Образец брака)', variable=self.label_var, value='ng').grid(
            row=0, column=2, sticky=tk.W, padx=5, pady=2)
        ttk.Label(passport_frame, text='Тип дефекта:').grid(row=1, column=0, sticky=tk.W, padx=5, pady=2)
        self.defect_var = tk.StringVar(value='none')
        ttk.Combobox(passport_frame, textvariable=self.defect_var,
                     values=['none', 'gap', 'width', 'pos'], width=15, state='readonly').grid(
            row=1, column=1, sticky=tk.W, padx=5, pady=2)
        ttk.Label(passport_frame, text='Комментарий:').grid(row=2, column=0, sticky=tk.W, padx=5, pady=2)
        self.note_var = tk.StringVar(value=self.existing_sidecar.get('note', '') if self.existing_sidecar else '')
        ttk.Entry(passport_frame, textvariable=self.note_var, width=50).grid(
            row=2, column=1, columnspan=3, sticky=tk.W, padx=5, pady=2)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.TOP, fill=tk.X, padx=5, pady=10)
        ttk.Button(btn_frame, text='Атомарно сохранить эталон',
                   command=self._save_etalon).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text='Отмена', command=self.destroy).pack(side=tk.LEFT, padx=5)

        self.status_var = tk.StringVar(value='Точки оси: 0 | Маски: 0')
        ttk.Label(self, textvariable=self.status_var).pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=2)

    def _on_tool_change(self):
        self._tool = self.tool_var.get()
        self._rect_start = None
        self._rect_end = None
        self._redraw()

    def _clear_centerline(self):
        self.centerline_points = []
        self._redraw()

    def _clear_ignore(self):
        self.ignore_rects = []
        self._redraw()

    def _finalize_centerline(self):
        if len(self.centerline_points) >= 2:
            self.status_var.set('Ось завершена: %d точек' % len(self.centerline_points))
        else:
            messagebox.showwarning('Ось', 'Нужно минимум 2 точки для траектории шва')
        self._redraw()

    def _canvas_to_image(self, cx, cy):
        return (int(cx / self.scale), int(cy / self.scale))

    def _image_to_canvas(self, x, y):
        return (int(x * self.scale), int(y * self.scale))

    def _on_canvas_click(self, event):
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        ix, iy = self._canvas_to_image(cx, cy)
        if self._tool == 'centerline':
            if 0 <= ix < self.w and 0 <= iy < self.h:
                self.centerline_points.append((ix, iy))
                self._redraw()
        elif self._tool == 'ignore':
            self._drawing_rect = True
            self._rect_start = (ix, iy)
            self._rect_end = (ix, iy)

    def _on_canvas_drag(self, event):
        if self._tool != 'ignore' or not self._drawing_rect:
            return
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        ix, iy = self._canvas_to_image(cx, cy)
        self._rect_end = (max(0, min(ix, self.w)), max(0, min(iy, self.h)))
        self._redraw()

    def _on_canvas_release(self, event):
        if self._tool != 'ignore' or not self._drawing_rect:
            return
        self._drawing_rect = False
        if self._rect_start and self._rect_end:
            x1 = min(self._rect_start[0], self._rect_end[0])
            y1 = min(self._rect_start[1], self._rect_end[1])
            x2 = max(self._rect_start[0], self._rect_end[0])
            y2 = max(self._rect_start[1], self._rect_end[1])
            if x2 - x1 > 3 and y2 - y1 > 3:
                self.ignore_rects.append([x1, y1, x2 - x1, y2 - y1])
        self._rect_start = None
        self._rect_end = None
        self._redraw()

    def _on_canvas_right_click(self, event):
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        ix, iy = self._canvas_to_image(cx, cy)
        if not self.centerline_points:
            return
        best_idx = None
        best_dist = float('inf')
        for i, (px, py) in enumerate(self.centerline_points):
            d = ((px - ix) ** 2 + (py - iy) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_idx = i
        if best_dist < 15:
            self.centerline_points.pop(best_idx)
            self._redraw()

    def _redraw(self):
        if self.base_image is None:
            return
        img = self.base_image.copy()
        for (rx, ry, rw, rh) in self.ignore_rects:
            cv2.rectangle(img, (rx, ry), (rx + rw, ry + rh), (128, 128, 128), -1)
            cv2.putText(img, 'IGNORE', (rx + 3, ry + 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        for i, (px, py) in enumerate(self.centerline_points):
            cv2.circle(img, (px, py), 4, (0, 255, 255), -1)
            if i > 0:
                prev = self.centerline_points[i - 1]
                cv2.line(img, prev, (px, py), (0, 255, 255), 2)
            cv2.putText(img, str(i), (px + 6, py - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        if self._drawing_rect and self._rect_start and self._rect_end:
            x1 = min(self._rect_start[0], self._rect_end[0])
            y1 = min(self._rect_start[1], self._rect_end[1])
            x2 = max(self._rect_start[0], self._rect_end[0])
            y2 = max(self._rect_start[1], self._rect_end[1])
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        for sl in self._slices_preview:
            cx, cy = sl['center']
            nx, ny = sl['normal']
            half_len = max(5, int(self.step_px * 0.5))
            x1 = int(round(cx - half_len * nx))
            y1 = int(round(cy - half_len * ny))
            x2 = int(round(cx + half_len * nx))
            y2 = int(round(cy + half_len * ny))
            color = (0, 0, 255) if sl['is_gap'] else ((0, 255, 0) if sl['status'] == 'ok' else (0, 255, 255))
            cv2.line(img, (x1, y1), (x2, y2), color, 1)
            if sl['is_gap']:
                cv2.circle(img, (cx, cy), 3, (0, 0, 255), -1)
        if self.scale != 1.0:
            disp = cv2.resize(img, (self.canvas_w, self.canvas_h), interpolation=cv2.INTER_AREA)
        else:
            disp = img
        rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
        ppm_header = 'P6 %d %d 255\n' % (disp.shape[1], disp.shape[0])
        ppm_data = ppm_header.encode('ascii') + rgb.tobytes()
        photo = tk.PhotoImage(width=disp.shape[1], height=disp.shape[0], data=ppm_data, format='PPM')
        self.canvas.create_image(0, 0, anchor=tk.NW, image=photo)
        self.canvas.image = photo
        self.canvas.configure(scrollregion=(0, 0, self.canvas_w, self.canvas_h))
        self.status_var.set('Точки оси: %d | Маски: %d | Инструмент: %s' % (
            len(self.centerline_points), len(self.ignore_rects), self._tool))

    def _preview_check(self):
        if not self.centerline_points or len(self.centerline_points) < 2:
            messagebox.showwarning('Разметка', 'Нужно минимум 2 точки для оси')
            return
        try:
            step_px = int(self.step_var.get())
        except ValueError:
            step_px = 15
        try:
            max_gap = float(self.gap_var.get())
        except ValueError:
            max_gap = 0.0
        try:
            pos_tol = float(self.pos_var.get())
        except ValueError:
            pos_tol = 2.5
        seam_cfg = self.config_data.get('seam_control', {})
        proc_cfg = dict(seam_cfg)
        proc_cfg['step_px'] = step_px
        proc_cfg['max_allowed_gap_mm'] = max_gap
        proc_cfg['pos_tolerance_mm'] = pos_tol
        proc_cfg['morph_kernel_size'] = 3
        proc_cfg['use_otsu'] = True
        proc_cfg['min_contrast_std'] = self.config_data.get('training', {}).get('min_contrast_std', 8.0)
        proc_cfg['gap_min_width_mm'] = 0.2
        proc_cfg['min_fill_ratio'] = 0.85
        proc = SeamProcessor(proc_cfg)
        proc.set_roi((0, 0, self.w, self.h))
        proc.set_etalon_annotation(
            centerline_px=self.centerline_points,
            ignore_rects=self.ignore_rects,
            step_px=step_px,
            max_allowed_gap_mm=max_gap,
            pos_tolerance_mm=pos_tol,
        )
        ok, width, msg, detail = proc.process(self.base_image)
        self._slices_preview = detail.get('slices', [])
        self._redraw()
        lines = []
        lines.append('Ширина: средняя %.2f мм | мин %.2f мм | макс %.2f мм' % (
            detail.get('mean_width_mm', 0),
            detail.get('min_width_mm', 0),
            detail.get('max_width_mm', 0),
        ))
        lines.append('Ожидаемая длина: %.2f мм | Заполнено: %.0f%%' % (
            detail.get('expected_length_mm', 0),
            detail.get('fill_ratio', 0) * 100,
        ))
        lines.append('Макс. разрыв: %.2f мм' % detail.get('max_gap_mm', 0))
        lines.append('Срезов: %d (разрывов: %d)' % (
            len(detail.get('slices', [])),
            sum(1 for s in detail.get('slices', []) if s['is_gap']),
        ))
        if msg != 'OK':
            lines.append('Статус: %s' % msg)
        else:
            lines.append('Статус: OK')
        self.metrics_var.set(' | '.join(lines))

    def _save_etalon(self):
        metrics = {}
        if self._slices_preview:
            widths = [s['width_mm'] for s in self._slices_preview if not s['is_gap']]
            if widths:
                px_per_mm = self.config_data.get('seam_control', {}).get('px_per_mm', 50.0)
                metrics = {
                    'mean_width_mm': float(np.mean(widths)),
                    'min_width_mm': float(np.min(widths)),
                    'max_width_mm': float(np.max(widths)),
                    'expected_length_mm': len(self._slices_preview) * self.step_px / px_per_mm,
                    'slice_widths_mm': [round(w, 3) for w in widths],
                }
        annotation = {
            'centerline_px': [list(p) for p in self.centerline_points],
            'ignore_rects': [list(r) for r in self.ignore_rects],
            'step_px': int(self.step_var.get()),
            'max_allowed_gap_mm': float(self.gap_var.get()),
            'pos_tolerance_mm': float(self.pos_var.get()),
            'metrics': metrics,
        }
        try:
            result = self.etalon_store.save_etalon(
                product_id=self.product_id,
                image_bgr=self.base_image,
                label=self.label_var.get(),
                config=self.config_data,
                annotation=annotation,
                defect_type=self.defect_var.get(),
                note=self.note_var.get(),
            )
        except Exception as e:
            messagebox.showerror('Ошибка сохранения', str(e))
            return
        messagebox.showinfo('Сохранено', 'Эталон сохранён:\n%s' % result['base_name'])
        if self.on_saved:
            self.on_saved(result)
        self.destroy()