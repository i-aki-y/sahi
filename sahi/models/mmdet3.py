# OBSS SAHI Tool
# Code written by Fatih C Akyon, 2020.

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pycocotools
from sahi.models.base import DetectionModel
from sahi.prediction import ObjectPrediction
from sahi.utils.compatibility import fix_full_shape_list, fix_shift_amount_list
from sahi.utils.cv import get_bbox_from_bool_mask
from sahi.utils.import_utils import check_requirements

logger = logging.getLogger(__name__)


class Mmdet3DetectionModel(DetectionModel):

    def __init__(
        self,
        model_path: Optional[str] = None,
        model: Optional[Any] = None,
        config_path: Optional[str] = None,
        device: Optional[str] = None,
        mask_threshold: float = 0.5,
        confidence_threshold: float = 0.3,
        category_mapping: Optional[Dict] = None,
        category_remapping: Optional[Dict] = None,
        load_at_init: bool = True,
        image_size: int = None,
        scope: str = "mmdet",
    ):
        self.scope = scope
        super().__init__(
            model_path, model, config_path, device, mask_threshold, confidence_threshold, category_mapping, category_remapping,
            load_at_init, image_size
        )

    def check_dependencies(self):
        check_requirements(["torch", "mmdet", "mmcv"])

    def load_model(self):
        """
        Detection model is initialized and set to self.model.
        """
        from mmdet.apis.det_inferencer import DetInferencer

        # create model
        model = DetInferencer(self.config_path, self.model_path, device=self.device, scope=self.scope)

        # update model image size
        if self.image_size is not None:
            raise ValueError("Updating the image_size is not supported, modify the config file.")

        self.set_model(model)

    def set_model(self, model: Any):
        """
        Sets the underlying MMDetection model.
        Args:
            model: Any
                A MMDetection model
        """

        # set self.model
        self.model = model

        # set category_mapping
        if not self.category_mapping:
            category_mapping = {str(ind): category_name for ind, category_name in enumerate(self.category_names)}
            self.category_mapping = category_mapping

    def perform_inference(self, image: np.ndarray):
        """
        Prediction is performed using self.model and the prediction result is set to self._original_predictions.
        Args:
            image: np.ndarray
                A numpy array that contains the image to be predicted. 3 channel image should be in RGB order.
        """

        # Confirm model is loaded
        if self.model is None:
            raise ValueError("Model is not loaded, load it by calling .load_model()")

        # Supports only batch of 1

        # perform inference
        if isinstance(image, np.ndarray):
            # https://github.com/obss/sahi/issues/265
            image = image[:, :, ::-1]
        # compatibility with sahi v0.8.15
        if not isinstance(image, list):
            image = [image]
        prediction_result = self.model(image)

        self._original_predictions = prediction_result['predictions']

    @property
    def num_categories(self):
        """
        Returns number of categories
        """
        return len(self.category_names)

    @property
    def has_mask(self):
        """
        Returns if model output contains segmentation mask
        """
        has_mask = self.model.model.with_mask
        return has_mask

    @property
    def category_names(self):
        classes = self.model.model.dataset_meta['classes']
        if type(classes) == str:
            # https://github.com/open-mmlab/mmdetection/pull/4973
            return (classes,)
        else:
            return classes

    def _create_object_prediction_list_from_original_predictions(
        self,
        shift_amount_list: Optional[List[List[int]]] = [[0, 0]],
        full_shape_list: Optional[List[List[int]]] = None,
    ):
        """
        self._original_predictions is converted to a list of prediction.ObjectPrediction and set to
        self._object_prediction_list_per_image.
        Args:
            shift_amount_list: list of list
                To shift the box and mask predictions from sliced image to full sized image, should
                be in the form of List[[shift_x, shift_y],[shift_x, shift_y],...]
            full_shape_list: list of list
                Size of the full image after shifting, should be in the form of
                List[[height, width],[height, width],...]
        """
        original_predictions = self._original_predictions
        category_mapping = self.category_mapping

        # compatilibty for sahi v0.8.15
        shift_amount_list = fix_shift_amount_list(shift_amount_list)
        full_shape_list = fix_full_shape_list(full_shape_list)

        # parse boxes and masks from predictions
        object_prediction_list_per_image = []
        for image_ind, original_prediction in enumerate(original_predictions):
            shift_amount = shift_amount_list[image_ind]
            full_shape = None if full_shape_list is None else full_shape_list[image_ind]

            boxes = original_prediction['bboxes']
            scores = original_prediction['scores']
            labels = original_prediction['labels']
            if self.has_mask:
                masks = original_prediction['masks']

            object_prediction_list = []

            n_detects = len(labels)
            # process predictions
            for i in range(n_detects):
                if self.has_mask:
                    mask = masks[i]

                bbox = boxes[i]
                score = scores[i]
                category_id = labels[i]
                category_name = category_mapping[str(category_id)]

                # ignore low scored predictions
                if score < self.confidence_threshold:
                    continue

                # parse prediction mask
                if self.has_mask:
                    if "counts" in mask:
                        bool_mask = pycocotools.mask.decode(mask)
                    else:
                        bool_mask = mask

                    # check if mask is valid
                    # https://github.com/obss/sahi/discussions/696
                    if get_bbox_from_bool_mask(bool_mask) is None:
                        continue
                else:
                    bool_mask = None

                # fix negative box coords
                bbox[0] = max(0, bbox[0])
                bbox[1] = max(0, bbox[1])
                bbox[2] = max(0, bbox[2])
                bbox[3] = max(0, bbox[3])

                # fix out of image box coords
                if full_shape is not None:
                    bbox[0] = min(full_shape[1], bbox[0])
                    bbox[1] = min(full_shape[0], bbox[1])
                    bbox[2] = min(full_shape[1], bbox[2])
                    bbox[3] = min(full_shape[0], bbox[3])

                # ignore invalid predictions
                if not (bbox[0] < bbox[2]) or not (bbox[1] < bbox[3]):
                    logger.warning(f"ignoring invalid prediction with bbox: {bbox}")
                    continue

                object_prediction = ObjectPrediction(
                    bbox=bbox,
                    category_id=category_id,
                    score=score,
                    bool_mask=bool_mask,
                    category_name=category_name,
                    shift_amount=shift_amount,
                    full_shape=full_shape,
                )
                object_prediction_list.append(object_prediction)
            object_prediction_list_per_image.append(object_prediction_list)
        self._object_prediction_list_per_image = object_prediction_list_per_image
