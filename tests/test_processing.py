import cv2
import numpy as np
import pytest

from app.processing import (
    Options,
    Report,
    cluster_patterns,
    descriptive_group_name,
    detect_orientation,
    enhance_image,
    estimate_skew,
    rotate_quadrant,
    rotate_without_cropping,
)


def text_image():
    image = np.full((1000, 800), 255, dtype=np.uint8)
    for row, y in enumerate(range(100, 920, 45)):
        cv2.putText(
            image, f"Linha {row:02d}: documento para teste 12345", (60, y),
            cv2.FONT_HERSHEY_SIMPLEX, 0.72, 35, 2, cv2.LINE_AA,
        )
    return image


@pytest.mark.parametrize("angle", [-12, -7, -2.4, 0, 3.5, 9, 13])
def test_estimate_skew_corrects_text(angle):
    skewed = rotate_without_cropping(text_image(), angle)
    correction = estimate_skew(skewed)
    assert correction == pytest.approx(-angle, abs=0.35)
    corrected = rotate_without_cropping(skewed, correction)
    assert abs(estimate_skew(corrected)) <= 0.35


def test_blank_page_is_not_rotated():
    assert estimate_skew(np.full((500, 400), 255, dtype=np.uint8)) == 0


def test_skew_outside_search_range_is_not_forced_to_boundary():
    assert estimate_skew(rotate_without_cropping(text_image(), 10), max_angle=3) == 0


def test_rotation_does_not_crop_content():
    image = np.full((200, 300, 3), 255, dtype=np.uint8)
    rotated = rotate_without_cropping(image, 10)
    assert rotated.shape[0] > image.shape[0]
    assert rotated.shape[1] > image.shape[1]


@pytest.mark.parametrize("mode", ["grayscale", "color", "blackwhite"])
def test_enhancement_modes(mode):
    image = cv2.cvtColor(text_image(), cv2.COLOR_GRAY2RGB)
    result = enhance_image(image, Options(color_mode=mode))
    assert result.dtype == np.uint8
    assert result.shape[:2] == image.shape[:2]
    assert result.ndim == (3 if mode == "color" else 2)
    if mode == "blackwhite":
        assert set(np.unique(result)).issubset({0, 255})


def test_orientation_unavailable_is_explicit(monkeypatch):
    monkeypatch.setattr("app.processing.tesseract_command", lambda: None)
    report = Report()
    assert detect_orientation(text_image(), report) == 0
    assert "orientation_unavailable" in report.warnings


def test_confident_orientation(monkeypatch):
    monkeypatch.setattr("app.processing.tesseract_command", lambda: "tesseract")
    monkeypatch.setattr(
        "app.processing.pytesseract.image_to_osd",
        lambda *args, **kwargs: {"rotate": 90, "orientation_conf": 15},
    )
    assert detect_orientation(text_image(), Report()) == 90
    assert rotate_quadrant(text_image(), 90).shape == (800, 1000)


def test_moderately_confident_orientation_is_applied(monkeypatch):
    monkeypatch.setattr("app.processing.tesseract_command", lambda: "tesseract")
    monkeypatch.setattr(
        "app.processing.pytesseract.image_to_osd",
        lambda *args, **kwargs: {"rotate": 180, "orientation_conf": 5},
    )
    assert detect_orientation(text_image(), Report()) == 180


def test_uncertain_orientation_is_not_applied(monkeypatch):
    monkeypatch.setattr("app.processing.tesseract_command", lambda: "tesseract")
    monkeypatch.setattr(
        "app.processing.pytesseract.image_to_osd",
        lambda *args, **kwargs: {"rotate": 180, "orientation_conf": 3},
    )
    report = Report()
    assert detect_orientation(text_image(), report) == 0
    assert "orientation_low_confidence" in report.warnings


def test_pattern_clustering_requires_a_consistent_layout_for_every_page():
    first = np.array([1, 0, 0], dtype=np.float32)
    second = np.array([0.99, 0.01, 0], dtype=np.float32)
    third = np.array([0, 1, 0], dtype=np.float32)
    first /= np.linalg.norm(first)
    second /= np.linalg.norm(second)
    assert cluster_patterns([(first, {"safra"}), (second, {"safra"}), (third, set())]) == [
        [0, 1], [2]
    ]


def test_descriptive_group_name_uses_recurring_specific_words():
    feature = np.array([1], dtype=np.float32)
    features = [(feature, {"borderos", "safra", "documento"})] * 3
    assert descriptive_group_name(features, [0, 1, 2], 1) == "grupo-borderos-safra"


def test_descriptive_group_name_falls_back_without_reliable_text():
    feature = np.array([1], dtype=np.float32)
    features = [(feature, {"documento"})] * 2
    assert descriptive_group_name(features, [0, 1], 4) == "grupo-004"
