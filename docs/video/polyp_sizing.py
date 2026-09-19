"""Endoscopic polyp sizing with ToF 8x8 depth + RGB — ManimGL explainer.

Render (from this directory, so custom_config.yml is picked up):

    python -m manimlib polyp_sizing.py PolypSizing          # preview window
    python -m manimlib polyp_sizing.py PolypSizing -w -l    # low-quality mp4
    python -m manimlib polyp_sizing.py PolypSizing -w --hd  # final

Text only — no LaTeX on this machine, so formulas are Unicode ``Text``.
Every number on screen is computed below; see ``__main__`` for the checks.
"""
from __future__ import annotations

import math

from manimlib import *

# ---------------------------------------------------------------- numbers ---
# All figures trace back to docs/project_overview.html and the plan.
TOF_FOV_DIAG_DEG = 65.0
TOF_ZONES = 8
CAM_F_MM = 3.6
CAM_PIXEL_MM = 0.0022           # 2.2 um pitch, 1/2.5" 5MP

SIGMA_D_MM = 1.5                # example ToF range sigma (measured on bench later)
WORK_D_MM = 30.0                # endoscopic working distance used in examples
POLYP_DIAM_MM = 10.0
POLYP_H_MM = 3.0


def tof_side_fov_deg() -> float:
    """Square side FoV from the diagonal spec, in tangent space."""
    return 2 * math.degrees(math.atan(math.tan(math.radians(TOF_FOV_DIAG_DEG / 2)) / math.sqrt(2)))


def zone_mm(d_mm: float) -> float:
    """Footprint of one zone at distance d."""
    half = math.radians(tof_side_fov_deg() / TOF_ZONES / 2)
    return 2 * d_mm * math.tan(half)


def f_px() -> float:
    return CAM_F_MM / CAM_PIXEL_MM


def cap_volume(a: float, h: float) -> float:
    return math.pi * h / 6 * (3 * a * a + h * h)


def diameter_rel_err(sigma: float, d: float) -> float:
    return sigma / d


def height_rel_err(sigma: float, h: float) -> float:
    return math.sqrt(2) * sigma / h


# ---------------------------------------------------------------- palette ---
C_VIS = TEAL_C          # visible light / RGB camera
C_IR = GOLD_C           # 940 nm / ToF / depth
C_POLYP = PINK
C_LINE = GREY_B
C_WARN = RED_C

FS_TITLE = 34
FS_BODY = 28
FS_SMALL = 22


def txt(s: str, **kw) -> Text:
    kw.setdefault("font_size", FS_BODY)
    return Text(s, **kw)


class PolypSizing(Scene):
    def construct(self):
        self.opening()
        self.no_scale()
        self.one_distance()
        self.optics()
        self.what_sensor_gives()
        self.volume_model()
        self.accuracy()
        self.closing()

    # -- helpers -----------------------------------------------------------
    def set_title(self, s: str):
        new = txt(s, font_size=FS_TITLE).to_edge(UP, buff=0.35)
        self.play(Transform(self.title, new))

    def clear_beat(self, *keep):
        keep = {id(m) for k in (*keep, self.title) for m in k.get_family()}
        gone = [m for m in self.mobjects if id(m) not in keep]
        if gone:
            self.play(*[FadeOut(m) for m in gone], run_time=0.6)

    # -- 0. title ------------------------------------------------------------
    def opening(self):
        t1 = txt("Endoscopic Polyp Sizing", font_size=52)
        t2 = txt("ToF 8×8 depth  +  RGB camera", font_size=FS_BODY, color=GREY_B)
        t2.next_to(t1, DOWN, buff=0.4)
        self.play(Write(t1))
        self.play(FadeIn(t2, shift=0.3 * UP))
        self.wait(1.2)
        self.title = t1
        self.play(FadeOut(t2), self.title.animate.scale(34 / 52).to_edge(UP, buff=0.35))

    # -- 1. the problem ------------------------------------------------------
    def no_scale(self):
        self.set_title("1. No absolute scale")

        # endoscope view: circular frame + polyp blob
        frame = Circle(radius=2.2, stroke_color=C_LINE, stroke_width=3).shift(1.6 * LEFT)
        mucosa = Circle(radius=2.2, fill_color="#3A1F26", fill_opacity=1, stroke_width=0)
        mucosa.move_to(frame)
        polyp = Circle(radius=0.55, fill_color=C_POLYP, fill_opacity=0.9, stroke_width=0)
        polyp.move_to(frame).shift(0.3 * RIGHT + 0.2 * DOWN)
        self.play(FadeIn(mucosa), ShowCreation(frame), GrowFromCenter(polyp))

        # two readings that produce the same image
        a = VGroup(txt("5 mm at 20 mm", color=C_POLYP), txt("→ same pixels", font_size=FS_SMALL, color=GREY_B))
        b = VGroup(txt("10 mm at 40 mm", color=C_POLYP), txt("→ same pixels", font_size=FS_SMALL, color=GREY_B))
        for g in (a, b):
            g.arrange(DOWN, buff=0.12, aligned_edge=LEFT)
        col = VGroup(a, txt("?", font_size=44, color=C_WARN), b).arrange(DOWN, buff=0.5)
        col.next_to(frame, RIGHT, buff=1.0)
        self.play(FadeIn(a, shift=0.2 * LEFT))
        self.play(FadeIn(b, shift=0.2 * LEFT))
        self.play(FadeIn(col[1]), Indicate(polyp, scale_factor=1.2))
        self.wait(1.5)
        self.polyp_scene = VGroup(mucosa, frame, polyp)
        self.polyp = polyp
        self.clear_beat(self.polyp_scene)

    # -- 2. one distance ------------------------------------------------------
    def one_distance(self):
        self.set_title("2. One distance → scale")
        scene = self.polyp_scene
        polyp = self.polyp

        # ToF ray landing on the polyp
        src = scene.get_right() + 3.6 * RIGHT + 0.6 * UP
        ray = DashedLine(src, polyp.get_center() + 0.55 * RIGHT, color=C_IR, stroke_width=4)
        d_lab = txt(f"d = {WORK_D_MM:.0f} mm", color=C_IR).next_to(src, UP, buff=0.2)
        self.play(ShowCreation(ray), FadeIn(d_lab))

        fpx = f_px()
        scale = WORK_D_MM / fpx
        eq1 = txt(f"f_px = {CAM_F_MM} mm / {CAM_PIXEL_MM * 1000:.1f} µm = {fpx:.0f} px", font_size=FS_SMALL, color=GREY_B)
        eq2 = txt(f"mm/px = d / f_px = {scale:.3f}", font_size=FS_BODY)
        eqs = VGroup(eq1, eq2).arrange(DOWN, buff=0.3, aligned_edge=LEFT)
        eqs.next_to(scene, RIGHT, buff=0.9).shift(0.8 * DOWN)
        self.play(FadeIn(eq1, shift=0.2 * UP))
        self.play(FadeIn(eq2, shift=0.2 * UP))

        # measured diameter replaces the "?"
        px = POLYP_DIAM_MM / scale
        meas = txt(f"{POLYP_DIAM_MM:.0f} mm  ({px:.0f} px)", color=C_POLYP, font_size=FS_BODY)
        meas.next_to(polyp, DOWN, buff=0.35)
        bracket = Line(polyp.get_left(), polyp.get_right(), color=C_POLYP, stroke_width=3).next_to(polyp, DOWN, buff=0.1)
        self.play(ShowCreation(bracket), FadeIn(meas))
        self.play(FlashAround(meas, time_width=1.5, run_time=1.5))
        self.wait(1.2)
        self.clear_beat()

    # -- 3. optical layout ----------------------------------------------------
    def optics(self):
        self.set_title("3. Coaxial optics — beam splitter")

        # positions along the common axis (y = 0)
        cam = RoundedRectangle(width=1.8, height=0.9, corner_radius=0.1, stroke_color=C_LINE).shift(4.6 * LEFT)
        cam_lens = Rectangle(width=0.25, height=0.5, stroke_color=C_LINE).next_to(cam, RIGHT, buff=0)
        cam_l = txt("RGB", font_size=FS_SMALL, color=C_VIS).move_to(cam)

        bs = Square(side_length=1.3, stroke_color=C_LINE).shift(0.3 * LEFT)
        bs_diag = Line(bs.get_corner(DL), bs.get_corner(UR), color=C_LINE)
        bs_l = txt("50:50 BS", font_size=FS_SMALL).next_to(bs, DOWN, buff=0.25)

        tof = RoundedRectangle(width=1.8, height=0.7, corner_radius=0.1, stroke_color=C_LINE)
        tof.next_to(bs, UP, buff=1.1)
        tof_l = txt("ToF 940 nm", font_size=FS_SMALL, color=C_IR).move_to(tof)

        lens = VGroup(
            ArcBetweenPoints(0.6 * UP, 0.6 * DOWN, angle=-0.9),
            ArcBetweenPoints(0.6 * DOWN, 0.6 * UP, angle=-0.9),
        ).set_stroke(C_LINE, 3).shift(2.3 * RIGHT)
        lens_l = txt("4x zoom lens", font_size=FS_SMALL).next_to(lens, DOWN, buff=0.35)

        sample = Rectangle(width=0.22, height=1.6, fill_color=C_POLYP, fill_opacity=0.8, stroke_width=0)
        sample.shift(4.9 * RIGHT)
        sample_l = txt("polyp", font_size=FS_SMALL, color=C_POLYP).next_to(sample, UP, buff=0.2)

        parts = VGroup(cam, cam_lens, cam_l, bs, bs_diag, bs_l, tof, tof_l, lens, lens_l, sample, sample_l)
        self.play(LaggedStart(*[FadeIn(m) for m in parts], lag_ratio=0.08, run_time=2.2))

        # visible ray: sample -> BS -> camera (transmitted)
        vis = VGroup(
            Line(sample.get_left(), bs.get_right(), color=C_VIS, stroke_width=4),
            Line(bs.get_left(), cam_lens.get_right(), color=C_VIS, stroke_width=4),
        )
        vis_l = txt("visible — transmitted to RGB", font_size=FS_SMALL, color=C_VIS)
        self.play(ShowCreation(vis, lag_ratio=0.5), FadeIn(vis_l))

        # IR ray: ToF -> BS -> sample and back (reflected twice)
        ir_out = VGroup(
            DashedLine(tof.get_bottom(), bs.get_top(), color=C_IR, stroke_width=4),
            DashedLine(bs.get_center() + 0.12 * DOWN, sample.get_left() + 0.12 * DOWN, color=C_IR, stroke_width=4),
        )
        ir_l = txt("940 nm — reflected to polyp and back", font_size=FS_SMALL, color=C_IR)
        legend = VGroup(vis_l, ir_l).arrange(DOWN, buff=0.15, aligned_edge=LEFT)
        legend.next_to(cam, DOWN, buff=0.9, aligned_edge=LEFT)
        self.play(ShowCreation(ir_out, lag_ratio=0.5), FadeIn(ir_l))
        self.wait(0.6)

        # the payoff
        note = VGroup(
            txt("Same optical axis → parallax = 0", font_size=FS_BODY),
            txt("zone ↔ pixel = one homography, any distance", font_size=FS_SMALL, color=GREY_B),
        ).arrange(DOWN, buff=0.15).to_edge(DOWN, buff=0.45)
        self.play(FadeIn(note, shift=0.2 * UP))
        self.play(Indicate(lens, scale_factor=1.2), FlashAround(lens_l, run_time=1.5))
        self.wait(1.5)
        self.clear_beat()

    # -- 4. what the sensor gives ---------------------------------------------
    def what_sensor_gives(self):
        self.set_title("4. What 8×8 actually gives")

        side = 5.2
        cell = side / TOF_ZONES
        grid = VGroup(*[
            Square(side_length=cell, stroke_color=GREY_D, stroke_width=1.5)
            for _ in range(TOF_ZONES * TOF_ZONES)
        ]).arrange_in_grid(TOF_ZONES, TOF_ZONES, buff=0).shift(2.2 * LEFT)
        self.play(ShowCreation(grid, lag_ratio=0.01, run_time=1.5))

        z_mm = zone_mm(WORK_D_MM)
        zones_covered = POLYP_DIAM_MM / z_mm
        spec = VGroup(
            txt(f"{TOF_ZONES}×{TOF_ZONES} zones · {tof_side_fov_deg() / TOF_ZONES:.2f}° each"),
            txt(f"zone = {z_mm:.1f} mm at d = {WORK_D_MM:.0f} mm", color=C_IR),
            txt(f"{POLYP_DIAM_MM:.0f} mm polyp ≈ {zones_covered:.1f} zones", color=C_POLYP),
        ).arrange(DOWN, buff=0.35, aligned_edge=LEFT).next_to(grid, RIGHT, buff=0.9)
        self.play(FadeIn(spec[0]))
        self.play(FadeIn(spec[1]))

        # polyp drawn to scale on the grid
        r = zones_covered * cell / 2
        polyp = Circle(radius=r, fill_color=C_POLYP, fill_opacity=0.35, stroke_color=C_POLYP, stroke_width=3)
        polyp.move_to(grid)
        self.play(GrowFromCenter(polyp), FadeIn(spec[2]))
        self.wait(0.8)

        verdict = VGroup(
            txt("Not shape.", font_size=FS_BODY, color=C_WARN),
            txt("Scale + height.", font_size=FS_BODY, color=C_IR),
        ).arrange(RIGHT, buff=0.5).to_edge(DOWN, buff=0.5)
        self.play(FadeIn(verdict[0], shift=0.2 * UP))
        self.play(FadeIn(verdict[1], shift=0.2 * UP))
        self.wait(1.5)
        self.clear_beat()

    # -- 5. volume model ------------------------------------------------------
    def volume_model(self):
        self.set_title("5. Volume — a model, not a measurement")

        a_mm, h_mm = POLYP_DIAM_MM / 2, POLYP_H_MM
        u = 0.42                                   # scene units per mm
        a, h = a_mm * u, h_mm * u
        base_y = -0.9
        R = (a * a + h * h) / (2 * h)               # sphere radius of the cap
        center = np.array([-1.8, base_y - (R - h), 0])
        phi = math.atan2(a, R - h)

        mucosa = Line(6.0 * LEFT + base_y * UP, 1.0 * RIGHT + base_y * UP, color=C_LINE, stroke_width=3)
        cap = Arc(start_angle=PI / 2 - phi, angle=2 * phi, radius=R, arc_center=center,
                  stroke_color=C_POLYP, stroke_width=4)
        self.play(ShowCreation(mucosa), ShowCreation(cap))

        top = center + R * UP
        mid = np.array([center[0], base_y, 0])
        a_line = Line(mid, mid + a * RIGHT, color=C_VIS, stroke_width=4)
        h_line = Line(mid, top, color=C_IR, stroke_width=4)
        a_l = txt("a  ← RGB × scale", font_size=FS_SMALL, color=C_VIS).next_to(a_line, DOWN, buff=0.15)
        h_l = txt("h  ← d(mucosa) − d(polyp)", font_size=FS_SMALL, color=C_IR).next_to(top, UP, buff=0.25)
        self.play(ShowCreation(a_line), FadeIn(a_l))
        self.play(ShowCreation(h_line), FadeIn(h_l))

        v = cap_volume(a_mm, h_mm)
        eqs = VGroup(
            txt("Spherical cap (sessile, Paris Is / IIa)", font_size=FS_SMALL, color=GREY_B),
            txt("V = (π·h / 6) · (3a² + h²)", font_size=FS_BODY),
            txt(f"a = {a_mm:.0f} mm, h = {h_mm:.0f} mm  →  V ≈ {v:.0f} mm³", font_size=FS_SMALL, color=C_POLYP),
        ).arrange(DOWN, buff=0.3, aligned_edge=LEFT).to_edge(RIGHT, buff=0.6).shift(0.6 * UP)
        self.play(FadeIn(eqs, lag_ratio=0.3))
        self.wait(1.8)
        self.clear_beat()

    # -- 6. accuracy budget -----------------------------------------------------
    def accuracy(self):
        self.set_title("6. Accuracy budget")

        e_d = diameter_rel_err(SIGMA_D_MM, WORK_D_MM)
        e_h = height_rel_err(SIGMA_D_MM, POLYP_H_MM)
        assume = txt(f"σ_d = {SIGMA_D_MM} mm · d = {WORK_D_MM:.0f} mm · h = {POLYP_H_MM:.0f} mm",
                     font_size=FS_SMALL, color=GREY_B).next_to(self.title, DOWN, buff=0.5)
        self.play(FadeIn(assume))

        def row(label, formula, pct, color, width):
            lab = txt(label, color=color)
            frm = txt(formula, font_size=FS_SMALL, color=GREY_B)
            bar = Rectangle(width=width, height=0.45, fill_color=color, fill_opacity=0.8, stroke_width=0)
            pc = txt(f"≈ {pct * 100:.0f}%", color=color)
            left = VGroup(lab, frm).arrange(DOWN, buff=0.1, aligned_edge=LEFT)
            barg = VGroup(bar, pc).arrange(RIGHT, buff=0.3)
            return VGroup(left, barg)

        max_w = 5.0
        r1 = row("Diameter", "σ_d / d", e_d, C_VIS, max_w * e_d / e_h)
        r2 = row("Height", "√2 · σ_d / h", e_h, C_WARN, max_w)
        rows = VGroup(r1, r2)
        VGroup(r1[0], r2[0]).arrange(DOWN, buff=0.9, aligned_edge=LEFT)
        x_bar = max(r[0].get_right()[0] for r in rows) + 0.8   # one baseline for both bars
        for r in rows:
            r[1].align_to(np.array([x_bar, 0, 0]), LEFT).match_y(r[0])
        rows.move_to(ORIGIN).shift(0.2 * DOWN)
        self.play(FadeIn(r1[0]), GrowFromEdge(r1[1][0], LEFT), FadeIn(r1[1][1]))
        self.play(FadeIn(r2[0]), GrowFromEdge(r2[1][0], LEFT), FadeIn(r2[1][1]))
        self.wait(0.8)

        verdict = VGroup(
            txt("Primary output: diameter", color=C_VIS),
            txt("Secondary: volume ± σ", color=C_WARN),
        ).arrange(RIGHT, buff=0.9).to_edge(DOWN, buff=0.5)
        self.play(FadeIn(verdict, shift=0.2 * UP))
        self.wait(1.8)
        self.clear_beat()

    # -- 7. closing -----------------------------------------------------------------
    def closing(self):
        self.play(FadeOut(self.title))
        t1 = txt("Endoscopic Polyp Sizing", font_size=48)
        t2 = txt("one ToF distance gives the image its scale", font_size=FS_BODY, color=GREY_B)
        t3 = txt("github.com/do-gongil/vl53l8cx-rgb-fusion", font_size=FS_SMALL, color=C_VIS)
        g = VGroup(t1, t2, t3).arrange(DOWN, buff=0.4)
        self.play(FadeIn(g, lag_ratio=0.3))
        self.wait(2)


if __name__ == "__main__":
    # Checks that run without manimgl. Numbers must match docs/project_overview.html.
    assert abs(tof_side_fov_deg() - 48.5) < 0.2
    assert abs(zone_mm(30) - 3.18) < 0.05
    assert abs(zone_mm(20) - 2.12) < 0.05
    assert abs(f_px() - 1636) < 1
    assert abs(cap_volume(5, 3) - 132) < 1
    assert abs(diameter_rel_err(1.5, 30) - 0.05) < 1e-9
    assert abs(height_rel_err(1.5, 3) - 0.707) < 0.001
    print("ok")
