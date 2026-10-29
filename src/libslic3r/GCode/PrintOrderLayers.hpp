#pragma once

#include <string>

namespace Slic3r {

// Color Batching prints a color several layers up before the next one comes back down, and writes no layer
// change tags: read again, the whole print would be one layer in the preview. This puts back ;LAYER_CHANGE,
// ;Z: and ;HEIGHT: before each extrusion at a new height, past the line `start_marker` begins (the start
// G-code before it is left as it is), so the layer slider steps through the print in printing order. A
// layer's height is the step down to the next lower height printed. Without the marker nothing changes.
std::string mark_layers_in_print_order(const std::string &gcode, const std::string &start_marker);

} // namespace Slic3r
