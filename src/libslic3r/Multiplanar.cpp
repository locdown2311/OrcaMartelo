#include "Multiplanar.hpp"

#include "Config.hpp"
#include "libslic3r.h"

#include <algorithm>
#include <cmath>

namespace Slic3r {

double HotendEnvelope::spread(Side s) const
{
    // cos/sin rather than 1/tan so that 90 is a clean zero instead of 1/1.6e16.
    const double a = std::clamp(angle[size_t(s)], 1., 90.) * PI / 180.;
    return std::max(0., std::cos(a) / std::sin(a));
}

const char *HotendEnvelope::option_key(Side s)
{
    static const char *keys[4] = { "nonplanar_hotend_angle_xpos", "nonplanar_hotend_angle_xneg",
                                   "nonplanar_hotend_angle_ypos", "nonplanar_hotend_angle_yneg" };
    return keys[size_t(s)];
}

const char *HotendEnvelope::side_name(Side s)
{
    static const char *names[4] = { "+X", "-X", "+Y", "-Y" };
    return names[size_t(s)];
}

HotendEnvelope HotendEnvelope::from_config(const ConfigBase &config)
{
    HotendEnvelope env;
    auto get = [&config](const char *key, double fallback) {
        const auto *opt = config.option<ConfigOptionFloat>(key);
        return opt == nullptr ? fallback : opt->value;
    };
    env.height = get("nonplanar_hotend_cone_height", env.height);
    for (int s = 0; s < 4; ++ s)
        env.angle[s] = get(option_key(Side(s)), env.angle[s]);
    return env;
}

namespace {
// The spreads of the X and Y faces looking uphill for a build normal leaning toward `azimuth`, each
// weighted by how squarely it faces uphill. The layer climbs AWAY from the lean, toward -azimuth.
struct UphillFaces { HotendEnvelope::Side fx, fy; double wx, wy; };
UphillFaces uphill_faces(double azimuth)
{
    const double dx = -std::cos(azimuth), dy = -std::sin(azimuth);
    return { dx > 0. ? HotendEnvelope::XPos : HotendEnvelope::XNeg,
             dy > 0. ? HotendEnvelope::YPos : HotendEnvelope::YNeg,
             std::abs(dx), std::abs(dy) };
}
} // namespace

double virtual_tilt_hotend_term_deg(const HotendEnvelope &envelope, std::optional<double> tilt_azimuth_rad)
{
    if (! envelope.enabled())
        return 90.;
    double reach = 0.;   // lateral reach of the envelope along the uphill direction, per mm of height
    if (tilt_azimuth_rad) {
        const UphillFaces f = uphill_faces(*tilt_azimuth_rad);
        reach = f.wx * envelope.spread(f.fx) + f.wy * envelope.spread(f.fy);
    } else {
        // Worst direction: within each quadrant |dx|*sx + |dy|*sy peaks at hypot(sx, sy), toward the corner.
        for (HotendEnvelope::Side fx : { HotendEnvelope::XPos, HotendEnvelope::XNeg })
            for (HotendEnvelope::Side fy : { HotendEnvelope::YPos, HotendEnvelope::YNeg })
                reach = std::max(reach, std::hypot(envelope.spread(fx), envelope.spread(fy)));
    }
    return std::atan2(1., reach) * 180. / PI;
}

HotendEnvelope::Side virtual_tilt_binding_face(const HotendEnvelope &envelope, std::optional<double> tilt_azimuth_rad)
{
    if (tilt_azimuth_rad) {
        const UphillFaces f = uphill_faces(*tilt_azimuth_rad);
        return f.wx * envelope.spread(f.fx) >= f.wy * envelope.spread(f.fy) ? f.fx : f.fy;
    }
    // Unknown direction: the fattest face.
    HotendEnvelope::Side worst = HotendEnvelope::XPos;
    for (int s = 1; s < 4; ++ s)
        if (envelope.spread(HotendEnvelope::Side(s)) > envelope.spread(worst))
            worst = HotendEnvelope::Side(s);
    return worst;
}

double virtual_tilt_limit_deg(const HotendEnvelope &envelope, double gantry_height_mm, double reach_mm,
                              std::optional<double> tilt_azimuth_rad)
{
    // Neither the hotend nor the gantry is modelled: no limit at all. A gantry height of 0 only means
    // the gantry was never measured; the hotend term below does not depend on it.
    const bool gantry_known = gantry_height_mm > 0.;
    if (! gantry_known && ! envelope.enabled())
        return 0.;

    // The hotend envelope, toward whichever side the layer climbs (90 when it is switched off).
    double limit = virtual_tilt_hotend_term_deg(envelope, tilt_azimuth_rad);

    // The gantry, once we know how high it is and how far the segment reaches.
    if (gantry_known && reach_mm > 0.)
        limit = std::min(limit, std::atan2(gantry_height_mm, reach_mm) * 180. / PI);

    // A limit of zero would mean "no limit" to every caller, so a geometry this hostile has to
    // report the smallest angle that still reads as a limit rather than silently opening up.
    return std::max(limit, 0.01);
}

} // namespace Slic3r
