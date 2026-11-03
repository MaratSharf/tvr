import os
import tkinter as tk
from tkinter import messagebox, ttk

import cv2

from etalon_annotator import EtalonAnnotator


def _bgr_to_photo(image, max_w, max_h):
    h, w = image.shape[:2]
    scale = min(max_w / float(w), max_h / float(h))
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    ppm_header = 'P6 %d %d 255\n' % (nw, nh)
    ppm_data = ppm_header.encode('ascii') + rgb.tobytes()
    return tk.PhotoImage(data=ppm_data, format='PPM'), scale


class EtalonViewer(tk.Toplevel):
    """
    Просмотр эталонных изображений: список слева, кадр с наложением
    разметки (ось, зоны игнорирования) справа, паспорт и метрики снизу.
    """

    def __init__(self, parent, etalon_store, config, product_id=None,
                 on_changed=None):
        super().__init__(parent)
        self.title('Просмотр эталонов')
        self.geometry('1180x760')
        self.minsize(900, 600)
        self.etalon_store = etalon_store
        self.config_data = config
        self.on_changed = on_changed
        self.entries = []
        self.current_index = -1
        self.image = None
        self.sidecar = None
        self.photo = None
        self.show_overlay = True
        self.scale = 1.0
        self._resize_job = None

        self._build_ui()
        self._reload_products(product_id)
        self.bind('<Left>', lambda e: self._step(-1))
        self.bind('<Right>', lambda e: self._step(1))
        self.protocol('WM_DELETE_WINDOW', self.destroy)

    # ------------------------------------------------------------------ #
    # Построение интерфейса
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        top = ttk.Frame(self, padding=(8, 6))
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text='ProductID:').pack(side=tk.LEFT)
        self.product_combo = ttk.Combobox(top, width=22, state='readonly')
        self.product_combo.pack(side=tk.LEFT, padx=(4, 12))
        self.product_combo.bind('<<ComboboxSelected>>', self._on_product_change)

        ttk.Button(top, text='← Пред.', command=lambda: self._step(-1)).pack(side=tk.LEFT)
        ttk.Button(top, text='След. →', command=lambda: self._step(1)).pack(side=tk.LEFT, padx=(4, 12))

        self.overlay_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text='Показывать разметку', variable=self.overlay_var,
                        command=self._redraw).pack(side=tk.LEFT, padx=(0, 12))

        ttk.Button(top, text='Разметить заново', command=self._reannotate).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text='Удалить', command=self._delete).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(top, text='Обновить', command=self._reload_list).pack(side=tk.LEFT)
        ttk.Button(top, text='Закрыть', command=self.destroy).pack(side=tk.RIGHT)

        body = ttk.Frame(self, padding=(8, 0, 8, 4))
        body.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(body)
        left.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Label(left, text='Список эталонов').pack(anchor='w')
        list_frame = ttk.Frame(left)
        list_frame.pack(fill=tk.Y, expand=True)
        self.listbox = tk.Listbox(list_frame, width=34, exportselection=False,
                                  font=('Consolas', 9))
        list_scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                    command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=list_scroll.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.Y, expand=True)
        list_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind('<<ListboxSelect>>', self._on_select)

        right = ttk.Frame(body)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0))

        self.canvas = tk.Canvas(right, bg='#1b1b1b', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind('<Configure>', self._on_resize)

        info = ttk.LabelFrame(self, text='Паспорт эталона', padding=6)
        info.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(0, 8))
        self.info_var = tk.StringVar(value='Эталон не выбран')
        ttk.Label(info, textvariable=self.info_var, justify=tk.LEFT,
                  font=('Segoe UI', 10)).pack(anchor='w')
        self.metrics_var = tk.StringVar(value='')
        ttk.Label(info, textvariable=self.metrics_var, justify=tk.LEFT,
                  font=('Segoe UI', 10), foreground='#555555').pack(anchor='w', pady=(2, 0))

        self.status_var = tk.StringVar(value='')
        ttk.Label(self, textvariable=self.status_var, padding=(8, 0, 8, 4)).pack(
            side=tk.BOTTOM, fill=tk.X)

    # ------------------------------------------------------------------ #
    # Список продуктов и эталонов
    # ------------------------------------------------------------------ #
    def _list_product_ids(self):
        base = self.etalon_store.base_folder
        if not os.path.isdir(base):
            return []
        return sorted(d for d in os.listdir(base)
                      if os.path.isdir(os.path.join(base, d)))

    def _reload_products(self, select_id=None):
        ids = self._list_product_ids()
        self.product_combo['values'] = ids
        if not ids:
            self.product_combo.set('')
            self._reload_list()
            return
        if select_id and select_id in ids:
            self.product_combo.set(select_id)
        elif self.product_combo.get() not in ids:
            self.product_combo.set(ids[0])
        self._reload_list()

    def _on_product_change(self, event=None):
        self._reload_list()

    def _reload_list(self, keep_selection=True):
        pid = self.product_combo.get()
        prev_file = None
        if keep_selection and 0 <= self.current_index < len(self.entries):
            prev_file = self.entries[self.current_index].get('file')

        self.listbox.delete(0, tk.END)
        self.entries = self.etalon_store.scan_etalons(pid) if pid else []
        for e in self.entries:
            label = e.get('label') or '?'
            width = e.get('mean_width_mm')
            w_txt = '%.2f' % width if width else '—'
            ts = (e.get('timestamp') or e.get('file') or '')[3:18]
            self.listbox.insert(tk.END, '%s  %-4s %6s mm' % (ts, label.upper(), w_txt))

        index = 0
        if prev_file:
            for i, e in enumerate(self.entries):
                if e.get('file') == prev_file:
                    index = i
                    break
        if self.entries:
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(index)
            self.listbox.see(index)
            self._show_entry(index)
        else:
            self.current_index = -1
            self.image = None
            self.sidecar = None
            self._redraw()
            self.info_var.set('Нет эталонов для продукта %s' % (pid or '—'))
            self.metrics_var.set('')
        self.status_var.set('Эталонов: %d' % len(self.entries))

    def _on_select(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            return
        self._show_entry(sel[0])

    def _show_entry(self, index):
        if not (0 <= index < len(self.entries)):
            return
        pid = self.product_combo.get()
        entry = self.entries[index]
        image, sidecar = self.etalon_store.load_etalon(pid, entry['file'])
        self.current_index = index
        self.image = image
        self.sidecar = sidecar
        self._update_info()
        self._redraw()

    def _step(self, delta):
        if not self.entries:
            return
        index = self.current_index + delta
        if index < 0:
            index = len(self.entries) - 1
        elif index >= len(self.entries):
            index = 0
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(index)
        self.listbox.see(index)
        self._show_entry(index)

    # ------------------------------------------------------------------ #
    # Информация о выбранном эталоне
    # ------------------------------------------------------------------ #
    def _update_info(self):
        side = self.sidecar or {}
        ann = side.get('annotation') or {}
        calib = side.get('calibration') or {}
        metrics = side.get('metrics') or {}
        entry = self.entries[self.current_index] if 0 <= self.current_index < len(self.entries) else {}

        label = side.get('label') or entry.get('label') or '?'
        defect = side.get('defect_type') or entry.get('defect_type') or 'none'
        note = side.get('note') or entry.get('note') or ''
        ts = side.get('timestamp') or entry.get('timestamp') or '?'

        line1 = 'Файл: %s | Статус: %s | Дефект: %s | Время: %s' % (
            entry.get('file', '?'), label.upper(), defect, ts)
        if note:
            line1 += ' | %s' % note
        self.info_var.set(line1)

        parts = []
        if metrics:
            parts.append('Ширина: средняя %.2f мм (мин %.2f / макс %.2f)' % (
                metrics.get('mean_width_mm', 0),
                metrics.get('min_width_mm', 0),
                metrics.get('max_width_mm', 0)))
            parts.append('Длина: %.2f мм' % metrics.get('expected_length_mm', 0))
        parts.append('Осевых точек: %d | Зон игнор: %d' % (
            len(ann.get('centerline_px') or []),
            len(ann.get('ignore_rects') or [])))
        parts.append('Шаг: %s px | Разрыв ≤ %s мм | Позиция ≤ %s мм' % (
            ann.get('step_px', '—'), ann.get('max_allowed_gap_mm', '—'),
            ann.get('pos_tolerance_mm', '—')))
        if calib:
            parts.append('px_per_mm: %s | ROI: %s' % (
                calib.get('px_per_mm', '—'), calib.get('roi', '—')))
        self.metrics_var.set(' | '.join(parts))

    # ------------------------------------------------------------------ #
    # Отрисовка
    # ------------------------------------------------------------------ #
    def _compose(self):
        img = self.image.copy()
        if self.show_overlay and self.sidecar:
            ann = self.sidecar.get('annotation') or {}
            for r in ann.get('ignore_rects') or []:
                rx, ry, rw, rh = [int(v) for v in r]
                cv2.rectangle(img, (rx, ry), (rx + rw, ry + rh), (128, 128, 128), -1)
                cv2.putText(img, 'IGNORE', (rx + 3, ry + 18),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            pts = [tuple(p) for p in (ann.get('centerline_px') or [])]
            for i, (px, py) in enumerate(pts):
                if i > 0:
                    cv2.line(img, pts[i - 1], (px, py), (0, 255, 255), 2)
                cv2.circle(img, (px, py), 4, (0, 255, 255), -1)
        return img

    def _redraw(self):
        self.canvas.delete('all')
        if self.image is None:
            self.canvas.create_text(
                self.canvas.winfo_width() // 2 or 300,
                self.canvas.winfo_height() // 2 or 200,
                text='Нет изображения', fill='#888888',
                font=('Segoe UI', 12))
            return
        img = self._compose()
        cw = max(200, self.canvas.winfo_width() - 4)
        ch = max(200, self.canvas.winfo_height() - 4)
        photo, self.scale = _bgr_to_photo(img, cw, ch)
        self.photo = photo
        self.canvas.create_image(0, 0, image=photo, anchor='nw')

    def _on_resize(self, event):
        if event.widget is not self.canvas:
            return
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(120, self._redraw)

    # ------------------------------------------------------------------ #
    # Действия
    # ------------------------------------------------------------------ #
    def _reannotate(self):
        if self.image is None or not (0 <= self.current_index < len(self.entries)):
            messagebox.showwarning('Эталон', 'Сначала выберите эталон в списке',
                                   parent=self)
            return
        pid = self.product_combo.get()
        EtalonAnnotator(
            parent=self,
            image_bgr=self.image,
            config=self.config_data,
            etalon_store=self.etalon_store,
            product_id=pid,
            on_saved=self._on_etalon_changed,
            existing_sidecar=self.sidecar,
        )

    def _delete(self):
        if not (0 <= self.current_index < len(self.entries)):
            messagebox.showwarning('Эталон', 'Сначала выберите эталон в списке',
                                   parent=self)
            return
        pid = self.product_combo.get()
        entry = self.entries[self.current_index]
        if not messagebox.askyesno('Удаление эталона',
                                   'Удалить %s?\nДействие необратимо.' % entry['file'],
                                   parent=self):
            return
        try:
            self.etalon_store.delete_etalon(pid, entry['file'])
        except Exception as e:
            messagebox.showerror('Ошибка', 'Не удалось удалить:\n%s' % e, parent=self)
            return
        self.status_var.set('Удалён: %s' % entry['file'])
        self._reload_list(keep_selection=False)
        if self.on_changed:
            self.on_changed()

    def _on_etalon_changed(self, result):
        self._reload_list()
        if self.on_changed:
            self.on_changed()
