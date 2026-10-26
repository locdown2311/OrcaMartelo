#include "WipeTowerPlacement.hpp"
#include "libslic3r.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <vector>

namespace Slic3r {

namespace {

enum class Side { Right, Left, Back, Front };
// Where the tower sits along the side it is on: at its middle, or at its end toward the wiper.
enum class Along { Middle, RightEnd, LeftEnd, BackEnd, FrontEnd };

struct Candidate
{
    Side  side;
    Along along;
};

// Candidates, best first, for each wiper position.
std::vector<Candidate> candidates(const WipeTowerPlacementInput &in)
{
    switch (in.wiper) {
    case NozzleWiperPosition::Right:
        return { { Side::Right, Along::Middle }, { Side::Back, Along::RightEnd }, { Side::Front, Along::RightEnd }, { Side::Left, Along::Middle } };
    case NozzleWiperPosition::Left:
        return { { Side::Left, Along::Middle }, { Side::Back, Along::LeftEnd }, { Side::Front, Along::LeftEnd }, { Side::Right, Along::Middle } };
    case NozzleWiperPosition::RearRight:
        return { { Side::Back, Along::RightEnd }, { Side::Right, Along::BackEnd }, { Side::Back, Along::Middle }, { Side::Left, Along::BackEnd } };
    case NozzleWiperPosition::RearLeft:
        return { { Side::Back, Along::LeftEnd }, { Side::Left, Along::BackEnd }, { Side::Back, Along::Middle }, { Side::Right, Along::BackEnd } };
    case NozzleWiperPosition::None:
    default: {
        // The side the tower is on now, judged by how far its centre lies past the parts' edges.
        const Vec2d centre = in.current + 0.5 * in.tower_size;
        const std::array<std::pair<double, Side>, 4> past = { {
            { centre.x() - in.parts.max.x(), Side::Right },
            { in.parts.min.x() - centre.x(), Side::Left },
            { centre.y() - in.parts.max.y(), Side::Back },
            { in.parts.min.y() - centre.y(), Side::Front },
        } };
        std::vector<std::pair<double, Side>> order(past.begin(), past.end());
        std::stable_sort(order.begin(), order.end(), [](const auto &a, const auto &b) { return a.first > b.first; });
        std::vector<Candidate> out;
        for (const auto &[distance, side] : order)
            out.push_back({ side, Along::Middle });
        return out;
    }
    }
}

} // namespace

std::optional<Vec2d> place_wipe_tower_beside_parts(const WipeTowerPlacementInput &in)
{
    if (! in.parts.defined || ! in.bed.defined)
        return std::nullopt;
    const double w = in.tower_size.x(), d = in.tower_size.y();
    const double clearance = in.gap + in.tower_brim;
    // The tower's body must stay this far inside the bed, to leave room for its brim.
    const double x_lo = in.bed.min.x() + in.tower_brim, x_hi = in.bed.max.x() - in.tower_brim - w;
    const double y_lo = in.bed.min.y() + in.tower_brim, y_hi = in.bed.max.y() - in.tower_brim - d;
    if (x_hi < x_lo || y_hi < y_lo)
        return std::nullopt;

    // Along a side in Y: centred on the parts, or on a tall part with a bed slinger, as near the
    // bed's centre as the parts' extent allows.
    auto middle_y = [&]() {
        const double centre = 0.5 * (in.parts.min.y() + in.parts.max.y());
        if (in.bed_slinger && in.parts_height > 40. && in.parts.size().y() > d) {
            const double bed_centre = 0.5 * (in.bed.min.y() + in.bed.max.y());
            return std::clamp(bed_centre, in.parts.min.y() + 0.5 * d, in.parts.max.y() - 0.5 * d) - 0.5 * d;
        }
        return centre - 0.5 * d;
    };
    auto middle_x = [&]() { return 0.5 * (in.parts.min.x() + in.parts.max.x()) - 0.5 * w; };

    for (const Candidate &c : candidates(in)) {
        Vec2d pos;
        switch (c.side) {
        case Side::Right: pos.x() = in.parts.max.x() + clearance;     break;
        case Side::Left:  pos.x() = in.parts.min.x() - clearance - w; break;
        case Side::Back:  pos.y() = in.parts.max.y() + clearance;     break;
        case Side::Front: pos.y() = in.parts.min.y() - clearance - d; break;
        }
        const bool across_x = c.side == Side::Right || c.side == Side::Left;
        if (across_x) {
            pos.y() = c.along == Along::BackEnd  ? in.parts.max.y() - d :
                      c.along == Along::FrontEnd ? in.parts.min.y() :
                                                   middle_y();
            if (pos.x() < x_lo - EPSILON || pos.x() > x_hi + EPSILON)
                continue; // no room on this side
            pos.y() = std::clamp(pos.y(), y_lo, y_hi);
        } else {
            pos.x() = c.along == Along::RightEnd ? in.parts.max.x() - w :
                      c.along == Along::LeftEnd  ? in.parts.min.x() :
                                                   middle_x();
            if (pos.y() < y_lo - EPSILON || pos.y() > y_hi + EPSILON)
                continue;
            pos.x() = std::clamp(pos.x(), x_lo, x_hi);
        }
        return pos;
    }
    return std::nullopt;
}

} // namespace Slic3r
