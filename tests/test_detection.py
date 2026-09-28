from io import BytesIO

from PIL import Image

from app.detection import MotionDetector


def _jpeg(color, size=(160, 90)):
    img = Image.new("RGB", size, color=color)
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_first_frame_never_reports_motion():
    detector = MotionDetector()
    result = detector.process_frame(_jpeg((10, 10, 10)))
    assert result.motion_detected is False
    assert result.score == 0.0


def test_identical_frames_report_no_motion():
    detector = MotionDetector()
    frame = _jpeg((50, 50, 50))
    detector.process_frame(frame)
    result = detector.process_frame(frame)
    assert result.motion_detected is False


def test_large_change_reports_motion():
    detector = MotionDetector(pixel_threshold=10, area_threshold=0.05)
    detector.process_frame(_jpeg((0, 0, 0)))
    result = detector.process_frame(_jpeg((255, 255, 255)))
    assert result.motion_detected is True
    assert result.score > 0.5


def test_reset_clears_previous_frame():
    detector = MotionDetector()
    detector.process_frame(_jpeg((0, 0, 0)))
    detector.reset()
    result = detector.process_frame(_jpeg((255, 255, 255)))
    # After reset, this is treated as the "first" frame again.
    assert result.motion_detected is False
