#pragma once

// Places the wipe tower beside the parts of a plate, on the path from the nozzle wiper (or purge
// chute) to the parts: the nozzle then goes wiper -> tower -> parts in one short stretch, so it
// travels less and oozes less onto the parts.

#include "BoundingBox.hpp"
#include "Point.hpp"
#include "PrintConfig.hpp"

#include <optional>

namespace Slic3r {

struct WipeTowerPlacementInput
{
    BoundingBoxf parts;               // the plate's parts seen from above
    double       parts_height { 0. };
    Vec2d        tower_size;          // the tower's body, without its brim
    double       tower_brim { 0. };
    BoundingBoxf bed;                 // the printable area
    NozzleWiperPosition wiper { NozzleWiperPosition::None };
    // The bed moves in Y (an "i3" printer): a tall part is best braced with the tower beside its
    // middle, near the bed's centre in Y, where the bed's moves shake it least.
    bool         bed_slinger { false };
    double       gap { 15. };         // between the parts and the tower's brim
    Vec2d        current;             // the tower's current lower left corner, for NozzleWiperPosition::None
};

// Lower left corner of the tower's body, or nothing when there is no room for it beside the parts.
// Sides are tried nearest the wiper first; with no wiper, from the side the tower is on now.
std::optional<Vec2d> place_wipe_tower_beside_parts(const WipeTowerPlacementInput &in);

} // namespace Slic3r
