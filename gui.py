import base64
import json
import logging
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import cv2

import main as app_main
from camera import HikrobotCamera
from communication import create_communicator
from image_processor import SeamProcessor
from roi_selector import ROIManager
from etalon_store import EtalonStore
from etalon_annotator import EtalonAnnotator

logger = logging.getLogger("gui")


class QueueLogHandler(logging.Handler):
    """Доставка записей журнала в очередь GUI."""

    def __init__(self, ui_queue):
        super().__init__()
        self.ui_queue = ui_queue

    def emit(self, record):
        try:
            self.ui_queue.put(('log', record.levelno, self.format(record)))
        except Exception:
            pass


class GuiReporter:
    """Потокобезопасная доставка статусов и результатов из рабочего потока."""

    def __init__(self, ui_queue):
        self.ui_queue = ui_queue

    def status(self, text):
        self.ui_queue.put(('status', str(text)))

    def result(self, ok, width, msg, image=None, raw=None, bbox=None):
        self.ui_queue.put(('result', bool(ok), float(width), str(msg),
                           time.strftime('%H:%M:%S')))
        if raw is not None:
            self.ui_queue.put(('raw', raw))
        if image is not None:
            self.ui_queue.put(('frame', image))


class NullDisplay:
    def show(self, image):
        pass

    def close(self):
        pass


def bgr_to_photo(image, max_w, max_h):
    h, w = image.shape[:2]
    scale = min(max_w / float(w), max_h / float(h))
    nw = max(1, int(w * scale))
    nh = max(1, int(h * scale))
    resized = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
    # PPM uncompressed format для снижения нагрузки на CPU (нет сжатия PNG)
    ppm_header = 'P6 %d %d 255\n' % (nw, nh)
    ppm_data = ppm_header.encode('ascii') + rgb.tobytes()
    return tk.PhotoImage(data=ppm_data, format='PPM'), scale


def save_config_atomic(config_path, config):
    data = {k: v for k, v in config.items() if k != '_config_path'}
    directory = os.path.dirname(os.path.abspath(config_path)) or '.'
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)
        os.replace(tmp_path, config_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


class SettingsWindow(tk.Toplevel):
    def __init__(self, parent, config, on_save):
        super().__init__(parent)
        self.title('Настройки')
        self.resizable(False, False)
        self.config = config
        self.on_save = on_save
        self.grab_set()

        seam = config['seam_control']
        comm_cfg = config['communication']['modbus']
        camera_cfg = config['camera']

        self.mode_var = tk.StringVar(value=config['communication']['mode'])
        self.cam_ip_var = tk.StringVar(value=str(camera_cfg.get('ip', '')))
        self.cam_unit_var = tk.StringVar(value=str(camera_cfg.get('modbus', {}).get('unit', 1)))
        self.plc_ip_var = tk.StringVar(value=str(comm_cfg.get('ip', '')))
        self.plc_unit_var = tk.StringVar(value=str(comm_cfg.get('unit', 1)))
        self.px_var = tk.StringVar(value=str(seam.get('px_per_mm', '')))
        self.min_var = tk.StringVar(value=str(seam.get('min_width_mm', '')))
        self.max_var = tk.StringVar(value=str(seam.get('max_width_mm', '')))
        self.cycle_var = tk.StringVar(value=str(config.get('limits', {}).get('cycle_time_s', '')))

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        rows = [
            ('Режим связи', ttk.Combobox(frame, textvariable=self.mode_var, state='readonly',
                                         values=('modbus', 'openshowvar', 'profinet', 'simulation'))),
            ('IP камеры', ttk.Entry(frame, textvariable=self.cam_ip_var, width=22)),
            ('Unit камеры (Modbus)', ttk.Entry(frame, textvariable=self.cam_unit_var, width=22)),
            ('IP ПЛК', ttk.Entry(frame, textvariable=self.plc_ip_var, width=22)),
            ('Unit ПЛК (Modbus)', ttk.Entry(frame, textvariable=self.plc_unit_var, width=22)),
            ('Калибровка px_per_mm', ttk.Entry(frame, textvariable=self.px_var, width=22)),
            ('Мин. ширина шва, мм', ttk.Entry(frame, textvariable=self.min_var, width=22)),
            ('Макс. ширина шва, мм', ttk.Entry(frame, textvariable=self.max_var, width=22)),
            ('Лимит цикла, с', ttk.Entry(frame, textvariable=self.cycle_var, width=22)),
        ]
        for i, (label, widget) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky='w', pady=3, padx=(0, 12))
            widget.grid(row=i, column=1, sticky='ew', pady=3)
        frame.columnconfigure(1, weight=1)

        buttons = ttk.Frame(frame)
        buttons.grid(row=len(rows), column=0, columnspan=2, pady=(14, 0), sticky='e')
        ttk.Button(buttons, text='Сохранить', command=self._save).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text='Отмена', command=self.destroy).pack(side=tk.LEFT)

    def _save(self):
        try:
            mode = self.mode_var.get()
            if mode not in ('modbus', 'openshowvar', 'profinet', 'simulation'):
                raise ValueError('неизвестный режим связи')
            cam_ip = self.cam_ip_var.get().strip()
            plc_ip = self.plc_ip_var.get().strip()
            if not cam_ip or not plc_ip:
                raise ValueError('IP не может быть пустым')
            cam_unit = int(self.cam_unit_var.get())
            plc_unit = int(self.plc_unit_var.get())
            px = float(self.px_var.get())
            min_w = float(self.min_var.get())
            max_w = float(self.max_var.get())
            cycle = float(self.cycle_var.get())
            if px <= 0 or min_w <= 0 or max_w <= 0 or cycle <= 0:
                raise ValueError('значения должны быть больше нуля')
            if min_w >= max_w:
                raise ValueError('мин. ширина должна быть меньше макс.')
        except ValueError as e:
            messagebox.showerror('Настройки', f'Некорректное значение: {e}', parent=self)
            return

        config = self.config
        config['communication']['mode'] = mode
        config['camera']['ip'] = cam_ip
        config['camera'].setdefault('modbus', {})['unit'] = cam_unit
        config['communication']['modbus']['ip'] = plc_ip
        config['communication']['modbus']['unit'] = plc_unit
        config['seam_control']['px_per_mm'] = px
        config['seam_control']['min_width_mm'] = min_w
        config['seam_control']['max_width_mm'] = max_w
        config['limits']['cycle_time_s'] = cycle

        try:
            save_config_atomic(config['_config_path'], config)
        except Exception as e:
            messagebox.showerror('Настройки', f'Не удалось сохранить config.json:\n{e}',
                                 parent=self)
            return
        self.on_save(config)
        self.destroy()


class ROIEditor(tk.Toplevel):
    def __init__(self, parent, image, roi, on_save):
        super().__init__(parent)
        self.title('Выбор зоны ROI')
        self.geometry('980x720')
        self.image = image
        self.on_save = on_save
        self.scale = 1.0
        self.photo = None
        self.start = None
        self.end = None
        self.rect_id = None
        self.text_id = None

        top = ttk.Frame(self, padding=8)
        top.pack(fill=tk.X)
        ttk.Label(top, text='Обведите зону шва мышью').pack(side=tk.LEFT)
        ttk.Button(top, text='Сохранить', command=self._save).pack(side=tk.RIGHT)
        ttk.Button(top, text='Отмена', command=self.destroy).pack(side=tk.RIGHT, padx=6)

        self.canvas = tk.Canvas(self, bg='#1b1b1b', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.canvas.bind('<ButtonPress-1>', self._on_press)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)

        self._show_image(roi)

    def _show_image(self, roi):
        self.update_idletasks()
        cw = max(self.canvas.winfo_width() - 2, 200)
        ch = max(self.canvas.winfo_height() - 2, 200)
        photo, scale = bgr_to_photo(self.image, cw, ch)
        if photo is None:
            return
        self.photo = photo
        self.scale = scale
        self.canvas.delete('all')
        self.canvas.create_image(0, 0, image=photo, anchor='nw')
        if roi:
            x, y, w, h = roi
            self.start = (x * scale, y * scale)
            self.end = ((x + w) * scale, (y + h) * scale)
            self._redraw()

    def _on_press(self, event):
        self.start = (event.x, event.y)
        self.end = (event.x, event.y)
        self._redraw()

    def _on_drag(self, event):
        self.end = (event.x, event.y)
        self._redraw()

    def _on_release(self, event):
        self.end = (event.x, event.y)
        self._redraw()

    def _redraw(self, color='#00e5ff'):
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
        if self.text_id is not None:
            self.canvas.delete(self.text_id)
            self.text_id = None
        if not self.start or not self.end:
            return
        x1, x2 = sorted((self.start[0], self.end[0]))
        y1, y2 = sorted((self.start[1], self.end[1]))
        self.rect_id = self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=2)
        w = int(abs(x2 - x1) / self.scale)
        h = int(abs(y2 - y1) / self.scale)
        self.text_id = self.canvas.create_text(
            x1 + 4, y1 + 6, anchor='nw', fill=color,
            font=('Segoe UI', 11, 'bold'), text=f'{w} x {h} px')

    def _save(self):
        if not self.start or not self.end:
            messagebox.showwarning('ROI', 'Обведите прямоугольник мышью', parent=self)
            return
        ih, iw = self.image.shape[:2]
        x1 = int(min(self.start[0], self.end[0]) / self.scale)
        y1 = int(min(self.start[1], self.end[1]) / self.scale)
        x2 = int(max(self.start[0], self.end[0]) / self.scale)
        y2 = int(max(self.start[1], self.end[1]) / self.scale)
        x1 = max(0, min(x1, iw - 1))
        y1 = max(0, min(y1, ih - 1))
        x2 = max(0, min(x2, iw))
        y2 = max(0, min(y2, ih))
        w, h = x2 - x1, y2 - y1
        if w < 2 or h < 2:
            messagebox.showwarning('ROI', 'Зона слишком маленькая', parent=self)
            return
        self.on_save((x1, y1, w, h))
        self.destroy()


class InspectionGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.ready = False
        self.title('Инспекция шва — KUKA KR200')
        self.geometry('1280x820')
        self.minsize(1024, 680)

        self.ui_queue = queue.Queue()
        self.stop_event = None
        self.worker = None
        self.processor = None
        self.last_raw = None
        self.photo = None
        self.ok_count = 0
        self.ng_count = 0
        self.running = False
        self.training_mode = False
        self.training_block_send = False
        self.etalon_store = None
        self.training_product_id = None

        try:
            self.config_data = app_main.load_config()
        except Exception as e:
            messagebox.showerror('Конфигурация',
                                 f'Не удалось загрузить config.json:\n{e}')
            self.destroy()
            return
        app_main.setup_logging(self.config_data)
        self.queue_handler = QueueLogHandler(self.ui_queue)
        logging.getLogger().addHandler(self.queue_handler)
        logger.info('GUI started')

        # Инициализация хранилища эталонов
        training_cfg = self.config_data.get('training', {})
        etalon_folder = training_cfg.get('folder', 'data/etalon')
        if not os.path.isabs(etalon_folder):
            etalon_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), etalon_folder)
        self.etalon_store = EtalonStore(base_folder=etalon_folder, config=self.config_data)
        self.training_product_id = training_cfg.get('active_product_id', 'ProductID_01')

        self._build_ui()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self._poll()
        self.ready = True

    def _build_ui(self):
        self.status_var = tk.StringVar(value='Готово')

        toolbar = ttk.Frame(self, padding=(8, 6))
        toolbar.pack(side=tk.TOP, fill=tk.X)

        self.start_btn = ttk.Button(toolbar, text='▶ Старт', command=self.start_inspection)
        self.start_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(toolbar, text='⏹ Стоп', command=self.stop_inspection,
                                   state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(6, 18))
        ttk.Button(toolbar, text='ROI-зона', command=self.open_roi_editor).pack(side=tk.LEFT)
        ttk.Button(toolbar, text='Настройки', command=self.open_settings).pack(
            side=tk.LEFT, padx=6)

        self.mode_label = ttk.Label(toolbar, text=self._mode_text(), font=('Segoe UI', 10, 'bold'))
        self.mode_label.pack(side=tk.LEFT, padx=18)

        # === Панель режима обучения ===
        train_frame = ttk.LabelFrame(toolbar, text='Режим обучения', padding=(6, 4))
        train_frame.pack(side=tk.LEFT, padx=18)

        self.train_mode_var = tk.StringVar(value='production')
        ttk.Radiobutton(train_frame, text='Инспекция', variable=self.train_mode_var,
                        value='production', command=self._on_train_mode_change).pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(train_frame, text='Обучение: Захват', variable=self.train_mode_var,
                        value='capture', command=self._on_train_mode_change).pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(train_frame, text='Инспекция по эталону', variable=self.train_mode_var,
                        value='verify', command=self._on_train_mode_change).pack(side=tk.LEFT, padx=2)

        ttk.Label(train_frame, text='ProductID:').pack(side=tk.LEFT, padx=(8, 2))
        self.product_combo = ttk.Combobox(train_frame, width=18, state='readonly')
        self.product_combo.pack(side=tk.LEFT, padx=2)
        self.product_combo['values'] = self._list_product_ids()
        self.product_combo.set(self.training_product_id)
        ttk.Button(train_frame, text='+', width=2, command=self._create_product_id).pack(side=tk.LEFT, padx=2)

        ttk.Button(train_frame, text='💾 Сохранить в эталон и разметить',
                   command=self._save_to_etalon).pack(side=tk.LEFT, padx=(8, 2))

        self.state_label = tk.Label(toolbar, text=' СТОП ', bg='#9e9e9e', fg='white',
                                    font=('Segoe UI', 10, 'bold'))
        self.state_label.pack(side=tk.RIGHT)

        status_bar = ttk.Frame(self, padding=(8, 2))
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(status_bar, textvariable=self.status_var).pack(anchor='w')

        log_frame = ttk.Frame(self, padding=(8, 0, 8, 4))
        log_frame.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(log_frame, text='Журнал событий').pack(anchor='w')
        log_body = ttk.Frame(log_frame)
        log_body.pack(fill=tk.X)
        self.log_text = tk.Text(log_body, height=9, state=tk.DISABLED,
                                font=('Consolas', 9), bg='#1e1e1e', fg='#d4d4d4',
                                wrap=tk.WORD)
        log_scroll = ttk.Scrollbar(log_body, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.tag_configure('error', foreground='#ff6b6b')
        self.log_text.tag_configure('warning', foreground='#ffd166')

        body = ttk.Frame(self, padding=8)
        body.pack(fill=tk.BOTH, expand=True)

        video_frame = ttk.LabelFrame(body, text='Камера', padding=4)
        video_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.video_label = tk.Label(video_frame, bg='#101010', text='Нет кадра',
                                    fg='#888888', font=('Segoe UI', 12))
        self.video_label.pack(fill=tk.BOTH, expand=True)

        info = ttk.LabelFrame(body, text='Результат', padding=12)
        info.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))

        self.result_label = tk.Label(info, text='—', font=('Segoe UI', 40, 'bold'),
                                     bg='#9e9e9e', fg='white', width=7, height=2)
        self.result_label.pack(fill=tk.X)
        self.width_label = ttk.Label(info, text='Ширина: — мм', font=('Segoe UI', 14))
        self.width_label.pack(anchor='w', pady=(14, 0))
        self.msg_label = ttk.Label(info, text='', font=('Segoe UI', 10), foreground='#555555')
        self.msg_label.pack(anchor='w', pady=(2, 0))
        self.time_label = ttk.Label(info, text='Время: —', font=('Segoe UI', 11))
        self.time_label.pack(anchor='w', pady=(6, 0))
        self.stats_label = ttk.Label(info, text='OK: 0    NOK: 0    Всего: 0',
                                     font=('Segoe UI', 11, 'bold'))
        self.stats_label.pack(anchor='w', pady=(18, 0))
        self.roi_label = ttk.Label(info, text=self._roi_text(), font=('Segoe UI', 10),
                                   foreground='#555555')
        self.roi_label.pack(anchor='w', pady=(18, 0))

    def _mode_text(self):
        return f"Режим: {self.config_data['communication']['mode']}"

    def _roi_text(self):
        roi = self.config_data.get('roi') or {}
        if roi.get('enabled') and roi.get('w', 0) > 0 and roi.get('h', 0) > 0:
            return f"ROI: {roi['x']},{roi['y']}  {roi['w']}x{roi['h']}"
        return 'ROI: не задан'

    def _append_log(self, text, tag=None):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + '\n', tag or ())
        self.log_text.see(tk.END)
        lines = int(self.log_text.index('end-1c').split('.')[0])
        if lines > 1000:
            self.log_text.delete('1.0', f'{lines - 1000}.0')
        self.log_text.config(state=tk.DISABLED)

    def _set_running(self, running, state_text=None):
        self.running = running
        self.start_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)
        if running:
            self.state_label.config(text=' РАБОТА ', bg='#2e7d32')
        else:
            self.state_label.config(text=' СТОП ', bg='#9e9e9e')
        if state_text:
            self.status_var.set(state_text)

    def _reset_stats(self):
        self.ok_count = 0
        self.ng_count = 0
        self.result_label.config(text='—', bg='#9e9e9e')
        self.width_label.config(text='Ширина: — мм')
        self.msg_label.config(text='')
        self.time_label.config(text='Время: —')
        self.stats_label.config(text='OK: 0    NOK: 0    Всего: 0')

    def start_inspection(self):
        if self.running:
            return
        try:
            config = app_main.load_config()
        except Exception as e:
            messagebox.showerror('Запуск', f'Не удалось загрузить config.json:\n{e}')
            return
        self.config_data = config
        self.mode_label.config(text=self._mode_text())
        self.roi_label.config(text=self._roi_text())
        self._reset_stats()
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self._worker,
                                       args=(config, self.stop_event), daemon=True)
        self._set_running(True, 'Запуск...')
        self._append_log(f'Запуск инспекции, режим {config["communication"]["mode"]}')
        self.worker.start()

    def stop_inspection(self):
        if self.stop_event and not self.stop_event.is_set():
            self.stop_event.set()
            self.stop_btn.config(state=tk.DISABLED)
            self.status_var.set('Остановка...')
            self._append_log('Запрос остановки...')

    def _worker(self, config, stop_event):
        reporter = GuiReporter(self.ui_queue)
        camera = None
        comm = None
        comm_connected = False
        try:
            camera = HikrobotCamera(
                {**config['camera'],
                 'image_folder': app_main.resolve_path(
                     config['camera'].get('image_folder', ''))})
            processor = SeamProcessor(config['seam_control'])
            self.processor = processor
            roi_manager = ROIManager(config['_config_path'])
            roi_manager.load_from_config(config)
            processor.set_roi(roi_manager.roi)
            comm = create_communicator(config)
            # В режиме обучения «Захват» блокируем отправку боевых сигналов в ПЛК
            if self.training_block_send:
                orig_send_result = comm.send_result
                def _blocked_send_result(status):
                    logger.info('[TRAINING] send_result заблокирован (режим обучения)')
                    return True
                comm.send_result = _blocked_send_result
            saver = app_main.ResultSaver(config)
            budget = float(config.get('limits', {}).get('cycle_time_s', 0.8))
            mode = config['communication']['mode']

            reporter.status('Подключение камеры...')
            if not camera.connect():
                logger.critical('Camera connection failed')
                reporter.status('ОШИБКА: не удалось подключиться к камере')
                return

            reporter.status(f'Подключение к ПЛК/роботу ({mode})...')
            comm_connected = comm.connect()
            if not comm_connected:
                logger.critical(f'Robot/PLC connection failed (mode={mode})')
                reporter.status(f'ОШИБКА: нет связи с ПЛК/роботом ({mode})')
                return

            reporter.status(f'Работа в режиме {mode}')
            display = NullDisplay()
            if mode == 'profinet':
                app_main.run_profinet_mode(camera, processor, comm, display, saver, budget,
                                           stop_event=stop_event, reporter=reporter)
            elif mode == 'simulation':
                app_main.run_simulation_mode(camera, processor, comm, display, saver, budget,
                                             stop_event=stop_event, reporter=reporter)
            else:
                app_main.run_modbus_mode(camera, processor, comm, display, saver, budget,
                                         stop_event=stop_event, reporter=reporter)
            reporter.status('Инспекция остановлена')
        except Exception:
            logger.exception('Fatal error in inspection loop')
            reporter.status('АВАРИЯ — подробности в журнале')
        finally:
            if comm_connected:
                try:
                    app_main.outputs_reset_checked(comm)
                except Exception:
                    logger.exception('Outputs reset on shutdown failed')
            for obj in (comm, camera):
                if obj is None:
                    continue
                try:
                    obj.disconnect()
                except Exception:
                    logger.exception(f'Cleanup {obj.__class__.__name__} failed')
            self.processor = None

    def _handle_event(self, kind, payload):
        if kind == 'status':
            text = payload[0]
            self._append_log(f'{time.strftime("%H:%M:%S")} {text}')
            self.status_var.set(text)
        elif kind == 'log':
            levelno, text = payload
            tag = None
            if levelno >= logging.ERROR:
                tag = 'error'
            elif levelno >= logging.WARNING:
                tag = 'warning'
            self._append_log(text, tag)
        elif kind == 'result':
            ok, width, msg, stamp = payload
            if ok:
                self.ok_count += 1
                result_text, bg = 'OK', '#2e7d32'
            else:
                self.ng_count += 1
                result_text, bg = 'NOK', '#c62828'
            total = self.ok_count + self.ng_count
            self.result_label.config(text=result_text, bg=bg)
            self.width_label.config(text=f'Ширина: {width:.2f} мм')
            self.msg_label.config(text=msg)
            self.time_label.config(text=f'Время: {stamp}')
            self.stats_label.config(
                text=f'OK: {self.ok_count}    NOK: {self.ng_count}    Всего: {total}')
        elif kind == 'raw':
            self.last_raw = payload[0]
        elif kind == 'frame':
            image = payload[0]
            max_w = max(200, self.video_label.winfo_width())
            max_h = max(200, self.video_label.winfo_height())
            photo, _ = bgr_to_photo(image, max_w, max_h)
            if photo is not None:
                self.photo = photo
                self.video_label.config(image=photo, text='')

    def _poll(self):
        if not self.winfo_exists():
            return
        for _ in range(200):
            try:
                kind, *payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle_event(kind, payload)
            except Exception:
                logger.exception('GUI event handling failed')
        if self.running and self.worker is not None and not self.worker.is_alive():
            self.worker = None
            self._set_running(False, 'Остановлено')
        self.after(100, self._poll)

    def open_settings(self):
        SettingsWindow(self, self.config_data, self._on_settings_saved)

    def _on_settings_saved(self, config):
        self.config_data = config
        self.mode_label.config(text=self._mode_text())
        if self.processor is not None:
            seam = config['seam_control']
            self.processor.min_width = seam['min_width_mm']
            self.processor.max_width = seam['max_width_mm']
            self.processor.px_per_mm = seam['px_per_mm']
        self._append_log('Настройки сохранены')
        self.status_var.set('Настройки сохранены')

    def open_roi_editor(self):
        image = self.last_raw
        if image is None:
            image = self._load_latest_frame()
        if image is None:
            messagebox.showwarning(
                'ROI', 'Нет кадра: запустите инспекцию или нажмите «Старт», '
                       'чтобы получить изображение с камеры')
            return
        roi = None
        cfg_roi = self.config_data.get('roi') or {}
        if cfg_roi.get('enabled') and cfg_roi.get('w', 0) > 0 and cfg_roi.get('h', 0) > 0:
            roi = (cfg_roi['x'], cfg_roi['y'], cfg_roi['w'], cfg_roi['h'])
        ROIEditor(self, image, roi, self._save_roi)

    def _load_latest_frame(self):
        try:
            cam_cfg = {**self.config_data['camera'],
                       'image_folder': app_main.resolve_path(
                           self.config_data['camera'].get('image_folder', ''))}
            return HikrobotCamera(cam_cfg).capture()
        except Exception:
            logger.exception('Failed to load latest frame from folder')
            return None

    def _save_roi(self, roi):
        try:
            ROIManager(self.config_data['_config_path']).save_to_config(roi)
        except Exception as e:
            messagebox.showerror('ROI', f'Не удалось сохранить ROI:\n{e}')
            return
        self.config_data['roi'] = {'enabled': True, 'x': roi[0], 'y': roi[1],
                                   'w': roi[2], 'h': roi[3]}
        if self.processor is not None:
            self.processor.set_roi(roi)
        self.roi_label.config(text=self._roi_text())
        self._append_log(f'ROI сохранён: {roi[0]},{roi[1]} {roi[2]}x{roi[3]}')
        self.status_var.set('ROI сохранён')

    def _on_train_mode_change(self):
        mode = self.train_mode_var.get()
        self.training_mode = (mode != 'production')
        self.training_block_send = (mode == 'capture')
        pid = self.product_combo.get()
        if pid:
            self.training_product_id = pid
        if self.training_block_send:
            self._append_log('Режим обучения: ЗАХВАТ — отправка результатов в ПЛК заблокирована')
        elif self.training_mode:
            self._append_log('Режим: Инспекция по эталону')
        else:
            self._append_log('Режим: Инспекция (рабочий)')
        self._update_train_button_state()

    def _update_train_button_state(self):
        has_frame = self.last_raw is not None
        # Кнопка сохранения доступна только если есть кадр

    def _list_product_ids(self):
        if self.etalon_store is None:
            return [self.training_product_id]
        base = self.etalon_store.base_folder
        if not os.path.isdir(base):
            return [self.training_product_id]
        ids = [d for d in os.listdir(base)
               if os.path.isdir(os.path.join(base, d))]
        if not ids:
            ids = [self.training_product_id]
        return sorted(ids)

    def _create_product_id(self):
        new_id = tk.simpledialog.askstring('Новый ProductID', 'Введите идентификатор продукта:',
                                           parent=self)
        if not new_id:
            return
        new_id = new_id.strip()
        if not new_id:
            return
        self.etalon_store.ensure_product(new_id)
        self.training_product_id = new_id
        self.product_combo['values'] = self._list_product_ids()
        self.product_combo.set(new_id)
        self._append_log('Создан ProductID: %s' % new_id)

    def _save_to_etalon(self):
        if self.last_raw is None:
            messagebox.showwarning('Эталон', 'Нет кадра для сохранения. Запустите инспекцию.')
            return
        pid = self.product_combo.get() or self.training_product_id
        self.training_product_id = pid

        # Обрезаем кадр по ROI если задан
        image = self.last_raw.copy()
        roi_cfg = self.config_data.get('roi', {})
        if roi_cfg.get('enabled') and roi_cfg.get('w', 0) > 0 and roi_cfg.get('h', 0) > 0:
            x, y, w, h = roi_cfg['x'], roi_cfg['y'], roi_cfg['w'], roi_cfg['h']
            image = image[y:y+h, x:x+w]

        # Открываем окно разметки
        EtalonAnnotator(
            parent=self,
            image_bgr=image,
            config=self.config_data,
            etalon_store=self.etalon_store,
            product_id=pid,
            on_saved=self._on_etalon_saved,
        )

    def _on_etalon_saved(self, result):
        self._append_log('Эталон сохранён: %s' % result.get('base_name', '?'))
        self.product_combo['values'] = self._list_product_ids()
        self.status_var.set('Эталон сохранён: %s' % result.get('base_name', '?'))

    def _on_close(self):
        if self.running:
            self._append_log('Остановка перед выходом...')
            if self.stop_event:
                self.stop_event.set()
            if self.worker is not None:
                self.worker.join(timeout=6)
        logging.getLogger().removeHandler(self.queue_handler)
        self.destroy()


def run_gui():
    app = InspectionGUI()
    if not app.ready:
        return 1
    app.mainloop()
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(run_gui())
