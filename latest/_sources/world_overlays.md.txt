(world-overlays)=
# Drawing on things in the world

Most HUD widgets sit at a fixed place on the frame: a battery readout in
a corner, a heading tape across the top. Some belong on *something in the
picture*: a marker on home, on the region of interest, on where the
rangefinder hit, on each detection. Those things are already in
SpiriSynq's frame tree as {py:class}`~SpiriSynq.example_types.position.Position`s
with poses in ECEF, so a widget can say which object it belongs on, and
the camera works out where that is in the picture.

The widget's SVG never does the geometry. It draws itself around its own
centre, and the camera places it. A widget opts in with one declaration in
its template, next to its `{# object: #}` declarations. A widget that
doesn't declare one works as it always has, and costs nothing extra.

| Declaration | Draws | Template gets |
| --- | --- | --- |
| `{# anchor: <alias> #}` | one copy, centred on the object | `view`: the object's place |
| `{# anchor: each <alias>.<field> #}` | one copy per object linked from `<field>` | `view` and `item`: each linked object's place and fields |
| `{# project: <alias>, ... #}` | one copy, on the frame, as usual | `view.<alias>`: each object's place, or `none` |

Every alias still needs its `{# object: <alias> = <topic> #}`
declaration, and a camera can rebind it with `overlay_widgets`'
`"bindings"`, as for any other widget. A widget has at most one `anchor`,
and either anchors or projects, not both.

## The camera needs a place

To know where an object is in its picture, the camera needs its own pose
and lens:

- **Its lens** is {py:attr}`~SpiriCamera.camera.CameraBase.intrinsics`,
  from settings, its source, or SpiriMav (see `set_intrinsics`).
- **Its pose:** a {py:class}`~SpiriCamera.camera.CameraBase` is a
  `Position`. Mount it on whatever carries it, usually a gimbal, by
  setting `relative_to`. `SPIRICAMERA_RELATIVE_TO=mu-1/robot_gimbal1` does
  that for `SpiriCamera run`, with an identity mount: no offset, the
  gimbal's axes. For anything else, set `mount` over SpiriSynq.
- **A relay** (a `spirisynq://` source, such as a HUD variant of another
  camera) shows another camera's view. It follows that camera's pose and
  its lens, live, unless it has been given a place of its own.

Until a camera has both a pose and a lens, it draws no anchored widgets,
and `view.<alias>` in a projecting widget is `none`.

A camera's own frame is FRD, like the rest of the tree: `x` out of the
lens, `y` to the right of the picture, `z` down it. A camera on a gimbal
with an identity mount looks along the gimbal's `x` axis, the way a
rangefinder on that gimbal points.

## `view`

{py:class}`~SpiriCamera.projection.View`'s fields:

| Field | |
| --- | --- |
| `x`, `y` | The object's pixel. |
| `r` | The radius, in pixels, of the circle holding the object's 90% accuracy cylinder ({py:class}`~SpiriSynq.example_types.position.Accuracy`), seen from here. `none` when the object's accuracy is unknown, or the cylinder reaches behind the camera. |
| `range` | Metres from the camera to the object. |
| `edge` | Whether the object, or its circle, crosses the frame's edge. |

The units are the ones the widget draws in:

- **For an anchored widget**, that means the widget's own pixels. These
  are the frame's pixels when overlays are burned in, and the canvas's
  when a browser renders them (`render_overlays_for_client`). So
  `<circle r="{{ view.r }}">` is the right size either way.
- **For a projecting widget**, they are the frame's pixels, the same as
  `frame.width` and `frame.height`. So the widget covers the frame with
  `width="100%" height="100%" viewBox="0 0 {{ frame.width }} {{ frame.height }}"`
  and draws at the pixels it's given.

## What gets drawn

- **An anchored copy is drawn** while any of its circle is on the frame.
  Off the frame, behind the camera, or with no pose (a rangefinder with
  no return), it isn't drawn. A copy partly off the frame is clipped.
- **A projecting widget is always drawn.** An object in front of the
  camera but out of shot still has its pixel, off the frame, and the
  SVG's own clipping handles anything drawn there. An object behind the
  camera, or with no pose, is `none`. A line to it has no honest 2D end,
  so skip it:

  ```jinja
  {% if view.a and view.b %}<line x1="{{ view.a.x }}" ... />{% endif %}
  ```

- **For `anchor: each`**, the field is a list of
  {py:class}`~SpiriSynq.link.Link`s, such as a robot's `gimbals`. Each
  linked object is mirrored as the list changes. A new link shows a frame
  after the list does, once its object has been mirrored.

## Examples

A marker on home, with its distance:

```jinja
{# object: home = mu-1/robot_home #}
{# anchor: home #}
<svg xmlns="http://www.w3.org/2000/svg" width="80" height="44">
  <path d="M40 10 L48 22 L40 34 L32 22 Z" fill="none" stroke="white" stroke-width="2"/>
  <text x="26" y="44" font-family="Miracode" font-size="13" fill="white">
    {{ view.range | round | int }} m
  </text>
</svg>
```

The widget is 80 px wide and 44 px tall, so its centre (40, 22) is the
diamond's centre, and that is what lands on home.

A ring on each detection, sized to how sure it is of where it is:

```jinja
{# object: detector = mu-1/detector #}
{# anchor: each detector.detections #}
{% set r = [view.r or 0, 8] | max %}
<svg xmlns="http://www.w3.org/2000/svg" width="{{ 2 * r + 4 }}" height="{{ 2 * r + 4 }}">
  <circle cx="{{ r + 2 }}" cy="{{ r + 2 }}" r="{{ r }}" fill="none" stroke="yellow" stroke-width="2"/>
</svg>
```

A line from where the rangefinder hit to the region of interest:

```jinja
{# object: spot = mu-1/robot_gimbal1_range #}
{# object: roi = mu-1/robot_roi #}
{# project: spot, roi #}
<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%"
     viewBox="0 0 {{ frame.width }} {{ frame.height }}">
  {% if view.spot and view.roi %}
  <line x1="{{ view.spot.x }}" y1="{{ view.spot.y }}" x2="{{ view.roi.x }}" y2="{{ view.roi.y }}"
        stroke="white" stroke-dasharray="6 4"/>
  {% endif %}
</svg>
```

## What it doesn't tell you

- **`r` is the object's own uncertainty, not the camera's.** It answers
  "where in the world could this be, as seen from here", not "where in
  this picture will it appear". These two differ when the object and the
  camera share an error. A rangefinder spot on the camera's own gimbal
  carries the robot's GPS error in its accuracy, but that error moves the
  camera too, so in the camera's own picture the spot sits on the axis
  however large `r` is. Draw `r` when the "where in the world" question is
  the one the widget answers.
- **The pose is the latest one,** not the pose at the instant the frame
  was taken: SpiriSynq poses carry no time yet. While a gimbal slews, an
  anchored widget trails the picture by however stale the pose is.
- **The lens is a pinhole,** with no distortion. Near the edge of a wide
  lens, a marker is some pixels off.
- **The camera's own mount is taken as exact:** a camera that is a few
  centimetres off its gimbal's axis, or not quite aligned with it, puts
  everything that far off.
