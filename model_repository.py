"""Репозиторий моделей изделий и профилей контрольных точек."""

import json
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

MODEL_DRAFT = "DRAFT"
MODEL_READY = "READY"
MODEL_ACTIVE = "ACTIVE"
MODEL_ARCHIVED = "ARCHIVED"
ALL_MODEL_STATUSES = (MODEL_DRAFT, MODEL_READY, MODEL_ACTIVE, MODEL_ARCHIVED)

MODEL_FILE = "model.json"
PROFILE_FILE = "inspection.json"
REFERENCE_FILE = "reference.png"
ANNOTATION_FILE = "annotation.json"

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_MAX_NAME_LEN = 64


def _safe_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("model/point name must be a string")
    name = name.strip()
    if not name:
        raise ValueError("name must not be empty")
    if len(name) > _MAX_NAME_LEN:
        raise ValueError("name too long (max %d characters)" % _MAX_NAME_LEN)
    if not _NAME_RE.match(name):
        raise ValueError(
            "name may contain only letters, digits, underscore, hyphen and dot"
        )
    base = name.rstrip(".")
    if base in ("CON", "PRN", "AUX", "NUL"):
        raise ValueError("reserved name: %s" % name)
    if re.match(r"^(COM|LPT)[1-9]$", base, re.IGNORECASE):
        raise ValueError("reserved name: %s" % name)
    if ".." in name:
        raise ValueError("name must not contain '..'")
    return name


def _is_within(base_dir: str, target_path: str) -> bool:
    base_abs = os.path.abspath(base_dir)
    target_abs = os.path.abspath(target_path)
    return target_abs == base_abs or target_abs.startswith(base_abs + os.sep)


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.write(chr(10))
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


class ModelRepositoryError(Exception):
    pass


class ModelNotFoundError(ModelRepositoryError):
    pass


class ModelAlreadyExistsError(ModelRepositoryError):
    pass

class ModelRepository:
    def __init__(self, base_folder: str = "data/models"):
        self.base_folder = os.path.abspath(base_folder)
        os.makedirs(self.base_folder, exist_ok=True)

    def model_folder(self, model_id: str) -> str:
        _safe_name(model_id)
        return os.path.join(self.base_folder, model_id)

    def point_folder(self, model_id: str, point_id: str) -> str:
        _safe_name(model_id)
        _safe_name(point_id)
        return os.path.join(self.model_folder(model_id), "points", point_id)

    def model_path(self, model_id: str) -> str:
        return os.path.join(self.model_folder(model_id), MODEL_FILE)

    def profile_path(self, model_id: str, point_id: str) -> str:
        return os.path.join(self.point_folder(model_id, point_id), PROFILE_FILE)

    def reference_path(self, model_id: str, point_id: str) -> str:
        return os.path.join(self.point_folder(model_id, point_id), REFERENCE_FILE)

    def annotation_path(self, model_id: str, point_id: str) -> str:
        return os.path.join(self.point_folder(model_id, point_id), ANNOTATION_FILE)

    def list_models(self) -> List[str]:
        if not os.path.isdir(self.base_folder):
            return []
        models = []
        for entry in os.listdir(self.base_folder):
            full = os.path.join(self.base_folder, entry)
            if os.path.isdir(full) and os.path.exists(
                os.path.join(full, MODEL_FILE)
            ):
                models.append(entry)
        return sorted(models)

    def model_exists(self, model_id: str) -> bool:
        _safe_name(model_id)
        return os.path.exists(self.model_path(model_id))

    def get_model(self, model_id: str) -> Dict[str, Any]:
        _safe_name(model_id)
        path = self.model_path(model_id)
        if not os.path.exists(path):
            raise ModelNotFoundError("Model not found: %s" % model_id)
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def create_model(
        self,
        model_id: str,
        label: Optional[str] = None,
        description: Optional[str] = None,
        required_points: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        _safe_name(model_id)
        if self.model_exists(model_id):
            raise ModelAlreadyExistsError("Model already exists: %s" % model_id)

        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        model = {
            "model_id": model_id,
            "label": label or model_id,
            "description": description or "",
            "status": MODEL_DRAFT,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "required_points": required_points or [],
            "active_point_version": {},
        }
        folder = self.model_folder(model_id)
        os.makedirs(folder, exist_ok=True)
        os.makedirs(os.path.join(folder, "points"), exist_ok=True)
        _atomic_write_json(self.model_path(model_id), model)
        return model

    def update_model_meta(
        self,
        model_id: str,
        label: Optional[str] = None,
        description: Optional[str] = None,
        required_points: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        model = self.get_model(model_id)
        if model["status"] not in (MODEL_DRAFT, MODEL_ARCHIVED):
            raise ModelRepositoryError(
                "Cannot edit model in status %s" % model["status"]
            )
        if label is not None:
            model["label"] = label
        if description is not None:
            model["description"] = description
        if required_points is not None:
            model["required_points"] = list(required_points)
        model["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        model["version"] = int(model.get("version", 1)) + 1
        _atomic_write_json(self.model_path(model_id), model)
        return model

    def set_model_status(self, model_id: str, status: str) -> Dict[str, Any]:
        if status not in ALL_MODEL_STATUSES:
            raise ValueError("Invalid status: %s" % status)
        model = self.get_model(model_id)
        old_status = model["status"]

        allowed = {
            MODEL_DRAFT: (MODEL_READY, MODEL_ARCHIVED),
            MODEL_READY: (MODEL_ACTIVE, MODEL_ARCHIVED, MODEL_DRAFT),
            MODEL_ACTIVE: (MODEL_ARCHIVED, MODEL_READY),
            MODEL_ARCHIVED: (MODEL_DRAFT,),
        }
        if status not in allowed.get(old_status, ()):
            raise ModelRepositoryError(
                "Cannot transition %s -> %s" % (old_status, status)
            )

        if status == MODEL_READY:
            self._verify_model_ready(model_id, model)

        model["status"] = status
        model["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        model["version"] = int(model.get("version", 1)) + 1
        _atomic_write_json(self.model_path(model_id), model)
        return model

    def activate_model(self, model_id: str) -> Dict[str, Any]:
        return self.set_model_status(model_id, MODEL_ACTIVE)

    def archive_model(self, model_id: str) -> Dict[str, Any]:
        return self.set_model_status(model_id, MODEL_ARCHIVED)

    def delete_model(self, model_id: str) -> None:
        model = self.get_model(model_id)
        if model["status"] == MODEL_ACTIVE:
            raise ModelRepositoryError("Cannot delete ACTIVE model")
        import shutil

        folder = self.model_folder(model_id)
        if not _is_within(self.base_folder, folder):
            raise ModelRepositoryError("Unsafe model folder path")
        shutil.rmtree(folder, ignore_errors=True)

    def _verify_model_ready(self, model_id: str, model: Dict[str, Any]) -> None:
        required = model.get("required_points", [])
        missing = []
        for point_id in required:
            pfolder = self.point_folder(model_id, point_id)
            if not os.path.isdir(pfolder):
                missing.append(point_id)
                continue
            profile = self.load_profile(model_id, point_id)
            if not profile or not self._profile_is_complete(profile):
                missing.append(point_id)
        if missing:
            raise ModelRepositoryError(
                "Model %s not ready: missing/incomplete profiles for points: %s"
                % (model_id, ", ".join(missing))
            )

    def list_points(self, model_id: str) -> List[str]:
        _safe_name(model_id)
        pbase = os.path.join(self.model_folder(model_id), "points")
        if not os.path.isdir(pbase):
            return []
        points = []
        for entry in os.listdir(pbase):
            full = os.path.join(pbase, entry)
            if os.path.isdir(full):
                points.append(entry)
        return sorted(points)

    def point_exists(self, model_id: str, point_id: str) -> bool:
        return os.path.exists(self.profile_path(model_id, point_id))

    def load_profile(self, model_id: str, point_id: str) -> Optional[Dict[str, Any]]:
        _safe_name(model_id)
        _safe_name(point_id)
        path = self.profile_path(model_id, point_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise ModelRepositoryError(
                "Corrupt profile for model=%s point=%s: %s" % (model_id, point_id, e)
            )

    def save_profile(
        self,
        model_id: str,
        point_id: str,
        profile: Dict[str, Any],
        new_version: bool = True,
    ) -> Dict[str, Any]:
        _safe_name(model_id)
        _safe_name(point_id)
        pfolder = self.point_folder(model_id, point_id)
        os.makedirs(pfolder, exist_ok=True)

        old_profile = self.load_profile(model_id, point_id)
        if new_version and old_profile is not None:
            ver_folder = os.path.join(pfolder, "__versions__")
            os.makedirs(ver_folder, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            ver_name = "profile_v%d_%s.json" % (
                int(old_profile.get("version", 0)) + 1,
                ts,
            )
            _atomic_write_json(os.path.join(ver_folder, ver_name), old_profile)

        profile["model_id"] = model_id
        profile["point_id"] = point_id
        profile["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        profile["version"] = (
            int(old_profile.get("version", 0)) + 1 if old_profile else 1
        )
        _atomic_write_json(self.profile_path(model_id, point_id), profile)
        return profile

    @staticmethod
    def _profile_is_complete(profile: Dict[str, Any]) -> bool:
        required = ("min_width_mm", "max_width_mm", "px_per_mm")
        return all(k in profile and profile[k] is not None for k in required)

    def verify_profile(self, model_id: str, point_id: str) -> Tuple[bool, List[str]]:
        errors = []
        profile = self.load_profile(model_id, point_id)
        if profile is None:
            return False, ["Profile not found"]
        if not self._profile_is_complete(profile):
            errors.append("Missing required calibration/width parameters")
        ref = self.reference_path(model_id, point_id)
        if not os.path.exists(ref):
            errors.append("Reference image missing")
        ann = self.annotation_path(model_id, point_id)
        if not os.path.exists(ann):
            errors.append("Annotation missing")
        return (len(errors) == 0), errors

    def save_reference_image(self, model_id: str, point_id: str, image_bytes: bytes) -> str:
        _safe_name(model_id)
        _safe_name(point_id)
        pfolder = self.point_folder(model_id, point_id)
        os.makedirs(pfolder, exist_ok=True)
        path = self.reference_path(model_id, point_id)
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(image_bytes)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return path

    def save_annotation(
        self, model_id: str, point_id: str, annotation: Dict[str, Any]
    ) -> Dict[str, Any]:
        _safe_name(model_id)
        _safe_name(point_id)
        pfolder = self.point_folder(model_id, point_id)
        os.makedirs(pfolder, exist_ok=True)
        annotation["model_id"] = model_id
        annotation["point_id"] = point_id
        annotation["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        annotation["version"] = 1
        _atomic_write_json(self.annotation_path(model_id, point_id), annotation)
        return annotation

    def load_annotation(
        self, model_id: str, point_id: str
    ) -> Optional[Dict[str, Any]]:
        path = self.annotation_path(model_id, point_id)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def check_integrity(self, model_id: str) -> Tuple[bool, List[str]]:
        errors = []
        try:
            model = self.get_model(model_id)
        except ModelRepositoryError as e:
            return False, [str(e)]

        required_points = model.get("required_points", [])
        for point_id in required_points:
            ok, errs = self.verify_profile(model_id, point_id)
            if not ok:
                for e in errs:
                    errors.append("%s/%s: %s" % (model_id, point_id, e))

        return len(errors) == 0, errors

    def get_status(self, model_id: str) -> str:
        model = self.get_model(model_id)
        return model.get("status", MODEL_DRAFT)