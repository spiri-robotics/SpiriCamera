#!/usr/bin/env python3
"""Run YOLO on a mirrored camera's frames and draw the boxes as a live overlay.

This is a standalone, independent process: it never opens a video device
itself, only mirrors an existing camera's frames over SpiriSynq, runs
detection locally, and publishes the results as their own SpiriSynq object
plus a HudWidget that renders them as SVG rectangles on top of the camera's
picture. See docs/tutorials/yolo_overlay.md for the full walkthrough.

Requires the "examples" extra: uv sync --extra examples

Usage:
    python examples/yolo_overlay.py some-host/dev-video0
"""

import argparse
import time
from dataclasses import dataclass, field

import cv2
import numpy as np
from psygnal.containers import EventedList
from ultralytics import YOLO

from SpiriCamera import Camera
from SpiriCamera.overlay import HudWidget
from SpiriSynq.syncable_objects import SyncableObject

#: Alias "det" is declared once here and used by every {{ objects.det.* }}
#: expression below -- see HudWidget.svg_template for the mechanism.
#: __DET_TOPIC__ is filled in at runtime with the Detections object's own
#: absolute topic, once it's known (see main()).
#: The label sits at `box.y - 4` rather than `box.y` with a `dy="-4"`
#: attribute: thorvg's SVG parser silently ignores `dy` on `<text>`, so
#: any such offset has to be baked into `y` itself instead.
#: `colors` is a plain MiniJinja dict literal, not a second SpiriSynq
#: object -- there's nothing live about a class-to-color mapping, so it's
#: baked into the template itself rather than published anywhere. The
#: `default` filter covers any class YOLO's model knows about but this
#: map doesn't, rather than the strict-undefined render failing outright.
#: The label is drawn five times, offset a pixel in each diagonal
#: direction in black plus once more on top in `color`, rather than
#: given a `stroke` -- thorvg's SVG parser silently ignores `stroke`/
#: `stroke-width`/`paint-order` on `<text>` (same family of gap as the
#: `dy` one above), so a real outline has to be faked with layered fills
#: instead. This is what keeps a label legible over a bright or
#: similarly-colored patch of the picture it's drawn on, not just a
#: uniformly dark background.
BOX_OVERLAY_SVG = """
{# object: det = __DET_TOPIC__ #}
{% set colors = {"person": "lime", "bus": "orange", "car": "deepskyblue"} %}
{% set halo_offsets = [[-1,-1],[1,-1],[-1,1],[1,1]] %}
<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%"
     viewBox="0 0 {{ frame.width }} {{ frame.height }}">
  {% for box in objects.det.boxes %}
  {% set color = colors[box.label] | default("yellow") %}
  {% set label_x = box.x %}
  {% set label_y = box.y - 4 %}
  <rect x="{{ box.x }}" y="{{ box.y }}"
        width="{{ box.w }}" height="{{ box.h }}"
        fill="none" stroke="{{ color }}" stroke-width="3"/>
  {% for dx, dy in halo_offsets %}
  <text x="{{ label_x + dx }}" y="{{ label_y + dy }}" fill="black" font-size="16">
    {{ box.label }} {{ box.score }}
  </text>
  {% endfor %}
  <text x="{{ label_x }}" y="{{ label_y }}" fill="{{ color }}" font-size="16">
    {{ box.label }} {{ box.score }}
  </text>
  {% endfor %}
</svg>
"""


@dataclass
class Detections(SyncableObject):
    """A generic SpiriSynq object carrying the latest YOLO boxes.

    Nothing about this is camera-specific -- it's the same
    "any SyncableObject can be an overlay data source" mechanism the
    overlay system already documents (SpiriCamera.overlay module
    docstring), just with a field shaped for bounding boxes.
    """

    #: An ``EventedList``, not a plain ``list``, so SpiriSynq republishes
    #: the whole list on any mutation -- see ``overlay_widgets`` in
    #: SpiriCamera.overlay for the same pattern with a dict.
    boxes: EventedList = field(default_factory=EventedList)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic", help="camera topic to mirror, e.g. some-host/dev-video0")
    parser.add_argument("--model", default="yolov8n.pt", help="ultralytics model to load")
    parser.add_argument("--conf", type=float, default=0.4, help="detection confidence threshold")
    args = parser.parse_args()

    cam = Camera.from_topic(args.topic)
    model = YOLO(args.model)

    detections = Detections(synq_topic="yolo-detector/detections", synq_authoritive=True)

    widget = HudWidget(
        synq_topic="yolo-detector/boxes_overlay",
        synq_authoritive=True,
        anchor="top_left",
        # Sits under any other overlay (a HUD, the tiger demo widget)
        # sharing the frame, rather than potentially painting over one.
        z_index=-100,
        svg_template=BOX_OVERLAY_SVG.replace("__DET_TOPIC__", detections.synq_absolute_path),
    )
    # Attach the widget to the mirrored camera so it actually renders there.
    cam.overlay_widgets[widget.synq_absolute_path] = {}

    print(f"mirroring camera: {cam.synq_absolute_path}")
    print(f"publishing detections on: {detections.synq_absolute_path}")
    print(f"publishing overlay widget on: {widget.synq_absolute_path}")

    last_jpeg = None
    try:
        while True:
            jpeg = cam.image
            if not jpeg or jpeg is last_jpeg:
                time.sleep(0.02)
                continue
            last_jpeg = jpeg

            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue

            result = model.predict(frame, conf=args.conf, verbose=False)[0]
            boxes = []
            for box in result.boxes:
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                boxes.append(
                    {
                        "x": round(x1, 1),
                        "y": round(y1, 1),
                        "w": round(x2 - x1, 1),
                        "h": round(y2 - y1, 1),
                        "label": model.names[int(box.cls[0])],
                        "score": round(float(box.conf[0]), 2),
                    }
                )
            detections.boxes[:] = boxes
    except KeyboardInterrupt:
        pass
    finally:
        widget.close()
        detections.close()
        cam.close()


if __name__ == "__main__":
    main()
