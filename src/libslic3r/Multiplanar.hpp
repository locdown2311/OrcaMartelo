#pragma once

// Multiplanar printing, virtual mode: the machine-geometry limits.
//
// An object split by cut planes (CutPlaneSegmentation.hpp) prints each segment in its own tilted
// layers. On an ordinary 3-axis printer the nozzle stays upright while it does, so a tilted segment
// is simply a sloped surface under a vertical tool, and how far it may lean is bounded by the hotend
// and the gantry. This file is the single definition of that bound: Print::validate() refuses a
// slice past it and the cut-plane gizmo stops a drag at it, and the two must never disagree.
//
// Ported from Unlayered Slicer (https://github.com/Unlayered3D/Unlayered-Slicer, AGPL-3.0).

#include <array>
#include <optional>

namespace Slic3r {

class ConfigBase;

// The hotend's collision envelope: a four-sided pyramid with its apex on the nozzle tip and its axis
// along the nozzle, each face at its own angle, because a real toolhead is not round -- the fan duct,
// the heater block and the probe stick out further on some sides than on others. Angles are measured
// UP FROM THE BED PLANE with the head vertical: 90 is a face flush with the nozzle, smaller is a
// fatter side. At a height h up the nozzle axis, face s stands spread(s) * h out from it.
struct HotendEnvelope
{
    enum Side { XPos = 0, XNeg, YPos, YNeg };

    double                height = 40.;                    // mm up the nozzle axis; <= 0 = no model
    std::array<double, 4> angle  = { 45., 45., 45., 45. }; // degrees from the bed plane, by Side

    bool   enabled() const { return height > 0.; }
    // Lateral reach per mm of height, 1/tan(angle): 1 at 45, 0 at 90.
    double spread(Side s) const;

    static HotendEnvelope from_config(const ConfigBase &config);
    static const char    *option_key(Side s);   // "nonplanar_hotend_angle_xpos", ...
    static const char    *side_name(Side s);    // "+X", "-X", "+Y", "-Y"
};

// How steep a segment may be built, from the machine's own geometry. Two things can hit a tilted
// segment printed with an upright nozzle, and the limit is whichever bites first:
//
//   The hotend. A segment whose build normal leans toward azimuth phi has its layer plane climbing
//   toward d = -(cos phi, sin phi): the material it has already laid stands up to r*tan(beta) above
//   the tip at a distance r that way. At a height h the envelope reaches h*(|d.x|*sx + |d.y|*sy)
//   along d, where sx, sy are the spreads of the X and Y faces that look uphill. The plane stays
//   clear of it while
//
//       beta <= atan(1 / (|d.x|*sx + |d.y|*sy))
//
//   which, tilting straight at one face, is exactly that face's angle from the bed. Between two faces
//   the envelope's corner leads, so at four equal 45-degree faces a diagonal tilt stops at 35.3
//   degrees, not 45.
//
//   The gantry. It sits `gantry_height_mm` above the nozzle tip. While a tilted layer prints, the
//   far end of that layer stands reach*tan(beta) above the nozzle, and that has to fit underneath:
//
//       beta <= atan(gantry_height_mm / reach_mm)
//
// `reach_mm` is the segment's horizontal extent (CutSegmentDescriptor::build_reach_max); <= 0 means
// it is not known and only the hotend term applies. `tilt_azimuth_rad` is the direction the build
// normal leans in (atan2 of its XY); absent means "not known", and yields the worst direction.
//
// `gantry_height_mm` <= 0 means the gantry was never measured, so its term is skipped. The hotend
// term applies whenever the envelope is on. Returns 0 = NO LIMIT only when both are off.
double virtual_tilt_limit_deg(const HotendEnvelope &envelope, double gantry_height_mm, double reach_mm,
                              std::optional<double> tilt_azimuth_rad = std::nullopt);

// The hotend term of virtual_tilt_limit_deg() alone (90 when the envelope is off), and the face that
// dominates it -- the one to name when it is what binds. Same meaning of an absent azimuth.
double               virtual_tilt_hotend_term_deg(const HotendEnvelope &envelope, std::optional<double> tilt_azimuth_rad);
HotendEnvelope::Side virtual_tilt_binding_face(const HotendEnvelope &envelope, std::optional<double> tilt_azimuth_rad);

} // namespace Slic3r
