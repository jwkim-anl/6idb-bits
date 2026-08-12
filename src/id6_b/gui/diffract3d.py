"""3D model of the 4S+2D diffractometer.

Ported from ``~/jwkim/python/diffract/diffractometer_bluesky.py``.  The geometry
constants and the kinematic chain are kept exactly as written there -- they were
matched against the real instrument, so they are not something to re-derive.

Lab frame (Bluesky convention):
    +x  incident beam direction
    +y  horizontal, perpendicular to the beam
    +z  vertical up

``vtk`` and ``pyvista`` are imported lazily so this module -- and therefore the
GUI as a whole -- still imports when they are not installed.  :data:`AVAILABLE`
says whether the 3D view can actually be built.
"""

import logging

import numpy as np

logger = logging.getLogger(__name__)


def _probe():
    try:
        import pyvista  # noqa: F401
        import vtk  # noqa: F401
        from pyvistaqt import QtInteractor  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - absence is a normal condition
        return False, str(exc)
    return True, ""


#: Whether vtk/pyvista/pyvistaqt could be imported, and why not if they could not.
AVAILABLE, IMPORT_ERROR = _probe()

#: Install hint shown in place of the 3D view.
INSTALL_HINT = (
    "3D view unavailable — vtk, pyvista and pyvistaqt are not installed.\n\n"
    "On a networked machine:\n"
    "    pip download --no-deps -d ./pv3d vtk==9.3.1 pyvista==0.44.2 "
    "pyvistaqt==0.11.2 pooch scooby\n"
    "then copy pv3d/ across and run:\n"
    "    pip install --no-deps --no-index ./pv3d/*.whl"
)


# ---------- rotation helpers ----------


def Rx(t):  # noqa: N802 - matches the source module's naming
    """Rotation about x by *t* radians."""
    c, s = np.cos(t), np.sin(t)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def Ry(t):  # noqa: N802
    """Rotation about y by *t* radians."""
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def Rz(t):  # noqa: N802
    """Rotation about z by *t* radians."""
    c, s = np.cos(t), np.sin(t)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def to_mat4(R):  # noqa: N803
    """Embed a 3x3 rotation in a 4x4 homogeneous matrix."""
    M = np.eye(4)
    M[:3, :3] = R
    return M


def trans4(dx, dy, dz):
    """4x4 translation matrix."""
    T = np.eye(4)
    T[0, 3] = dx
    T[1, 3] = dy
    T[2, 3] = dz
    return T


def rot_about4(axis, theta, pivot):
    """4x4 rotation about *axis* by *theta* radians around the point *pivot*."""
    if axis == "x":
        R = to_mat4(Rx(theta))
    elif axis == "y":
        R = to_mat4(Ry(theta))
    elif axis == "z":
        R = to_mat4(Rz(theta))
    else:
        raise ValueError(axis)
    cx, cy, cz = pivot
    return trans4(cx, cy, cz) @ R @ trans4(-cx, -cy, -cz)


def vtk_matrix(M):  # noqa: N803
    """Convert a 4x4 numpy matrix to a ``vtkMatrix4x4``."""
    import vtk

    m = vtk.vtkMatrix4x4()
    for i in range(4):
        for j in range(4):
            m.SetElement(i, j, float(M[i, j]))
    return m


def rotation_to_align(v1, v2):
    """3x3 rotation matrix that maps unit vector *v1* onto unit vector *v2*."""
    v1 = v1 / np.linalg.norm(v1)
    v2 = v2 / np.linalg.norm(v2)
    cross = np.cross(v1, v2)
    dot = float(np.dot(v1, v2))
    c_norm = float(np.linalg.norm(cross))
    if c_norm < 1e-9:
        if dot > 0:
            return np.eye(3)
        perp = (
            np.array([1.0, 0.0, 0.0]) if abs(v1[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        )
        axis = np.cross(v1, perp)
        axis /= np.linalg.norm(axis)
        return 2.0 * np.outer(axis, axis) - np.eye(3)
    axis = cross / c_norm
    K = np.array(
        [
            [0.0, -axis[2], axis[1]],
            [axis[2], 0.0, -axis[0]],
            [-axis[1], axis[0], 0.0],
        ]
    )
    return np.eye(3) + c_norm * K + (1.0 - dot) * (K @ K)


# ---------- geometry ----------


class Diffractometer:
    """The 4S+2D model: meshes, kinematic chain and the three overlays."""

    # base + column dimensions
    BASE_RADIUS = 3.0
    BASE_TOP_Z = -2.3
    BASE_BOTTOM_Z = -2.9
    COLUMN_HALF_X = 0.5
    COLUMN_HALF_Y = 0.05
    COLUMN_Y = -2.3
    COLUMN_TOP_Z = 2.6

    # mu (sample) base + support, on top of the nu base
    MU_BASE_RADIUS = 2.0
    MU_BASE_BOTTOM_Z = -2.3
    MU_BASE_TOP_Z = -1.9
    MU_COL_HALF_X = 0.35
    MU_COL_HALF_Y = 0.035
    MU_COL_TOP_Z = 2.6
    MU_COL_Y = -MU_BASE_RADIUS + MU_COL_HALF_Y

    # chi cradle: hollow cylinder, axis along x, shifted upstream
    CHI_R_OUT = abs(MU_COL_Y)
    CHI_R_IN = CHI_R_OUT - 0.40
    CHI_H = 2 * MU_COL_HALF_X
    CHI_CX = -CHI_H

    # eta arm plate
    ETA_WIDTH = 2 * MU_COL_HALF_X
    ETA_LENGTH = CHI_H
    ETA_DOWN_LENGTH = ETA_LENGTH
    ETA_HALF_Z = ETA_WIDTH / 2
    ETA_HALF_Y = MU_COL_HALF_Y

    # phi mount cylinder, axis along y
    PHI_R = 0.36
    PHI_SAMPLE_GAP = 0.10

    SAMPLE_HALF = 0.12

    #: Beam passes through (0, 0, BEAM_Z) -- the rotation centre height.
    BEAM_Z = 2.0

    # detector arm
    ARM_OUT_X = 4.0
    ARM_HALF_Y = 0.05
    ARM_HALF_Z = 0.5

    def __init__(self):
        """Start at all-zero angles with an identity UB."""
        self.angles = dict(mu=0.0, eta=0.0, chi=0.0, phi=0.0, delta=0.0, nu=0.0)
        self.actors = []
        self.scatter_plane_actor = None
        self.scatter_plane_visible = False
        self.ref_vec_actor = None
        self.ref_vec_visible = False
        self.q_vec_actor = None
        self.q_vec_visible = False
        self.UB = np.eye(3)
        self.hkl = np.array([1.0, 0.0, 0.0])

    def _add(self, plotter, mesh, chain, **kwargs):
        actor = plotter.add_mesh(mesh, **kwargs)
        self.actors.append((actor, chain))
        return actor

    def build(self, plotter):
        """Create every mesh in *plotter* and apply the initial transforms."""
        import pyvista as pv

        # ---- static lab-frame parts ----
        plate = pv.Box(bounds=(-4.5, 4.5, -4.5, 4.5, -3.25, -3.0))
        plotter.add_mesh(plate, color="#3a3a3a", name="plate")

        L_in = 6.6
        beam_in = pv.Arrow(
            start=(-7.0, 0, self.BEAM_Z),
            direction=(1, 0, 0),
            tip_length=0.32 / L_in,
            tip_radius=0.11 / L_in,
            shaft_radius=0.04 / L_in,
            scale=L_in,
        )
        plotter.add_mesh(beam_in, color="#ffcc00", name="beam_in")

        # ---- nu stage: blue round base ----
        base = pv.Cylinder(
            center=(0, 0, (self.BASE_TOP_Z + self.BASE_BOTTOM_Z) / 2),
            direction=(0, 0, 1),
            radius=self.BASE_RADIUS,
            height=self.BASE_TOP_Z - self.BASE_BOTTOM_Z,
            resolution=128,
        )
        self._add(plotter, base, ["nu"], color="#1f4e88", smooth_shading=True)

        nu_stripe = pv.Box(
            bounds=(
                -self.BASE_RADIUS,
                -self.BASE_RADIUS + 0.9,
                -0.12,
                0.12,
                self.BASE_TOP_Z - 0.02,
                self.BASE_TOP_Z + 0.002,
            )
        )
        self._add(plotter, nu_stripe, ["nu"], color="#ffcc00")

        col = pv.Box(
            bounds=(
                -self.COLUMN_HALF_X,
                self.COLUMN_HALF_X,
                self.COLUMN_Y - self.COLUMN_HALF_Y,
                self.COLUMN_Y + self.COLUMN_HALF_Y,
                self.BASE_TOP_Z,
                self.COLUMN_TOP_Z,
            )
        )
        self._add(plotter, col, ["nu"], color="#1f4e88", smooth_shading=True)

        # ---- mu stage: green disk + column ----
        mu_base = pv.Cylinder(
            center=(0, 0, (self.MU_BASE_TOP_Z + self.MU_BASE_BOTTOM_Z) / 2),
            direction=(0, 0, 1),
            radius=self.MU_BASE_RADIUS,
            height=self.MU_BASE_TOP_Z - self.MU_BASE_BOTTOM_Z,
            resolution=128,
        )
        self._add(plotter, mu_base, ["mu"], color="#1f8048", smooth_shading=True)

        mu_stripe = pv.Box(
            bounds=(
                -self.MU_BASE_RADIUS,
                -self.MU_BASE_RADIUS + 0.7,
                -0.10,
                0.10,
                self.MU_BASE_TOP_Z - 0.02,
                self.MU_BASE_TOP_Z + 0.002,
            )
        )
        self._add(plotter, mu_stripe, ["mu"], color="#ffcc00")

        mu_col = pv.Box(
            bounds=(
                -self.MU_COL_HALF_X,
                self.MU_COL_HALF_X,
                self.MU_COL_Y - self.MU_COL_HALF_Y,
                self.MU_COL_Y + self.MU_COL_HALF_Y,
                self.MU_BASE_TOP_Z,
                self.MU_COL_TOP_Z,
            )
        )
        self._add(plotter, mu_col, ["mu"], color="#1f8048", smooth_shading=True)

        # ---- eta arm plate ----
        eta_upstream = -self.MU_COL_HALF_X - self.ETA_LENGTH
        eta_downstream = -self.MU_COL_HALF_X + self.ETA_DOWN_LENGTH
        eta_arm = pv.Box(
            bounds=(
                eta_upstream,
                eta_downstream,
                self.MU_COL_Y - self.ETA_HALF_Y,
                self.MU_COL_Y + self.ETA_HALF_Y,
                self.BEAM_Z - self.ETA_HALF_Z,
                self.BEAM_Z + self.ETA_HALF_Z,
            )
        )
        self._add(plotter, eta_arm, ["mu", "eta"], color="#1f8048", smooth_shading=True)

        # ---- chi cradle ----
        chi_cx, chi_cy, chi_cz = self.CHI_CX, 0.0, self.BEAM_Z
        R_out, R_in, H = self.CHI_R_OUT, self.CHI_R_IN, self.CHI_H
        outer = pv.Cylinder(
            center=(chi_cx, chi_cy, chi_cz),
            direction=(1, 0, 0),
            radius=R_out,
            height=H,
            capping=False,
            resolution=96,
        )
        inner = pv.Cylinder(
            center=(chi_cx, chi_cy, chi_cz),
            direction=(1, 0, 0),
            radius=R_in,
            height=H,
            capping=False,
            resolution=96,
        )
        cap_a = pv.Disc(
            center=(chi_cx + H / 2, chi_cy, chi_cz),
            inner=R_in,
            outer=R_out,
            normal=(1, 0, 0),
            r_res=1,
            c_res=96,
        )
        cap_b = pv.Disc(
            center=(chi_cx - H / 2, chi_cy, chi_cz),
            inner=R_in,
            outer=R_out,
            normal=(1, 0, 0),
            r_res=1,
            c_res=96,
        )
        chi_cradle = outer + inner + cap_a + cap_b
        self._add(
            plotter,
            chi_cradle,
            ["mu", "eta", "chi"],
            color="#87ceeb",
            smooth_shading=True,
        )

        # ---- phi mount ----
        phi_y1 = -self.PHI_SAMPLE_GAP
        phi_y0 = -self.CHI_R_OUT
        phi_center_y = (phi_y0 + phi_y1) / 2
        phi_mount = pv.Cylinder(
            center=(0.0, phi_center_y, self.BEAM_Z),
            direction=(0, 1, 0),
            radius=self.PHI_R,
            height=phi_y1 - phi_y0,
            resolution=64,
        )
        self._add(
            plotter,
            phi_mount,
            ["mu", "eta", "chi", "phi"],
            color="#e6c021",
            smooth_shading=True,
        )

        phi_tab = pv.Box(
            bounds=(
                self.PHI_R,
                self.PHI_R + 0.18,
                phi_center_y - 0.08,
                phi_center_y + 0.08,
                self.BEAM_Z - 0.08,
                self.BEAM_Z + 0.08,
            )
        )
        self._add(
            plotter,
            phi_tab,
            ["mu", "eta", "chi", "phi"],
            color="#c40b0b",
            smooth_shading=True,
        )

        sample = pv.Cube(
            center=(0, 0, self.BEAM_Z),
            x_length=2 * self.SAMPLE_HALF,
            y_length=2 * self.SAMPLE_HALF,
            z_length=2 * self.SAMPLE_HALF,
        )
        self._add(
            plotter,
            sample,
            ["mu", "eta", "chi", "phi"],
            color="#c40b0b",
            smooth_shading=True,
        )

        # ---- detector arm ----
        plate_x0 = self.COLUMN_HALF_X
        plate_x1 = plate_x0 + self.ARM_OUT_X
        arm = pv.Box(
            bounds=(
                plate_x0,
                plate_x1,
                self.COLUMN_Y - self.ARM_HALF_Y,
                self.COLUMN_Y + self.ARM_HALF_Y,
                self.BEAM_Z - self.ARM_HALF_Z,
                self.BEAM_Z + self.ARM_HALF_Z,
            )
        )
        self._add(plotter, arm, ["nu", "delta"], color="#b8252e", smooth_shading=True)

        bracket = pv.Box(
            bounds=(
                plate_x1 - 2 * self.ARM_HALF_Y,
                plate_x1,
                self.COLUMN_Y,
                0.0,
                self.BEAM_Z - self.ARM_HALF_Z,
                self.BEAM_Z + self.ARM_HALF_Z,
            )
        )
        self._add(
            plotter, bracket, ["nu", "delta"], color="#b8252e", smooth_shading=True
        )

        DH = 0.45
        det_x = plate_x1 - self.ARM_HALF_Y
        det_y = 0.0
        detector = pv.Box(
            bounds=(
                det_x - DH,
                det_x + DH,
                det_y - DH,
                det_y + DH,
                self.BEAM_Z - DH,
                self.BEAM_Z + DH,
            )
        )
        self._add(
            plotter, detector, ["nu", "delta"], color="#3a3a3a", smooth_shading=True
        )

        det_face = pv.Plane(
            center=(det_x - DH - 0.01, det_y, self.BEAM_Z),
            direction=(-1, 0, 0),
            i_size=2 * DH * 0.95,
            j_size=2 * DH * 0.95,
        )
        self._add(plotter, det_face, ["nu", "delta"], color="#ffe080")

        sample_pt = np.array([0.0, 0.0, self.BEAM_Z])
        det_pt = np.array([det_x, det_y, self.BEAM_Z])
        v = det_pt - sample_pt
        L = float(np.linalg.norm(v))
        sbeam = pv.Cylinder(
            center=tuple((sample_pt + det_pt) / 2),
            direction=tuple(v / L),
            radius=0.05,
            height=L,
        )
        self._add(plotter, sbeam, ["nu", "delta"], color="#ff8800")

        # ---- overlays, hidden until toggled ----
        sp_mesh = pv.Plane(
            center=(0, 0, 0),
            direction=(0, 0, 1),
            i_size=8,
            j_size=8,
            i_resolution=1,
            j_resolution=1,
        )
        self.scatter_plane_actor = plotter.add_mesh(
            sp_mesh,
            color="#00cfff",
            opacity=0.3,
            show_edges=False,
            lighting=False,
            name="scatter_plane",
        )
        self.scatter_plane_actor.visibility = False

        rv_mesh = pv.Arrow(
            start=(0, 0, 0),
            direction=(1, 0, 0),
            tip_length=0.25,
            tip_radius=0.08,
            shaft_radius=0.04,
            scale=2.0,
        )
        self.ref_vec_actor = plotter.add_mesh(rv_mesh, color="#ff00cc", name="ref_vec")
        self.ref_vec_actor.visibility = False

        qv_mesh = pv.Arrow(
            start=(0, 0, 0),
            direction=(1, 0, 0),
            tip_length=0.25,
            tip_radius=0.08,
            shaft_radius=0.04,
            scale=2.0,
        )
        self.q_vec_actor = plotter.add_mesh(qv_mesh, color="#00ff88", name="q_vec")
        self.q_vec_actor.visibility = False

        self.update_transforms()

    # ---- overlays ----

    def update_scatter_plane(self):
        """Orient the scattering-plane disc from delta and nu."""
        if self.scatter_plane_actor is None:
            return
        a = self.angles
        d = np.radians(a["delta"])
        nu = np.radians(a["nu"])
        # ki = (1,0,0), kf = (cos d cos nu, cos d sin nu, sin d); n = ki x kf
        n = np.array([0.0, -np.sin(d), np.cos(d) * np.sin(nu)])
        norm = float(np.linalg.norm(n))
        if norm < 1e-6 or not self.scatter_plane_visible:
            self.scatter_plane_actor.visibility = False
            return
        self.scatter_plane_actor.visibility = True
        n /= norm
        R = rotation_to_align(np.array([0.0, 0.0, 1.0]), n)
        M = trans4(0, 0, self.BEAM_Z) @ to_mat4(R)
        self.scatter_plane_actor.user_matrix = vtk_matrix(M)

    def update_ref_vec(self):
        """Point the reference arrow along ``UB @ hkl``, carried by the sample chain."""
        if self.ref_vec_actor is None:
            return
        if not self.ref_vec_visible:
            self.ref_vec_actor.visibility = False
            return
        Q = self.UB @ self.hkl
        Q_norm = float(np.linalg.norm(Q))
        if Q_norm < 1e-9:
            self.ref_vec_actor.visibility = False
            return
        self.ref_vec_actor.visibility = True
        R_Q = rotation_to_align(np.array([1.0, 0.0, 0.0]), Q / Q_norm)
        a = {k: np.radians(v) for k, v in self.angles.items()}
        sample = (0, 0, self.BEAM_Z)
        S = (
            rot_about4("z", a["mu"], sample)
            @ rot_about4("y", -a["eta"], sample)
            @ rot_about4("x", a["chi"], sample)
            @ rot_about4("y", -a["phi"], sample)
        )
        M = S @ trans4(0, 0, self.BEAM_Z) @ to_mat4(R_Q)
        self.ref_vec_actor.user_matrix = vtk_matrix(M)

    def update_q_vec(self):
        """Point the Q arrow along kf - ki."""
        if self.q_vec_actor is None:
            return
        if not self.q_vec_visible:
            self.q_vec_actor.visibility = False
            return
        a = self.angles
        d = np.radians(a["delta"])
        nu = np.radians(a["nu"])
        kf = np.array([np.cos(d) * np.cos(nu), np.cos(d) * np.sin(nu), np.sin(d)])
        Q = kf - np.array([1.0, 0.0, 0.0])
        Q_norm = float(np.linalg.norm(Q))
        if Q_norm < 1e-9:
            self.q_vec_actor.visibility = False
            return
        self.q_vec_actor.visibility = True
        R_Q = rotation_to_align(np.array([1.0, 0.0, 0.0]), Q / Q_norm)
        self.q_vec_actor.user_matrix = vtk_matrix(
            trans4(0, 0, self.BEAM_Z) @ to_mat4(R_Q)
        )

    def chain_matrix(self, chain):
        """Return the 4x4 transform for a kinematic *chain* of joint names."""
        a = {k: np.radians(v) for k, v in self.angles.items()}
        sample = (0, 0, self.BEAM_Z)
        column = (0, self.COLUMN_Y, self.BEAM_Z)

        def rot_for(joint):
            if joint == "mu":
                return rot_about4("z", a["mu"], sample)
            if joint == "eta":
                return rot_about4("y", -a["eta"], sample)
            if joint == "chi":
                return rot_about4("x", a["chi"], sample)
            if joint == "phi":
                return rot_about4("y", -a["phi"], sample)
            if joint == "nu":
                return rot_about4("z", a["nu"], (0, 0, 0))
            # delta pivots about horizontal -y through the column
            if joint == "delta":
                return rot_about4("y", -a["delta"], column)
            raise ValueError(joint)

        M = np.eye(4)
        for joint in chain:
            M = M @ rot_for(joint)
        return M

    def update_transforms(self):
        """Re-apply every actor's transform from the current angles."""
        for actor, chain in self.actors:
            actor.user_matrix = vtk_matrix(self.chain_matrix(chain))
        self.update_scatter_plane()
        self.update_ref_vec()
        self.update_q_vec()
