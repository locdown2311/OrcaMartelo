///|/ Copyright (c) Prusa Research 2023 Enrico Turri @enricoturri1966
///|/
///|/ libvgcode is released under the terms of the AGPLv3 or higher
///|/
#ifndef VGCODE_GCODEINPUTDATA_HPP
#define VGCODE_GCODEINPUTDATA_HPP

#include "PathVertex.hpp"
#include <vector>
#include "Types.hpp"

namespace libvgcode {

struct GCodeInputData
{
    //
    // Whether or not the gcode was generated with spiral vase mode enabled.
    // Required to properly detect fictitious layer changes when spiral vase mode is enabled.
    //
    bool spiral_vase_mode{ false };
    //
    // List of path vertices (gcode moves)
    // See: PathVertex
    //
    std::vector<PathVertex> vertices;
    //
    // Palette for extruders colors
    //
    PaletteWithAlpha tools_colors;
    //
    // Palette for color print colors
    //
    PaletteWithAlpha color_print_colors;
    //
    // Multiplanar cut planes: palette of unit build directions referenced by
    // PathVertex::tool_axis_id. Entry 0 must be +Z; left empty, the viewer supplies { +Z }.
    //
    std::vector<Vec3> tool_axes;
};

} // namespace libvgcode

#endif // VGCODE_BITSET_HPP
