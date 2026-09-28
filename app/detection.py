"""
Pishak Home — motion detection.

Deliberately simple: downscaled grayscale frame differencing, no OpenCV
dependency (heavier install, more RAM) and no continuous AI inference.
This is the "before heavy AI" detector the project spec calls for — cheap
enough to run every second or two on a Pi 3B+.

AI-based object/pet detection (Phase 8) is a separate, optional module
layered on top of this one later; it must not replace this lightweight
pass, only run after motion is already suspected.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import numpy as np
from PIL import Image

DOWNSCALE_SIZE = (160, 90)  # small on purpose: cheap diffing, not analysis


@dataclass
class MotionResult:
    motion_detected: bool
    score: float  # 0.0 - 1.0, fraction of changed pixels
    timestamp: float


def _decode_gray(jpeg_bytes: bytes) -> np.ndarray:
    with Image.open(BytesIO(jpeg_bytes)) as img:
        img = img.convert("L").resize(DOWNSCALE_SIZE)
        return np.asarray(img, dtype=np.int16)


class MotionDetector:
    def __init__(self, pixel_threshold: int = 25, area_threshold: float = 0.02):
        """
        pixel_threshold: minimum per-pixel grayscale delta to count as "changed".
        area_threshold: fraction of the frame that must change to call it motion
                         (0.02 = 2% of pixels), which filters out sensor noise.
        """
        self.pixel_threshold = pixel_threshold
        self.area_threshold = area_threshold
        self._previous: Optional[np.ndarray] = None

    def reset(self) -> None:
        self._previous = None

    def process_frame(self, jpeg_bytes: bytes) -> MotionResult:
        current = _decode_gray(jpeg_bytes)
        if self._previous is None:
            self._previous = current
            return MotionResult(motion_detected=False, score=0.0, timestamp=time.time())

        diff = np.abs(current - self._previous)
        changed = diff > self.pixel_threshold
        score = float(changed.mean())
        self._previous = current

        return MotionResult(
            motion_detected=score >= self.area_threshold,
            score=round(score, 4),
            timestamp=time.time(),
        )
