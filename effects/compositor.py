"""A tiny layer/slot compositor for the LED mapper (VISION Phase 3).

The old `LEDMapper.render()` was a flat pass-chain: a single RGB buffer that
every effect read and overwrote in turn.  When several overlays were active at
once (star-power lift + beat pulse + bonus burst…) they stacked in code order
and each clamped independently, so bright frames clipped to white in an
order-dependent "fight" (see VISION §"Layer / slot compositor").

This module gives the mapper the photonics-dmx model instead: each independent
element renders into its *own* pre-allocated buffer — a `Layer` — and a
`Compositor` folds the active layers together with an explicit blend mode
(REPLACE / ADD / MIX / MIX_LIT) and opacity.  The key property that ends the
"fight": MIX is a *convex* blend, so stacking two MIX-toward-white layers
screen-combines (`t = 1-(1-t1)(1-t2)`) and can never overshoot 255 — additive
whitening no longer clips per-pass.

Hot-path discipline (same as `mapper.py`): every buffer and alpha array is
allocated once at construction; nothing is allocated per frame.  All math is
flat-bytearray integer work with `min(255, …)` clamps.  Inactive layers are
skipped via the `active` flag, so an idle frame costs almost nothing.
"""

# ── Blend modes ──────────────────────────────────────────────────
# How a layer's pixels combine with the base buffer beneath it.
REPLACE = 0      # base = layer            (opaque paint; used for the wash)
ADD = 1          # base = min(255, base + layer*a)   (additive glow / shimmer)
MIX = 2          # base = base*(1-a) + layer*a       (alpha-over; convex, bounded)
MIX_LIT = 3      # like MIX but only where base is already lit (overlay on wash)
MIX_PREMULT = 4  # base = base*(1-a) + layer         (alpha-over, premultiplied
                 # colour: the layer buffer already holds colour*coverage — the
                 # scanner motion layer accumulates heads this way)


class Layer:
    """One composited element: an RGB buffer plus how it blends.

    Args:
        n_pixels: pixel count (buffer is ``n_pixels*3`` bytes).
        mode: one of REPLACE / ADD / MIX / MIX_LIT.
        per_pixel_alpha: if True, allocate a per-pixel coverage array
            (``alpha[i]`` in 0..1); otherwise the scalar ``opacity`` is used
            for every pixel.  Motion (soft head coverage) needs per-pixel;
            a flat overlay can use the scalar.

    The effective coverage at pixel ``i`` is ``alpha[i] * opacity`` when
    per-pixel, else ``opacity``.  REPLACE ignores alpha (it copies).
    """

    __slots__ = ("buf", "alpha", "opacity", "mode", "active", "_n")

    def __init__(self, n_pixels: int, mode: int = MIX,
                 per_pixel_alpha: bool = False):
        self._n = n_pixels
        self.buf = bytearray(n_pixels * 3)
        self.alpha = [0.0] * n_pixels if per_pixel_alpha else None
        self.opacity = 1.0
        self.mode = mode
        self.active = False

    def clear(self):
        """Zero the buffer (and per-pixel alpha).  Call before re-rendering."""
        b = self.buf
        for k in range(len(b)):
            b[k] = 0
        if self.alpha is not None:
            a = self.alpha
            for i in range(self._n):
                a[i] = 0.0


class Compositor:
    """Folds a fixed, ordered set of layers into a base buffer in place.

    The caller owns the base buffer (the wash) and the layers; the compositor
    only defines *how* they combine.  ``composite`` walks the given layers in
    order and blends each active one down onto ``base`` using its mode.
    """

    @staticmethod
    def composite(base: bytearray, layers) -> None:
        """Blend each active layer in ``layers`` onto ``base`` in order.

        ``base`` and every layer buffer must be the same length.  All work is
        in place on ``base``; no allocation.
        """
        n3 = len(base)
        for layer in layers:
            if not layer.active:
                continue
            lb = layer.buf
            mode = layer.mode
            op = layer.opacity
            alpha = layer.alpha

            if mode == REPLACE:
                # Opaque copy (opacity/alpha ignored — used for the wash base).
                base[:] = lb
                continue

            if mode == ADD:
                if alpha is None:
                    if op >= 1.0:
                        for k in range(n3):
                            v = base[k] + lb[k]
                            base[k] = v if v < 255 else 255
                    else:
                        for k in range(n3):
                            v = base[k] + int(lb[k] * op)
                            base[k] = v if v < 255 else 255
                else:
                    for i in range(len(alpha)):
                        a = alpha[i] * op
                        if a <= 0.0:
                            continue
                        o = i * 3
                        v = base[o] + int(lb[o] * a);     base[o]     = v if v < 255 else 255
                        v = base[o + 1] + int(lb[o + 1] * a); base[o + 1] = v if v < 255 else 255
                        v = base[o + 2] + int(lb[o + 2] * a); base[o + 2] = v if v < 255 else 255
                continue

            if mode == MIX_PREMULT:
                # Alpha-over with premultiplied colour: base*(1-a) + layer.
                # Coverage must be per-pixel (the motion layer's alpha array).
                for i in range(len(alpha)):
                    a = alpha[i] * op
                    if a <= 0.0:
                        continue
                    if a > 1.0:
                        a = 1.0
                    o = i * 3
                    inv = 1.0 - a
                    r = int(base[o] * inv) + lb[o]
                    g = int(base[o + 1] * inv) + lb[o + 1]
                    b = int(base[o + 2] * inv) + lb[o + 2]
                    base[o]     = r if r < 255 else 255
                    base[o + 1] = g if g < 255 else 255
                    base[o + 2] = b if b < 255 else 255
                continue

            # MIX / MIX_LIT: convex alpha-over.  base = base*(1-a) + layer*a.
            lit_only = (mode == MIX_LIT)
            if alpha is None:
                a = op
                if a <= 0.0:
                    continue
                inv = 1.0 - a
                for i in range(n3 // 3):
                    o = i * 3
                    if lit_only and not (base[o] | base[o + 1] | base[o + 2]):
                        continue
                    base[o]     = int(base[o] * inv + lb[o] * a)
                    base[o + 1] = int(base[o + 1] * inv + lb[o + 1] * a)
                    base[o + 2] = int(base[o + 2] * inv + lb[o + 2] * a)
            else:
                for i in range(len(alpha)):
                    a = alpha[i] * op
                    if a <= 0.0:
                        continue
                    if a > 1.0:
                        a = 1.0
                    o = i * 3
                    if lit_only and not (base[o] | base[o + 1] | base[o + 2]):
                        continue
                    inv = 1.0 - a
                    base[o]     = int(base[o] * inv + lb[o] * a)
                    base[o + 1] = int(base[o + 1] * inv + lb[o + 1] * a)
                    base[o + 2] = int(base[o + 2] * inv + lb[o + 2] * a)
