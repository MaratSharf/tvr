"""Режим обучения TVRUS: создание моделей, захват эталонов, разметка, активация."""

import os
import time
from typing import Any, Dict, List, Optional, Tuple

from model_repository import (
    ModelRepository,
    ModelNotFoundError,
    ModelAlreadyExistsError,
    MODEL_DRAFT,
    MODEL_READY,
    MODEL_ACTIVE,
    MODEL_ARCHIVED,
)


class TrainingManagerError(Exception):
    pass


class TrainingManager:
    def __init__(self, repository: Optional[ModelRepository] = None):
        self.repo = repository or ModelRepository()

    def list_models(self) -> List[Dict[str, Any]]:
        result = []
        for model_id in self.repo.list_models():
            try:
                model = self.repo.get_model(model_id)
                result.append({
                    "model_id": model["model_id"],
                    "label": model.get("label", model_id),
                    "description": model.get("description", ""),
                    "status": model.get("status", MODEL_DRAFT),
                    "version": model.get("version", 1),
                    "created_at": model.get("created_at", ""),
                    "updated_at": model.get("updated_at", ""),
                    "required_points": model.get("required_points", []),
                })
            except ModelNotFoundError:
                continue
        return result

    def create_model(
        self,
        model_id: str,
        label: Optional[str] = None,
        description: Optional[str] = None,
        required_points: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        try:
            return self.repo.create_model(
                model_id=model_id,
                label=label,
                description=description,
                required_points=required_points,
            )
        except ModelAlreadyExistsError as e:
            raise TrainingManagerError(str(e))

    def get_model(self, model_id: str) -> Dict[str, Any]:
        try:
            model = self.repo.get_model(model_id)
        except ModelNotFoundError as e:
            raise TrainingManagerError(str(e))

        points = []
        for point_id in self.repo.list_points(model_id):
            ok, errs = self.repo.verify_profile(model_id, point_id)
            points.append({
                "point_id": point_id,
                "profile_exists": self.repo.point_exists(model_id, point_id),
                "reference_exists": os.path.exists(
                    self.repo.reference_path(model_id, point_id)
                ),
                "annotation_exists": os.path.exists(
                    self.repo.annotation_path(model_id, point_id)
                ),
                "profile_complete": ok,
                "profile_errors": errs,
            })

        return {
            "model_id": model["model_id"],
            "label": model.get("label", model_id),
            "description": model.get("description", ""),
            "status": model.get("status", MODEL_DRAFT),
            "version": model.get("version", 1),
            "created_at": model.get("created_at", ""),
            "updated_at": model.get("updated_at", ""),
            "required_points": model.get("required_points", []),
            "points": points,
        }

    def update_model(
        self,
        model_id: str,
        label: Optional[str] = None,
        description: Optional[str] = None,
        required_points: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        try:
            return self.repo.update_model_meta(
                model_id=model_id,
                label=label,
                description=description,
                required_points=required_points,
            )
        except ModelNotFoundError as e:
            raise TrainingManagerError(str(e))
        except ModelRepositoryError as e:
            raise TrainingManagerError(str(e))

    def delete_model(self, model_id: str) -> None:
        try:
            self.repo.delete_model(model_id)
        except (ModelNotFoundError, ModelRepositoryError) as e:
            raise TrainingManagerError(str(e))

    def add_point(
        self,
        model_id: str,
        point_id: str,
        min_width_mm: float,
        max_width_mm: float,
        px_per_mm: float,
        threshold: int = 120,
        use_otsu: bool = True,
        step_px: int = 15,
        max_allowed_gap_mm: float = 0.5,
        pos_tolerance_mm: float = 2.5,
        min_fill_ratio: float = 0.85,
    ) -> Dict[str, Any]:
        model = self.repo.get_model(model_id)
        if model["status"] not in (MODEL_DRAFT, MODEL_ARCHIVED):
            raise TrainingManagerError(
                "Cannot edit point for model in status %s" % model["status"]
            )

        profile = {
            "min_width_mm": float(min_width_mm),
            "max_width_mm": float(max_width_mm),
            "px_per_mm": float(px_per_mm),
            "threshold": int(threshold),
            "use_otsu": bool(use_otsu),
            "step_px": int(step_px),
            "max_allowed_gap_mm": float(max_allowed_gap_mm),
            "pos_tolerance_mm": float(pos_tolerance_mm),
            "min_fill_ratio": float(min_fill_ratio),
            "gap_min_width_mm": 0.2,
            "morph_kernel_size": 3,
            "min_contrast_std": 8.0,
        }
        try:
            saved = self.repo.save_profile(model_id, point_id, profile, new_version=True)
        except ModelRepositoryError as e:
            raise TrainingManagerError(str(e))

        if model["status"] == MODEL_ARCHIVED:
            self.repo.set_model_status(model_id, MODEL_DRAFT)

        return saved

    def get_profile(self, model_id: str, point_id: str) -> Dict[str, Any]:
        try:
            profile = self.repo.load_profile(model_id, point_id)
        except ModelNotFoundError as e:
            raise TrainingManagerError(str(e))
        except ModelRepositoryError as e:
            raise TrainingManagerError(str(e))

        if profile is None:
            raise TrainingManagerError(
                "Profile not found for model=%s point=%s" % (model_id, point_id)
            )
        return profile

    def save_reference(self, model_id: str, point_id: str, image_bytes: bytes) -> str:
        try:
            return self.repo.save_reference_image(model_id, point_id, image_bytes)
        except ModelRepositoryError as e:
            raise TrainingManagerError(str(e))

    def save_annotation(
        self, model_id: str, point_id: str, annotation: Dict[str, Any]
    ) -> Dict[str, Any]:
        try:
            return self.repo.save_annotation(model_id, point_id, annotation)
        except ModelRepositoryError as e:
            raise TrainingManagerError(str(e))

    def get_annotation(
        self, model_id: str, point_id: str
    ) -> Optional[Dict[str, Any]]:
        try:
            return self.repo.load_annotation(model_id, point_id)
        except ModelRepositoryError as e:
            raise TrainingManagerError(str(e))

    def check_readiness(self, model_id: str) -> Tuple[bool, List[str]]:
        try:
            return self.repo.check_integrity(model_id)
        except ModelNotFoundError as e:
            return False, [str(e)]
        except ModelRepositoryError as e:
            return False, [str(e)]

    def approve_model(self, model_id: str) -> Dict[str, Any]:
        ok, errors = self.check_readiness(model_id)
        if not ok:
            raise TrainingManagerError(
                "Model not ready: %s" % "; ".join(errors)
            )
        try:
            return self.repo.set_model_status(model_id, MODEL_READY)
        except ModelRepositoryError as e:
            raise TrainingManagerError(str(e))

    def activate_model(self, model_id: str) -> Dict[str, Any]:
        try:
            model = self.repo.get_model(model_id)
        except ModelNotFoundError as e:
            raise TrainingManagerError(str(e))

        if model["status"] != MODEL_READY:
            raise TrainingManagerError(
                "Model must be in READY status before activation, current: %s"
                % model["status"]
            )

        try:
            return self.repo.set_model_status(model_id, MODEL_ACTIVE)
        except ModelRepositoryError as e:
            raise TrainingManagerError(str(e))

    def archive_model(self, model_id: str) -> Dict[str, Any]:
        try:
            return self.repo.archive_model(model_id)
        except ModelRepositoryError as e:
            raise TrainingManagerError(str(e))