"""SVG HUD overlays, baked into frames before they are encoded.

An overlay is a :py:class:`HudWidget`: a small SVG template, anchored to a
corner or a percentage coordinate of the frame. Widgets are not owned by
the camera that draws them -- each is its own :py:class:`SyncableObject`,
published wherever its author runs (a UI panel, an ML detector, ...), and
:py:class:`OverlayMixin` only mirrors the ones a camera's
``overlay_widgets`` names.

A widget's SVG is templated with MiniJinja against three namespaces:
``exif`` (the current frame's tags, see :py:mod:`SpiriCamera.exif`),
``frame`` (the current frame's actual, as-received width/height/framerate
-- see :py:attr:`FRAME_NAME`), and
``objects`` (the latest field values of whatever other SpiriSynq objects
the widget declares for itself). ``frame`` exists alongside ``objects``
rather than folded into it because these values -- what the *rendered*
frame actually is, this call -- are never published as synced fields at
all (``CameraBase.synq_skip_sync``, see :py:func:`camera_metrics_widget`);
there is no topic a widget could declare to reach them, only the local
values :py:meth:`OverlayMixin._render_overlays` already has in hand for
the frame it is about to composite onto.

A real topic path is never written directly into a ``{{ }}`` expression
-- only an alias is, as ``{{ objects.<alias>.<field> }}``, because a
zenoh topic can contain characters (a hyphen in a hostname, say) that are
not valid there. The alias, and the *default* object it resolves to, are
declared right next to where the alias is used, as a MiniJinja comment
MiniJinja itself already strips at render time and that never appears in
the rendered SVG::

    {# object: battery = azrael/battery_monitor #}
    <text>{{ objects.battery.voltage }}</text>

A declared object is a whole :py:class:`~SpiriSynq.syncable_objects.SyncableObject`
-- every one of its synced fields is available as ``objects.<alias>.<field>``,
mirrored generically via
:py:meth:`~SpiriSynq.session.Session.from_topic_untyped` (so the widget
author needs no locally-importable class for it, and never sees its RPC
methods -- ``from_topic_untyped`` only ever exposes synced attributes).
This is templated in :py:func:`resolve_objects`, which reads both halves
of that same text -- MiniJinja's own static analysis
(``undeclared_variables_in_str``) for what the render body actually
reads, against :py:func:`declared_objects` for what has been declared --
and raises if a template reads an alias with no matching declaration,
which is meant to be caught by whoever is authoring the widget, not
silently ignored.

The declared topic is only a *default*. Whichever camera is actually
rendering the widget can override it -- to point ``battery`` at a
different concrete object than the widget author had in mind -- via
:py:attr:`OverlayMixin.overlay_widgets`, without touching the shared
:py:class:`HudWidget` itself (which would repoint it for every other
camera mirroring the same widget too). The same field also carries a
per-camera position override, for exactly the same reason: a widget's own
``anchor``/``custom_anchor_x``/``custom_anchor_y`` are its default
placement, and a camera can place it elsewhere for itself alone. See
:py:attr:`OverlayMixin.overlay_widgets` for the shape of both.

Rendered SVG is rasterized with ``thorvg_python`` at its own **natural**
pixel size (``Picture.get_size()``), never scaled to fit a declared box:
a widget author who writes ``font-size="24"`` gets a real 24px on screen
regardless of the camera's resolution. Scaling a small raster up to fit a
4K frame (or down to fit a low-res one) would either blur the text or
make it illegible -- crisp, fixed-size text was chosen over staying a
constant fraction of the frame across resolutions.

The one deliberate exception is a root ``<svg>`` whose ``width`` and
``height`` are *both* percentages (``width="20%" height="20%"``): that
scales the whole widget, preserving its own aspect ratio, to fit within
that percentage of the frame being rendered onto -- see
:py:func:`rasterize`. This is opt-in, not a default: a widget author who
wants "always a fifth of the picture, whatever that picture's
resolution" now has a way to say so, alongside the natural-pixel-size
behaviour above, which stays the default for a plain ``width="260"``.
Which is right depends on the widget -- fixed-size text and a HUD panel
generally want to stay crisp and constant-sized; a logo, icon, or other
graphic that is meant to track the frame's proportions wants percentage
sizing instead.
"""

from __future__ import annotations

import functools
import importlib.resources
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import minijinja
import numpy as np
import thorvg_python as thorvg
from loguru import logger
from psygnal.containers import EventedDict
from SpiriSynq.syncable_objects import SyncableObject

if TYPE_CHECKING:
    # Deferred: SpiriCamera.camera imports this module, not the other
    # way around -- only the type hint on camera_metrics_widget needs it.
    from SpiriCamera.camera import Camera

#: Fractional (x, y) of the *frame* that a named anchor's widget corner
#: should land on. ``0.5`` centres the widget over that axis; ``1.0``
#: flushes the widget's far edge against the frame's far edge.
_NAMED_ANCHORS: dict[str, tuple[float, float]] = {
    "top_left": (0.0, 0.0),
    "top_middle": (0.5, 0.0),
    "top_right": (1.0, 0.0),
    "center_left": (0.0, 0.5),
    "center_middle": (0.5, 0.5),
    "center_right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0),
    "bottom_middle": (0.5, 1.0),
    "bottom_right": (1.0, 1.0),
}

#: Every valid :py:attr:`HudWidget.anchor` value, named anchors plus
#: ``"custom"`` -- for a UI to offer as a choice, since ``_NAMED_ANCHORS``
#: alone would leave "custom" out.
ANCHORS: tuple[str, ...] = (*_NAMED_ANCHORS, "custom")

#: Top-level template name a widget's declared, live SpiriSynq objects are
#: exposed under, e.g. ``{{ objects.battery.voltage }}`` for the alias
#: ``battery``.
OBJECTS_NAME = "objects"

#: Top-level template name the current frame's EXIF tags are exposed under.
EXIF_NAME = "exif"

#: Top-level template name the current frame's actual, as-received
#: ``width``/``height``/``framerate`` are exposed under, e.g.
#: ``{{ frame.width }}x{{ frame.height }}``. See the module docstring for
#: why these are injected directly rather than reached through ``objects``.
FRAME_NAME = "frame"

#: Matches an object declaration comment: `{# object: alias = real/topic #}`.
#: A dedicated, narrow syntax we own completely -- not an attempt to
#: parse arbitrary MiniJinja expression syntax -- which is what keeps it
#: reliable: the literal words `object:` and `=` are fixed, the alias is
#: a plain identifier, and everything from `=` up to `#}` is the topic,
#: whitespace-trimmed.
_OBJECT_DECLARATION_RE = re.compile(
    r"\{#\s*object:\s*(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<topic>[^#]+?)\s*#\}"
)

_env = minijinja.Environment()
# Default ("chainable") undefined behavior renders a bare missing value as
# an empty string rather than raising -- fine for `{{ objects.a.b }}` where
# `a` is entirely absent (accessing `.b` on it does raise), but a plain
# `{{ objects.<alias>.<field> }}` with no value yet would then silently
# render as "" instead of the per-frame render failure this module relies
# on to skip a widget until its alias has resolved to something.
_env.undefined_behavior = "strict"

#: One process-wide software-rendering engine. thorvg initializes itself
#: once; a second engine per widget would be pure overhead.
_engine = thorvg.Engine()
_engine.init(threads=0)

#: The name every bundled-font ``<text font-family="...">`` in this
#: module's own templates uses, and the only font guaranteed to be
#: available regardless of the host -- see the loading block below.
#: Not an arbitrary label: ``font_load()`` registers a font under the
#: family name baked into the file's own metadata, so this has to match
#: Miracode's actual name table entry, verified empirically below.
DEFAULT_FONT = "Miracode"

#: Bundled rather than discovered from the host's installed fonts:
#: thorvg does not fall back to any OS text rendering -- an SVG
#: `<text>` element with no matching loaded font simply draws nothing,
#: on every platform, including the slim Docker image this project
#: ships (which has no fonts installed at all). A widget author who
#: forgets to reference a loaded font by name gets a silently blank
#: widget, which is exactly the failure this avoids for SpiriCamera's
#: own templates.
#:
#: Loaded from a real file path (`font_load`), not `font_load_data`
#: from in-memory bytes -- this build of thorvg returns
#: `Result.NOT_SUPPORTED` for `font_load_data` regardless of the
#: mimetype string given it, but the file-path loader works. Simpler to
#: resolve the packaged asset's real on-disk path directly than to
#: route around that through `importlib.resources.as_file()`'s
#: context-manager lifetime for what is a one-time, process-lifetime
#: registration anyway.
#:
#: Miracode was picked for legibility at the small sizes an overlay
#: renders at, not for completeness -- it is missing the Ukrainian
#: letters U+0490/U+0491 (Ge with upturn). A widget that needs those
#: specifically should load and reference its own font instead of
#: relying on this default; see `thorvg_python.paint.text.Text.font_load`.
#:
#: font_load() is a global registration despite living on a `Text`
#: instance -- the instance itself is discarded immediately, only used
#: to reach the underlying (process-wide, thorvg-internal) font cache.
_font_path = importlib.resources.files("SpiriCamera").joinpath("assets/Miracode.ttf")
_font_result = thorvg.Text(_engine).font_load(str(_font_path))
if _font_result != thorvg.Result.SUCCESS:
    logger.warning(f"overlay: could not load bundled font {DEFAULT_FONT!r}: {_font_result}")


class OverlayError(ValueError):
    """Raised when a widget's SVG cannot be templated or rasterized."""


@dataclass
class HudWidget(SyncableObject):
    """One overlay element: an SVG template anchored on the frame.

    Owned by whoever authors the overlay, not by the camera that draws
    it -- a camera only mirrors widgets named in its ``overlay_widgets``
    (see :py:class:`OverlayMixin`), it never creates or publishes one.

    Attributes
    ----------
    svg_template : str
        MiniJinja source for the widget's SVG. May reference
        ``{{ exif.* }}`` for the current frame's tags, and
        ``{{ objects.<alias>.<field> }}`` for any other SpiriSynq
        object's latest field values, provided the same alias is
        declared with a ``{# object: <alias> = <default topic> #}``
        comment somewhere in this same template; see
        :py:func:`resolve_objects`. The declared topic is only a
        default -- a camera rendering this widget can bind the alias to
        a different object of its own choosing; see
        :py:attr:`OverlayMixin.overlay_widgets`.
    anchor : str
        This widget's *default* placement: one of the nine-point grid
        (``top_left`` .. ``bottom_right``) or ``"custom"`` to use
        :py:attr:`custom_anchor_x`/:py:attr:`custom_anchor_y` instead.
        A camera rendering this widget can override the placement for
        itself alone; see :py:attr:`OverlayMixin.overlay_widgets`.
    custom_anchor_x : float
        Percent (0-100) of the frame's width. Only used when
        :py:attr:`anchor` is ``"custom"``, in which case it names the
        widget's top-left corner directly -- unlike the named anchors,
        which use the widget's own rendered size to flush it against an
        edge or centre it.
    custom_anchor_y : float
        Percent (0-100) of the frame's height. See
        :py:attr:`custom_anchor_x`.
    """

    svg_template: str = ""
    anchor: str = "top_left"
    custom_anchor_x: float = 0.0
    custom_anchor_y: float = 0.0


def declared_objects(svg_template: str) -> dict[str, str]:
    """Alias -> default SpiriSynq topic declarations in a widget's own
    template.

    A widget declares every object it needs right where it uses it, as a
    comment MiniJinja already strips at render time::

        {# object: battery = azrael/battery_monitor #}

    Parameters
    ----------
    svg_template : str
        A :py:attr:`HudWidget.svg_template`.

    Returns
    -------
    dict[str, str]
        ``{alias: default_topic}``, one entry per declaration found. A
        template with none declares nothing.
    """
    return {
        match["alias"]: match["topic"]
        for match in _OBJECT_DECLARATION_RE.finditer(svg_template)
    }


def resolve_objects(svg_template: str) -> dict[str, str]:
    """Aliases a widget's template actually reads, checked against its
    own declarations.

    Cross-checks two things read out of the same template text: every
    ``{{ objects.<alias>.<field> }}`` MiniJinja's own static analysis
    (``undeclared_variables_in_str``) finds the render body actually
    using, against every alias :py:func:`declared_objects` finds
    declared. This is the enforcement point the module docstring
    describes -- a widget author gets a clear, immediate error for a
    typo'd or missing declaration, rather than a silently-undefined
    value only visible in the rendered picture.

    Parameters
    ----------
    svg_template : str
        A :py:attr:`HudWidget.svg_template`.

    Returns
    -------
    dict[str, str]
        ``{alias: default_topic}``, restricted to aliases the template
        body actually reads -- a declared-but-unused alias is not
        mirrored.

    Raises
    ------
    OverlayError
        If the template reads ``objects.<alias>`` for an alias with no
        matching declaration.
    """
    prefix = f"{OBJECTS_NAME}."
    names = _env.undeclared_variables_in_str(svg_template, nested=True)
    used: set[str] = set()
    for name in names:
        if not name.startswith(prefix):
            continue
        # Only the segment right after `objects.` is the alias --
        # `objects.battery.voltage` reads field `voltage` off alias
        # `battery`, and that deeper access is the expected shape now
        # that an alias names a whole object, not one scalar value.
        alias = name.removeprefix(prefix).split(".", 1)[0]
        if alias:
            used.add(alias)

    declared = declared_objects(svg_template)
    missing = sorted(used - declared.keys())
    if missing:
        raise OverlayError(
            f"template reads objects.{missing[0]} with no matching "
            f"'{{# object: {missing[0]} = ... #}}' declaration"
        )

    return {alias: declared[alias] for alias in used}


def render_svg(
    svg_template: str,
    *,
    objects: Mapping[str, Mapping[str, object]],
    exif_tags: Mapping[str, str],
    frame_info: Mapping[str, object],
) -> str:
    """Render a widget's template against live object field values, EXIF
    tags, and the actual frame being rendered onto.

    Parameters
    ----------
    svg_template : str
        A :py:attr:`HudWidget.svg_template`.
    objects : Mapping[str, Mapping[str, object]]
        ``{alias: {field: value}}`` for the aliases :py:func:`resolve_objects`
        found in use, exposed as ``{{ objects.<alias>.<field> }}``. An
        alias the template reads but that is missing here renders as
        undefined, which MiniJinja raises on -- see ``Raises`` below.
    exif_tags : Mapping[str, str]
        The current frame's tags, exposed as ``{{ exif.* }}``.
    frame_info : Mapping[str, object]
        The current frame's actual ``width``/``height``/``framerate``,
        exposed as ``{{ frame.* }}``. See :py:attr:`FRAME_NAME` and the
        module docstring for why these are injected directly instead of
        being reached through ``objects``.

    Returns
    -------
    str
        The rendered SVG source.

    Raises
    ------
    OverlayError
        If the template is malformed, or reads an alias with no value
        yet (e.g. an object that has not resolved, or a field it does
        not have). Either way this is one frame's problem, not a reason
        to stop rendering the widget forever -- the caller is expected
        to retry on the next frame.
    """
    try:
        return _env.render_str(
            svg_template,
            **{
                OBJECTS_NAME: {alias: dict(fields) for alias, fields in objects.items()},
                EXIF_NAME: dict(exif_tags),
                FRAME_NAME: dict(frame_info),
            },
        )
    except Exception as exc:
        raise OverlayError(f"could not render overlay template: {exc}") from exc


#: Matches a root ``<svg ...>`` opening tag.
_ROOT_SVG_TAG_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)

#: Matches a ``width="20%"``/``height="20%"`` attribute within a tag
#: matched by ``_ROOT_SVG_TAG_RE``.
_PERCENT_SIZE_ATTR_RE = re.compile(r'\b(width|height)\s*=\s*"(-?[0-9]+(?:\.[0-9]+)?)%"')


def _percent_size(svg: str) -> tuple[float, float] | None:
    """``(width%, height%)`` if the root ``<svg>`` declares both as
    percentages, else ``None``.

    All-or-nothing: a ``width`` given as a percentage with an absolute
    ``height`` (or vice versa) does not count -- there is no meaning for
    "20% wide, 50px tall" in :py:func:`rasterize`.
    """
    match = _ROOT_SVG_TAG_RE.search(svg)
    if match is None:
        return None
    attrs = dict(_PERCENT_SIZE_ATTR_RE.findall(match.group(0)))
    if "width" not in attrs or "height" not in attrs:
        return None
    return float(attrs["width"]), float(attrs["height"])


def _strip_percent_size(svg: str) -> str:
    """Drop percentage ``width``/``height`` from the root ``<svg>`` tag.

    Leaves thorvg to fall back to the SVG's own ``viewBox`` for its
    natural size -- see :py:func:`rasterize`, which scales that natural
    size against the *frame* itself, rather than let thorvg resolve the
    percentage against the ``viewBox`` (a different, and here irrelevant,
    reference size).
    """
    def _strip(match: re.Match[str]) -> str:
        return _PERCENT_SIZE_ATTR_RE.sub("", match.group(0))

    return _ROOT_SVG_TAG_RE.sub(_strip, svg, count=1)


def _load_and_measure(
    svg: str,
    frame_width: int | None,
    frame_height: int | None,
) -> tuple[thorvg.Picture, float, float]:
    """Parse ``svg`` into a thorvg ``Picture`` and resolve its pixel
    size, without drawing it.

    Shared by :py:func:`rasterize` (which draws the returned picture)
    and :py:func:`measure_size` (which only needs the size) -- see
    :py:func:`rasterize` for the percentage-sizing behavior this
    implements.

    Raises
    ------
    OverlayError
        If the SVG could not be parsed, declares no size, or uses
        percentage sizing with no frame size given to scale it against.
    """
    percent_size = _percent_size(svg)
    if percent_size is not None:
        if frame_width is None or frame_height is None:
            raise OverlayError(
                "overlay SVG has a percentage width/height but no frame "
                "size was given to scale it against"
            )
        svg = _strip_percent_size(svg)

    picture = thorvg.Picture(_engine)
    result = picture.load_data(svg.encode("utf-8"), mimetype="svg", rpath=None, copy=True)
    if result != 0:
        raise OverlayError(f"thorvg could not parse overlay SVG (result {result})")

    _, natural_width, natural_height = picture.get_size()
    if natural_width <= 0 or natural_height <= 0:
        raise OverlayError(
            f"overlay SVG has no natural size ({natural_width}x{natural_height})"
        )

    if percent_size is not None:
        width_pct, height_pct = percent_size
        box_width = width_pct / 100.0 * frame_width
        box_height = height_pct / 100.0 * frame_height
        # The smaller of the two ratios, applied uniformly to both axes,
        # so the widget fits *within* the percentage box without being
        # stretched out of its own proportions.
        scale = min(box_width / natural_width, box_height / natural_height)
        natural_width *= scale
        natural_height *= scale
        picture.set_size(natural_width, natural_height)

    return picture, natural_width, natural_height


def measure_size(
    svg: str,
    frame_width: int | None = None,
    frame_height: int | None = None,
) -> tuple[int, int]:
    """The pixel size :py:func:`rasterize` would draw ``svg`` at, without
    actually rasterizing it.

    Used by the client-render path (:py:meth:`OverlayMixin.render_overlays_for_client`)
    to keep a browser-laid-out widget's box pixel-identical to the one
    :py:func:`anchor_position` assumes server-side, without paying for a
    software rasterization pass just to learn a size.

    Parameters, return value, and exceptions: see :py:func:`rasterize`.
    """
    _, natural_width, natural_height = _load_and_measure(svg, frame_width, frame_height)
    width, height = math.ceil(natural_width), math.ceil(natural_height)
    if width <= 0 or height <= 0:
        raise OverlayError(f"overlay SVG has no natural size ({width}x{height})")
    return width, height


def _normalize_svg_size(svg: str, width: float, height: float) -> str:
    """Overwrite the root ``<svg>``'s ``width``/``height`` (percentage or
    absolute, whatever it currently declares) with explicit pixel values.

    Used by the client-render path so the ``<svg>`` element a browser
    actually lays out occupies exactly the box :py:func:`measure_size`
    (and, before it, :py:func:`anchor_position`) computed -- otherwise a
    percentage-sized widget would be laid out at its full declared box
    and its content merely letterboxed inside it, drifting off the
    anchor position computed for the smaller, aspect-fit box.
    """
    def _replace(match: re.Match[str]) -> str:
        tag = _PERCENT_SIZE_ATTR_RE.sub("", match.group(0))
        tag = re.sub(r'\s(width|height)\s*=\s*"[^"]*"', "", tag)
        return tag[:-1] + f' width="{width}" height="{height}">'

    return _ROOT_SVG_TAG_RE.sub(_replace, svg, count=1)


def rasterize(
    svg: str,
    frame_width: int | None = None,
    frame_height: int | None = None,
) -> np.ndarray:
    """Parse and rasterize an SVG, at its own natural pixel size or
    scaled to a percentage of the frame.

    Never forces a size for an ordinarily-sized widget: its declared
    ``width``/``height`` or ``viewBox`` *is* the pixel size it is
    rendered and, later, composited at, unscaled. See the module
    docstring for why, and for the one exception this function does
    implement: a root ``<svg>`` whose ``width`` and ``height`` are both
    percentages is scaled -- preserving its own aspect ratio -- to fit
    within that percentage of ``frame_width``/``frame_height``, the same
    way CSS ``object-fit: contain`` would.

    Parameters
    ----------
    svg : str
        Rendered SVG source, as returned by :py:func:`render_svg`.
    frame_width, frame_height : int, optional
        Size of the frame this is about to be composited onto. Only
        consulted -- and required -- when ``svg``'s root ``width``/
        ``height`` are both percentages; ignored otherwise.

    Returns
    -------
    np.ndarray
        An ``(height, width, 4)`` ``uint8`` array, straight (not
        premultiplied) RGBA, transparent where nothing was drawn.

    Raises
    ------
    OverlayError
        If the SVG could not be parsed, declares no size to render at,
        or uses percentage sizing with no frame size given to scale it
        against.
    """
    picture, natural_width, natural_height = _load_and_measure(svg, frame_width, frame_height)

    width, height = math.ceil(natural_width), math.ceil(natural_height)
    if width <= 0 or height <= 0:
        raise OverlayError(f"overlay SVG has no natural size ({width}x{height})")

    canvas = thorvg.SwCanvas(_engine)
    canvas.set_target(width, height)
    canvas.add(picture)
    canvas.draw(True)
    canvas.sync()

    buffer = np.ctypeslib.as_array(canvas.buffer_arr)
    # .copy(): buffer_arr's backing memory belongs to `canvas`, which is
    # local to this call and free to be garbage-collected right after.
    return buffer.view(np.uint8).reshape(height, width, 4).copy()


def anchor_position(
    anchor: str,
    custom_x: float,
    custom_y: float,
    frame_width: int,
    frame_height: int,
    widget_width: int,
    widget_height: int,
) -> tuple[int, int]:
    """Pixel position for a widget's top-left corner.

    Parameters
    ----------
    anchor : str
        A :py:attr:`HudWidget.anchor` value.
    custom_x, custom_y : float
        :py:attr:`HudWidget.custom_anchor_x`/``_y``, used only when
        ``anchor == "custom"``.
    frame_width, frame_height : int
        Size of the frame the widget is being composited onto.
    widget_width, widget_height : int
        Size of the rasterized widget, i.e. :py:func:`rasterize`'s
        output shape.

    Returns
    -------
    tuple[int, int]
        ``(x, y)`` of the widget's top-left corner, in frame pixels. Not
        clamped to the frame -- :py:func:`composite` clips it.
    """
    if anchor == "custom":
        return (
            round(custom_x / 100.0 * frame_width),
            round(custom_y / 100.0 * frame_height),
        )

    fraction_x, fraction_y = _NAMED_ANCHORS.get(anchor, _NAMED_ANCHORS["top_left"])
    return (
        round(fraction_x * (frame_width - widget_width)),
        round(fraction_y * (frame_height - widget_height)),
    )


def composite(frame: np.ndarray, rgba: np.ndarray, x: int, y: int) -> np.ndarray:
    """Alpha-blend a rasterized widget onto a copy of a BGR frame.

    Parameters
    ----------
    frame : np.ndarray
        A ``(height, width, 3)`` BGR frame, as read from a source.
    rgba : np.ndarray
        A widget raster from :py:func:`rasterize`.
    x, y : int
        Top-left corner to place ``rgba`` at, from
        :py:func:`anchor_position`. May be negative, or place ``rgba``
        partly or fully outside ``frame``; the overlap is clipped.

    Returns
    -------
    np.ndarray
        A new array -- ``frame`` is never mutated in place, so a camera
        with no overlays configured is unaffected by this module, and
        :py:meth:`~SpiriCamera.camera.Camera._encode`'s
        cache-by-identity check still sees a stable object when overlays
        happen to render byte-identical output.
    """
    result = frame.copy()
    frame_height, frame_width = result.shape[:2]
    widget_height, widget_width = rgba.shape[:2]

    src_x, src_y = max(0, -x), max(0, -y)
    dst_x, dst_y = max(0, x), max(0, y)
    width = min(widget_width - src_x, frame_width - dst_x)
    height = min(widget_height - src_y, frame_height - dst_y)
    if width <= 0 or height <= 0:
        return result

    region = rgba[src_y : src_y + height, src_x : src_x + width]
    alpha = region[:, :, 3:4].astype(np.float32) / 255.0
    rgb_as_bgr = region[:, :, 2::-1].astype(np.float32)
    background = result[dst_y : dst_y + height, dst_x : dst_x + width].astype(np.float32)
    blended = rgb_as_bgr * alpha + background * (1.0 - alpha)
    result[dst_y : dst_y + height, dst_x : dst_x + width] = blended.astype(np.uint8)
    return result


# ---------------------------------------------------------------------------
# Ready-made widgets
# ---------------------------------------------------------------------------


def camera_metrics_widget(camera: "Camera", *, anchor: str = "bottom_left") -> HudWidget:
    """Build and publish a widget showing a camera's own live stats.

    A ready-made overlay rather than a hand-authored one: baking a
    camera's actual resolution, framerate, and quality onto its own
    picture is common enough -- a debugging HUD, a sanity check that
    frames are really arriving at the rate they should -- to not be
    re-typed as SVG each time.

    ``width``/``height``/``framerate`` come from ``{{ frame.* }}``,
    injected directly by :py:meth:`OverlayMixin._render_overlays` rather
    than read as an object field: ``received_width``/``received_height``/
    ``received_framerate`` are deliberately never published as fields of
    their own (see ``CameraBase.synq_skip_sync``), so there is no live
    object this widget -- or any widget -- could declare to reach them;
    see the module docstring for why ``frame`` exists as its own
    namespace for exactly this. ``quality`` is read *live* off
    ``camera`` itself instead, exactly like any other overlay widget's
    ``objects.<alias>.<field>`` values -- ``camera`` is consulted only
    here, at build time, to learn its own topic, which becomes the
    ``cam`` alias's *default* binding (see the module docstring): no
    separate wiring step is needed, ``cam.overlay_widgets[widget_topic]
    = {}`` is enough to mirror this widget and have it resolve ``cam``
    to ``camera`` itself.

    The second line reads ``{{ exif.timestamp }}`` -- the frame's own
    capture time, unlike the render-time values above -- with a MiniJinja
    ``default`` filter so ``Camera.exif_enabled = False`` (which leaves
    ``exif_tags`` empty) renders ``"untagged"`` instead of failing the
    whole widget's render under this module's strict-undefined setting.

    Parameters
    ----------
    camera : SpiriCamera.camera.Camera
        The camera to describe.
    anchor : str
        Where to anchor the widget; see :py:attr:`HudWidget.anchor`.

    Returns
    -------
    HudWidget
        A published, authoritative widget under its own topic
        (``f"{camera.synq_topic}_metrics"``). The caller owns it, and
        should ``close()`` it when done -- typically alongside
        ``camera`` itself.
    """
    topic = camera.synq_absolute_path

    svg_template = f"""\
{{# object: cam = {topic} #}}
<svg xmlns="http://www.w3.org/2000/svg" width="260" height="52">
  <rect width="260" height="52" fill="black" fill-opacity="0.55"/>
  <text x="8" y="20" font-family="{DEFAULT_FONT}" font-size="16" fill="white"
      >{{{{ frame.width }}}}x{{{{ frame.height }}}} {{{{ frame.framerate }}}}fps q={{{{ objects.cam.quality }}}}</text>
  <text x="8" y="40" font-family="{DEFAULT_FONT}" font-size="16" fill="white"
      >{{{{ exif.timestamp | default("untagged") }}}}</text>
</svg>"""

    return HudWidget(
        synq_topic=f"{camera.synq_topic}_metrics",
        synq_authoritive=True,
        anchor=anchor,
        svg_template=svg_template,
    )


#: The bundled Ghostscript tiger, as vendored -- a ``viewBox="0 0 900
#: 900"`` and no ``width``/``height`` of its own. :py:func:`tiger_widget`
#: adds a percentage ``width``/``height`` on top of this at build time,
#: so the fraction of the frame it fills is a parameter, not baked into
#: the file.
_tiger_svg_path = importlib.resources.files("SpiriCamera").joinpath("assets/tiger.svg")


def tiger_widget(
    camera: "Camera", *, size_percent: float = 20.0, anchor: str = "center_middle"
) -> HudWidget:
    """Build and publish a widget showing the classic Ghostscript tiger.

    A ready-made demonstration of percentage sizing (see
    :py:func:`rasterize`): the vendored artwork is a fixed, unrelated
    900x900 ``viewBox``, but ``size_percent`` is baked into the widget's
    root ``<svg width="{size_percent}%" height="{size_percent}%">`` so it
    always renders at that fraction of the frame -- shrinking or growing
    with the camera's resolution -- rather than at some fixed pixel size
    that would look tiny on a 4K frame and oversized on a small one.

    Parameters
    ----------
    camera : SpiriCamera.camera.Camera
        The camera this widget's topic is namespaced under. The artwork
        itself does not depend on ``camera`` otherwise.
    size_percent : float
        Fraction of the frame, in each dimension, the tiger is scaled to
        fit within, preserving its own aspect ratio.
    anchor : str
        Where to anchor the widget; see :py:attr:`HudWidget.anchor`.

    Returns
    -------
    HudWidget
        A published, authoritative widget under its own topic
        (``f"{camera.synq_topic}_tiger"``). The caller owns it, and
        should ``close()`` it when done -- typically alongside
        ``camera`` itself.
    """
    svg = _tiger_svg_path.read_text(encoding="utf-8")
    svg_template = _ROOT_SVG_TAG_RE.sub(
        lambda match: match.group(0).replace(
            "<svg ", f'<svg width="{size_percent}%" height="{size_percent}%" ', 1
        ),
        svg,
        count=1,
    )

    return HudWidget(
        synq_topic=f"{camera.synq_topic}_tiger",
        synq_authoritive=True,
        anchor=anchor,
        svg_template=svg_template,
    )


# ---------------------------------------------------------------------------
# Mixin
# ---------------------------------------------------------------------------


@dataclass
class OverlayMixin:
    """Overlay support for a :py:class:`~SpiriCamera.camera.CameraBase`.

    The only synced field is :py:attr:`overlay_widgets`; every other
    piece of state this needs (mirrored widgets, mirrored data-source
    objects, per-widget error de-duplication, cached template-parse
    results) is plain instance state the host class sets up itself -- see
    :py:class:`~SpiriCamera.camera.Camera`'s constructor, which does the
    same for its own EXIF tag providers. This mixin has no
    ``__post_init__`` of its own and starts nothing by itself; the host
    class calls :py:meth:`_overlay_sync_widgets` once after construction
    (to pick up a rehydrated or constructor-supplied ``overlay_widgets``
    immediately) and :py:meth:`_render_overlays` from its own capture
    pipeline.
    """

    #: ``{widget_topic: usage}``, where ``widget_topic`` is a mirrored
    #: :py:class:`HudWidget`'s absolute topic and ``usage`` is a plain
    #: dict of *this camera's* overrides for that widget -- everything in
    #: it is optional:
    #:
    #: - ``"x"``, ``"y"``: percent-of-frame position, overriding the
    #:   widget's own ``anchor``/``custom_anchor_x``/``custom_anchor_y``
    #:   for this camera alone (same semantics as the widget's own
    #:   ``custom`` anchor). Omitted -> use the widget's own placement,
    #:   same as every other camera mirroring it.
    #: - ``"bindings"``: ``{alias: object_topic}``, overriding which
    #:   object a widget's declared alias resolves to, for this camera
    #:   alone. Omitted, or missing a given alias -> use that alias's own
    #:   ``{# object: alias = default_topic #}`` declaration.
    #:
    #: A widget's mere presence as a key is what makes this camera render
    #: it -- there is no separate list of topics. An
    #: :py:class:`~psygnal.containers.EventedDict`, not a plain ``dict``,
    #: so SpiriSynq republishes the whole mapping on any mutation (add,
    #: remove, or edit a usage dict) the same way it already does for any
    #: other evented container field -- see
    #: ``SyncableObject._zenoh_publish_changes``. Live: editing this at
    #: runtime adds, drops, rebinds, or repositions widgets on the next
    #: frame, no restart needed.
    overlay_widgets: EventedDict = field(default_factory=EventedDict)

    #: When set, :py:meth:`~SpiriCamera.camera.Camera.read` skips
    #: :py:meth:`_render_overlays` entirely -- the raw frame is encoded
    #: unmodified, and a consumer (e.g. the NiceGUI UI) is expected to
    #: call :py:meth:`render_overlays_for_client` itself and render the
    #: result in the browser instead. A plain synced field like any
    #: other -- flippable live, no camera reconstruction needed.
    overlay_client_render: bool = False

    def _overlay_sync_widgets(self) -> None:
        """Reconcile mirrored widgets and mirrored data-source objects
        against the current ``overlay_widgets``.

        Only does network work (mirroring a widget, mirroring an object)
        the first time a topic is seen -- an already-mirrored widget or
        object is left alone and trusted to update itself live via its
        own SpiriSynq subscription, not re-fetched. That makes this cheap
        enough to call every frame (see :py:meth:`_render_overlays`),
        which is what actually keeps newly-added or newly-rebound
        entries picked up promptly without needing fine-grained
        ``EventedDict`` change signals wired up.

        Tolerant of a topic that does not resolve yet: it is retried on
        the next call rather than raising, since ``overlay_widgets`` is
        meant to be editable through a half-typed value the same way
        ``source_str`` is.
        """
        wanted = set(self.overlay_widgets.keys())

        for topic in list(self._overlay_widgets):
            if topic not in wanted:
                self._overlay_widgets.pop(topic).close()
                self._overlay_invalidate_widget_cache(topic)

        for topic in wanted:
            if topic in self._overlay_widgets:
                continue
            try:
                widget = HudWidget.from_topic(topic, session=self.synq_session)
            except Exception as exc:
                self._overlay_complain(topic, f"overlay topic unavailable, {exc}")
                continue
            self._overlay_widgets[topic] = widget
            self._overlay_complaints.pop(topic, None)
            # Population is lazy (`_cached_declared_objects`/
            # `_cached_resolve_objects`), but invalidation has to be
            # eager: this is the only place that ever sees the widget's
            # *old* template, right before a remote edit can replace it.
            widget.events.svg_template.connect(
                functools.partial(self._overlay_invalidate_widget_cache, topic)
            )

        self._overlay_sync_objects()

    def _overlay_invalidate_widget_cache(self, topic: str, *_args: object) -> None:
        """Drop a widget's cached template-parse results.

        Connected to that widget's own ``events.svg_template`` signal in
        :py:meth:`_overlay_sync_widgets` -- a widget's template is edited
        far less often than it is rendered, so :py:func:`declared_objects`
        and :py:func:`resolve_objects` (a regex scan plus a MiniJinja
        static-analysis pass) are worth caching per widget rather than
        re-running every frame; this is what keeps that cache correct
        across a live template edit instead of serving stale aliases.
        """
        self._overlay_declared_cache.pop(topic, None)
        self._overlay_resolved_cache.pop(topic, None)

    def _cached_declared_objects(self, topic: str, widget: HudWidget) -> dict[str, str]:
        """:py:func:`declared_objects`, cached until ``widget``'s template
        changes -- see :py:meth:`_overlay_invalidate_widget_cache`."""
        cached = self._overlay_declared_cache.get(topic)
        if cached is None:
            cached = declared_objects(widget.svg_template)
            self._overlay_declared_cache[topic] = cached
        return cached

    def _cached_resolve_objects(self, topic: str, widget: HudWidget) -> dict[str, str]:
        """:py:func:`resolve_objects`, cached until ``widget``'s template
        changes -- see :py:meth:`_overlay_invalidate_widget_cache`.

        Deliberately does not cache an :py:exc:`OverlayError` -- a
        template with a missing declaration should keep failing loudly
        (via :py:meth:`_overlay_complain`) on every call, not just once.
        """
        cached = self._overlay_resolved_cache.get(topic)
        if cached is None:
            cached = resolve_objects(widget.svg_template)
            self._overlay_resolved_cache[topic] = cached
        return cached

    def _overlay_sync_objects(self) -> None:
        """Reconcile mirrored data-source objects against every mirrored
        widget's declared aliases and this camera's binding overrides.

        Companion to :py:meth:`_overlay_sync_widgets`, same "only fetch a
        topic the first time it's seen, then trust the subscription"
        policy -- see there for why.
        """
        wanted_topics: set[str] = set()
        for widget_topic, widget in self._overlay_widgets.items():
            usage = self.overlay_widgets.get(widget_topic, {})
            overrides = usage.get("bindings", {})
            for alias, default_topic in self._cached_declared_objects(
                widget_topic, widget
            ).items():
                object_topic = overrides.get(alias) or default_topic
                if object_topic:
                    wanted_topics.add(object_topic)

        for topic in list(self._overlay_objects):
            if topic not in wanted_topics:
                self._overlay_objects.pop(topic).close()

        for topic in wanted_topics:
            if topic in self._overlay_objects:
                continue
            try:
                self._overlay_objects[topic] = self.synq_session.from_topic_untyped(topic)
            except Exception as exc:
                self._overlay_complain(topic, f"overlay object unavailable, {exc}")

    def _overlay_object_values_for(self, widget: HudWidget) -> dict[str, dict[str, object]]:
        """Latest known field values, keyed by alias, for each object
        ``widget`` declares and reads.

        Raises
        ------
        OverlayError
            Propagated from :py:func:`resolve_objects` if the template
            reads an alias it never declared.
        """
        widget_topic = widget.synq_absolute_path
        overrides = self.overlay_widgets.get(widget_topic, {}).get("bindings", {})

        values: dict[str, dict[str, object]] = {}
        for alias, default_topic in self._cached_resolve_objects(widget_topic, widget).items():
            object_topic = overrides.get(alias) or default_topic
            obj = self._overlay_objects.get(object_topic) if object_topic else None
            complaint_key = f"{widget_topic}#{alias}"
            if obj is None:
                self._overlay_complain(
                    complaint_key, f"object {object_topic!r} unavailable for alias {alias!r}"
                )
                continue
            self._overlay_complaints.pop(complaint_key, None)
            values[alias] = {
                field_name: getattr(obj, field_name)
                for field_name in type(obj).valid_sync_paths()
                if "/" not in field_name
            }
        return values

    def _overlay_complain(self, key: str, message: str) -> None:
        """Log a failure once per distinct message, not once per frame."""
        if self._overlay_complaints.get(key) == message:
            return
        self._overlay_complaints[key] = message
        logger.warning(f"{self.synq_topic}: {message}")

    def _render_overlays(self, frame: np.ndarray) -> np.ndarray:
        """Composite every resolvable widget onto ``frame``.

        No-ops -- returns ``frame`` unchanged, same object -- when
        ``overlay_widgets`` is empty, so a camera with no overlays
        configured sees zero behavioural change from this mixin,
        including :py:meth:`~SpiriCamera.camera.Camera._encode`'s
        cache-by-identity optimization.

        Parameters
        ----------
        frame : np.ndarray
            The raw BGR frame about to be encoded.

        Returns
        -------
        np.ndarray
            ``frame`` with every resolvable widget baked in. A widget
            that fails to render this frame (bad template, a declared
            alias with no value yet, a thorvg parse failure) is skipped
            and logged once, not raised -- one broken overlay should
            never stop the camera from publishing frames.
        """
        self._overlay_sync_widgets()
        if not self._overlay_widgets:
            return frame

        frame_height, frame_width = frame.shape[:2]
        # The frame actually being rendered, not what was requested --
        # see the module docstring and camera_metrics_widget for why
        # this is injected directly rather than read through `objects`.
        frame_info = {
            "width": frame_width,
            "height": frame_height,
            "framerate": round(self.received_framerate, 1),
        }
        result = frame
        for topic, widget in self._overlay_widgets.items():
            try:
                svg = render_svg(
                    widget.svg_template,
                    objects=self._overlay_object_values_for(widget),
                    exif_tags=self.exif_tags,
                    frame_info=frame_info,
                )
                rgba = rasterize(svg, frame_width, frame_height)
                widget_height, widget_width = rgba.shape[:2]
                usage = self.overlay_widgets.get(topic, {})
                if "x" in usage and "y" in usage:
                    x, y = anchor_position(
                        "custom",
                        usage["x"],
                        usage["y"],
                        frame_width,
                        frame_height,
                        widget_width,
                        widget_height,
                    )
                else:
                    x, y = anchor_position(
                        widget.anchor,
                        widget.custom_anchor_x,
                        widget.custom_anchor_y,
                        frame_width,
                        frame_height,
                        widget_width,
                        widget_height,
                    )
                result = composite(result, rgba, x, y)
            except OverlayError as exc:
                self._overlay_complain(topic, f"overlay {topic!r} not rendered, {exc}")
            else:
                self._overlay_complaints.pop(topic, None)

        return result

    def render_overlays_for_client(self, frame_width: int, frame_height: int) -> str:
        """SVG markup positioning every resolvable widget by itself, for
        a browser to render natively instead of :py:meth:`_render_overlays`
        rasterizing and compositing it server-side.

        Companion to :py:meth:`_render_overlays` for a camera with
        :py:attr:`overlay_client_render` set -- same widget iteration,
        same live object values, same :py:func:`anchor_position` math
        (so placement matches the server-rendered path pixel for pixel),
        but each widget is measured (:py:func:`measure_size`) rather
        than rasterized, then positioned with an SVG ``<g transform>``
        instead of being alpha-blended into a raster.

        Parameters
        ----------
        frame_width, frame_height : int
            The frame size overlays are anchored against -- typically
            ``self.received_width``/``self.received_height``, i.e. the
            size of the image the browser is actually displaying.

        Returns
        -------
        str
            Zero or more ``<g transform="translate(...)">...</g>``
            elements, one per resolvable widget, meant to be wrapped in
            a ``<svg viewBox="0 0 {frame_width} {frame_height}">`` by
            the caller. Empty when ``overlay_widgets`` is empty or no
            widget currently resolves -- same no-op contract as
            :py:meth:`_render_overlays`.
        """
        self._overlay_sync_widgets()
        if not self._overlay_widgets:
            return ""

        frame_info = {
            "width": frame_width,
            "height": frame_height,
            "framerate": round(self.received_framerate, 1),
        }
        pieces: list[str] = []
        for topic, widget in self._overlay_widgets.items():
            try:
                svg = render_svg(
                    widget.svg_template,
                    objects=self._overlay_object_values_for(widget),
                    exif_tags=self.exif_tags,
                    frame_info=frame_info,
                )
                widget_width, widget_height = measure_size(svg, frame_width, frame_height)
                usage = self.overlay_widgets.get(topic, {})
                if "x" in usage and "y" in usage:
                    x, y = anchor_position(
                        "custom",
                        usage["x"],
                        usage["y"],
                        frame_width,
                        frame_height,
                        widget_width,
                        widget_height,
                    )
                else:
                    x, y = anchor_position(
                        widget.anchor,
                        widget.custom_anchor_x,
                        widget.custom_anchor_y,
                        frame_width,
                        frame_height,
                        widget_width,
                        widget_height,
                    )
                sized_svg = _normalize_svg_size(svg, widget_width, widget_height)
            except OverlayError as exc:
                self._overlay_complain(topic, f"overlay {topic!r} not rendered, {exc}")
            else:
                self._overlay_complaints.pop(topic, None)
                pieces.append(f'<g transform="translate({x},{y})">{sized_svg}</g>')

        return "".join(pieces)

    def _overlay_close(self) -> None:
        """Release every mirrored widget and mirrored data-source object."""
        for widget in self._overlay_widgets.values():
            widget.close()
        self._overlay_widgets.clear()
        for obj in self._overlay_objects.values():
            obj.close()
        self._overlay_objects.clear()
        self._overlay_declared_cache.clear()
        self._overlay_resolved_cache.clear()


__all__ = [
    "ANCHORS",
    "EXIF_NAME",
    "FRAME_NAME",
    "OBJECTS_NAME",
    "HudWidget",
    "OverlayError",
    "OverlayMixin",
    "anchor_position",
    "camera_metrics_widget",
    "composite",
    "declared_objects",
    "rasterize",
    "render_svg",
    "resolve_objects",
    "tiger_widget",
]
