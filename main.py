import json
import logging
import logging.handlers
import time
import sys
import os
import cv2
from datetime import datetime
from communication import create_communicator
from camera import HikrobotCamera
from image_processor import SeamProcessor
from roi_selector import ROISelector, ROIManager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger("main")


def resolve_path(path):
    """Абсолютный путь: относительные пути трактуются относительно main.py."""
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def load_config(path='config.json'):
    config_path = path if os.path.isabs(path) else os.path.join(BASE_DIR, path)
    with open(config_path, encoding='utf-8') as f:
        config = json.load(f)
    config['_config_path'] = config_path
    validate_config(config)
    return config


def validate_config(config):
    if config.get('communication', {}).get('mode') not in (
            'modbus', 'openshowvar', 'profinet', 'simulation'):
        raise ValueError("communication.mode must be modbus/openshowvar/profinet/simulation")
    seam = config.get('seam_control', {})
    if float(seam.get('px_per_mm', 0) or 0) <= 0:
        raise ValueError("seam_control.px_per_mm must be > 0 (run calibration)")
    if float(seam.get('min_width_mm', 0)) <= 0 or float(seam.get('max_width_mm', 0)) <= 0:
        raise ValueError("seam_control width limits must be > 0")


def setup_logging(config):
    log_cfg = config.get('logging', {})
    level = getattr(logging, str(log_cfg.get('level', 'INFO')).upper(), logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s')

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    log_file = resolve_path(log_cfg.get('file', 'app.log'))
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(fmt)
    root.addHandler(console)


def cleanup_folder(folder, max_files=0, max_age_days=0):
    """Очистка папки: старше max_age_days и старше лимита max_files."""
    if not folder or not os.path.isdir(folder):
        return 0
    try:
        entries = [(e.stat().st_mtime, e.path) for e in os.scandir(folder) if e.is_file()]
    except OSError as e:
        logger.warning(f"Cleanup scan failed for {folder}: {e}")
        return 0

    removed = 0
    if max_age_days and max_age_days > 0:
        cutoff = time.time() - max_age_days * 86400
        keep = []
        for mtime, path in entries:
            if mtime < cutoff:
                try:
                    os.remove(path)
                    removed += 1
                except OSError:
                    keep.append((mtime, path))
            else:
                keep.append((mtime, path))
        entries = keep

    if max_files and max_files > 0 and len(entries) > max_files:
        entries.sort(key=lambda e: e[0])
        for mtime, path in entries[:len(entries) - max_files]:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass

    if removed:
        logger.info(f"Cleanup {folder}: removed {removed} files")
    return removed


def send_result_checked(comm, ok, **kwargs):
    """Отправка результата с проверкой и одной повторной попыткой."""
    for attempt in (1, 2):
        try:
            result = comm.send_result(ok, **kwargs) if kwargs else comm.send_result(ok)
        except Exception as e:
            logger.error(f"send_result exception (attempt {attempt}): {e}")
            result = False
        if result is not False:
            return True
        logger.error(f"send_result failed (attempt {attempt})")
        time.sleep(0.1)
    return False


def outputs_reset_checked(comm):
    for attempt in (1, 2):
        try:
            result = comm.reset_outputs()
        except Exception as e:
            logger.error(f"reset_outputs exception (attempt {attempt}): {e}")
            result = False
        if result is not False:
            return True
        logger.error(f"reset_outputs failed (attempt {attempt})")
        time.sleep(0.1)
    return False


def report_cycle_time(prefix, started, budget):
    elapsed = time.time() - started
    if budget and elapsed > budget:
        logger.warning(f"{prefix}: {elapsed:.3f}s exceeds budget {budget:.3f}s")
    else:
        logger.info(f"{prefix}: {elapsed:.3f}s")
    return elapsed


class ResultSaver:
    def __init__(self, config):
        self.folder = resolve_path(config.get('results_folder', ''))
        self.enabled = bool(self.folder)
        if self.enabled:
            os.makedirs(self.folder, exist_ok=True)

    def save(self, image, ok, width_mm, msg):
        if not self.enabled or image is None:
            return
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        status = "OK" if ok else "NOK"
        filename = f"{status}_{timestamp}_{width_mm:.2f}mm.png"
        path = os.path.join(self.folder, filename)
        if not cv2.imwrite(path, image):
            logger.error(f"Failed to save result image: {path}")


class Display:
    def __init__(self, config):
        self.enabled = config.get('display', {}).get('enabled', True)
        self.window_name = "Seam Inspection"
        # waitKey(ms) блокирует главный цикл: по умолчанию 1 мс (нереализующий вызов)
        self.waitKey = max(1, int(config.get('display', {}).get('wait_key_ms', 1)))

    def show(self, image):
        if not self.enabled or image is None:
            return
        cv2.imshow(self.window_name, image)
        cv2.waitKey(self.waitKey)

    def close(self):
        if self.enabled:
            try:
                cv2.destroyWindow(self.window_name)
            except cv2.error:
                pass


def grab_image(camera, timeout=2.3):
    """Ожидание нового кадра после триггера с ограниченным временем."""
    time.sleep(0.3)
    deadline = time.time() + timeout
    image = camera.capture()
    while image is None and time.time() < deadline:
        time.sleep(0.2)
        image = camera.capture()
    return image


def select_roi_mode(camera, processor, roi_manager):
    print("ROI selection mode - capture image and draw zone")

    camera.trigger_capture()
    image = grab_image(camera)
    if image is None:
        print("Failed to capture image from camera")
        return

    selector = ROISelector("Draw ROI - ENTER to confirm, ESC to cancel")
    roi = selector.select(image)

    if roi:
        roi_manager.save_to_config(roi)
        processor.set_roi(roi)
        print(f"ROI set: x={roi[0]}, y={roi[1]}, w={roi[2]}, h={roi[3]}")

        result_img = selector.draw_roi(image, roi, label="Selected ROI")
        cv2.imshow("ROI Result", result_img)
        cv2.waitKey(2000)
        cv2.destroyWindow("ROI Result")
    else:
        print("ROI selection cancelled")


def run_modbus_mode(camera, processor, comm, display, saver, budget):
    """Modbus/Owen MK210 mode: DI/DO based test stand logic."""
    outputs_reset_checked(comm)
    print("System ready - waiting for DI04 (start)")
    while True:
        print("Waiting for DI04 (start)...")
        if not comm.wait_for_di(4, timeout=30):
            continue
        cycle_start = time.time()
        print("DI04 received - starting conveyor")

        if comm.set_conveyor(True) is False:
            logger.error("Failed to start conveyor (DO0)")

        all_ok = True
        capture_count = 0

        while True:
            print("Waiting for DI06 (capture) or DI05 (end)...")
            di = comm.wait_for_di_any([5, 6], timeout=30)

            if di is None:
                logger.error("DI05/DI06 timeout")
                all_ok = False
                break

            if di == 5:
                print("DI05 received - stopping conveyor")
                break

            capture_count += 1
            capture_start = time.time()
            print(f"DI06 received - capture #{capture_count}")

            if not camera.trigger_capture():
                logger.error("Camera trigger failed")
                all_ok = False
                print("NOK: Camera trigger failed")
                report_cycle_time(f"Capture #{capture_count} (trigger failed)", capture_start, budget)
                continue

            image = grab_image(camera)
            if image is None:
                logger.error("No new image in folder")
                all_ok = False
                print("NOK: No new image")
                report_cycle_time(f"Capture #{capture_count} (no image)", capture_start, budget)
                continue

            ok, width, msg, bbox = processor.process(image)
            logger.info(f"Capture #{capture_count}: {width:.2f}mm - {msg}")
            print(f"  {'OK' if ok else 'NOK'}: {width:.2f}mm - {msg}")

            if not ok:
                all_ok = False

            result_img = processor.draw_result(image, bbox, ok, width)
            display.show(result_img)
            saver.save(result_img, ok, width, msg)
            report_cycle_time(f"Capture #{capture_count} processing", capture_start, budget)

        if comm.set_conveyor(False) is False:
            logger.error("Failed to stop conveyor (DO0)")
        if not send_result_checked(comm, all_ok):
            logger.critical(f"Result not delivered to PLC: {'OK' if all_ok else 'NG'}")
        result_text = "OK" if all_ok else "NG"
        logger.info(f"Cycle complete: {capture_count} captures - {result_text}")
        print(f"Cycle complete: {capture_count} captures - {result_text}")

        report_cycle_time(f"Cycle total ({capture_count} captures)", cycle_start, budget)
        outputs_reset_checked(comm)
        time.sleep(0.5)


def run_profinet_mode(camera, processor, comm, display, saver, budget):
    """PROFINET/cifX mode: ISRA BEADMASTER2D protocol with KRC4."""
    print("PROFINET/ISRA protocol mode - waiting for KRC4 commands")
    while True:
        print("Waiting for KRC4 trigger (O_START + O_ACQ)...")
        if not comm.wait_for_trigger(timeout=30):
            continue
        cycle_start = time.time()
        print("Trigger received - KRC4 started measurement")

        if not camera.trigger_capture():
            logger.error("Camera trigger failed")
            send_result_checked(comm, False, error_code=1)
            print("NOK: Camera trigger failed")
            continue

        image = grab_image(camera)
        if image is None:
            logger.error("No new image in folder")
            send_result_checked(comm, False, error_code=2)
            print("NOK: No new image")
            continue

        ok, width, msg, bbox = processor.process(image)
        logger.info(f"Measurement: {width:.2f}mm - {msg}")
        print(f"  {'OK' if ok else 'NOK'}: {width:.2f}mm - {msg}")

        if not send_result_checked(comm, ok, width_mm=width):
            logger.critical(f"Result not delivered to KRC4: {'OK' if ok else 'NG'}")

        result_img = processor.draw_result(image, bbox, ok, width)
        display.show(result_img)
        saver.save(result_img, ok, width, msg)
        report_cycle_time("KRC4 measurement", cycle_start, budget)

        print("Waiting for KRC4 reset (O_RESET)...")
        comm.wait_for_reset(timeout=30)
        print("Reset received - ready for next cycle")


def run_simulation_mode(camera, processor, comm, display, saver, budget):
    print("Simulation mode - press Ctrl+C to stop")
    while True:
        loop_start = time.time()
        camera.trigger_capture()
        time.sleep(0.5)
        image = camera.capture()
        if image is not None:
            ok, width, msg, bbox = processor.process(image)
            logger.info(f"Width: {width:.2f}mm - {msg}")
            print(f"{'OK' if ok else 'NOK'}: {width:.2f}mm - {msg}")
            result_img = processor.draw_result(image, bbox, ok, width)
            display.show(result_img)
            saver.save(result_img, ok, width, msg)
        report_cycle_time("Simulation loop", loop_start, budget)
        time.sleep(1)


def run_storage_cleanup(config):
    storage = config.get('storage', {})
    max_files = int(storage.get('max_files', 0))
    max_age_days = int(storage.get('max_age_days', 0))
    if not max_files and not max_age_days:
        return
    folders = [
        resolve_path(config.get('camera', {}).get('image_folder', '')),
        resolve_path(config.get('results_folder', '')),
    ]
    for folder in folders:
        cleanup_folder(folder, max_files=max_files, max_age_days=max_age_days)


def main():
    try:
        config = load_config()
    except Exception as e:
        print(f"FATAL: cannot load config.json: {e}")
        return 1

    setup_logging(config)
    logger.info(f"Starting, config: {config['_config_path']}")

    try:
        camera = HikrobotCamera({**config['camera'],
                                 'image_folder': resolve_path(config['camera'].get('image_folder', ''))})
        processor = SeamProcessor(config['seam_control'])
        comm = create_communicator(config)
        display = Display(config)
        saver = ResultSaver(config)
        roi_manager = ROIManager(config['_config_path'])
        roi_manager.load_from_config(config)
        processor.set_roi(roi_manager.roi)

        run_storage_cleanup(config)

        budget = float(config.get('limits', {}).get('cycle_time_s', 0.8))
        mode = config['communication']['mode']
        comm_connected = False

        exit_code = 0
        try:
            if "--roi" in sys.argv:
                if not camera.connect():
                    logger.critical("Camera connection failed")
                    print("Camera connection failed")
                    return 1
                select_roi_mode(camera, processor, roi_manager)
                return 0

            if not camera.connect():
                logger.critical("Camera connection failed")
                print("Camera connection failed")
                return 1

            print(f"Communication mode: {mode}")

            comm_connected = comm.connect()
            if not comm_connected:
                # Режим simulation поднимается фабрикой и connect() всегда True,
                # поэтому отказ связи - это авария, а не повод для тихого
                # перехода в simulation (робот/ПЛК остался бы без результата).
                logger.critical(f"Robot/PLC connection failed (mode={mode}) - aborting")
                print(f"FATAL: robot/PLC connection failed (mode={mode})")
                return 1

            if mode == 'profinet':
                run_profinet_mode(camera, processor, comm, display, saver, budget)
            elif mode == 'simulation':
                run_simulation_mode(camera, processor, comm, display, saver, budget)
            else:
                run_modbus_mode(camera, processor, comm, display, saver, budget)
        except KeyboardInterrupt:
            print("Stopped by user")
            logger.info("Stopped by user")
        except Exception:
            logger.exception("Fatal error in main loop")
            print("Fatal error - see log")
            exit_code = 1
        finally:
            # Гарантированный сброс выходов: конвейер/лампы не должны
            # остаться включёнными после падения или Ctrl+C.
            if comm_connected:
                try:
                    outputs_reset_checked(comm)
                except Exception:
                    logger.exception("Outputs reset on shutdown failed")
            for closer in (display.close, comm.disconnect, camera.disconnect):
                try:
                    closer()
                except Exception:
                    logger.exception(f"Cleanup {closer.__name__} failed")
        return exit_code
    except Exception:
        logging.getLogger("main").exception("Fatal startup error")
        return 1


if __name__ == '__main__':
    sys.exit(main())
