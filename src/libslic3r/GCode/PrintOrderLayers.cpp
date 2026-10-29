#include "PrintOrderLayers.hpp"

#include "../LocalesUtils.hpp"

#include <algorithm>
#include <cmath>
#include <sstream>
#include <string_view>
#include <vector>

namespace Slic3r {

namespace {

// A G0/G1 move: the values it sets, NaN where it sets none.
struct Move
{
    bool   is_move = false;
    double x = NAN, y = NAN, z = NAN, e = NAN;
};

Move parse_move(const std::string &line)
{
    Move m;
    if (line.size() < 3 || line[0] != 'G' || (line[1] != '0' && line[1] != '1') || (line[2] != ' ' && line[2] != '\t'))
        return m;
    m.is_move = true;
    for (size_t i = 3; i < line.size() && line[i] != ';';) {
        const char axis = line[i];
        if (axis == 'X' || axis == 'Y' || axis == 'Z' || axis == 'E') {
            size_t used = 0;
            const double v = string_to_double_decimal_point(std::string_view(line).substr(i + 1), &used);
            if (used > 0)
                (axis == 'X' ? m.x : axis == 'Y' ? m.y : axis == 'Z' ? m.z : m.e) = v;
            i += 1 + used;
        } else
            ++ i;
    }
    return m;
}

bool extrudes(const Move &m) { return m.is_move && ! std::isnan(m.e) && m.e > 0. && (! std::isnan(m.x) || ! std::isnan(m.y)); }

} // namespace

std::string mark_layers_in_print_order(const std::string &gcode, const std::string &start_marker)
{
    const size_t start = gcode.find("\n" + start_marker);
    if (start_marker.empty() || start == std::string::npos)
        return gcode;
    std::vector<std::string> lines;
    {
        std::istringstream in(gcode);
        for (std::string line; std::getline(in, line);)
            lines.emplace_back(std::move(line));
    }
    size_t first = 0;
    for (size_t offset = 0; first < lines.size() && offset <= start; ++ first)
        offset += lines[first].size() + 1;

    // The heights extruded at past the marker, each one's layer height the step from the one below.
    std::vector<double> heights;
    {
        double z = 0.;
        for (size_t i = 0; i < lines.size(); ++ i) {
            const Move m = parse_move(lines[i]);
            if (! std::isnan(m.z))
                z = m.z;
            if (i >= first && extrudes(m))
                heights.push_back(z);
        }
    }
    std::sort(heights.begin(), heights.end());
    heights.erase(std::unique(heights.begin(), heights.end(), [](double a, double b) { return std::abs(a - b) < 1e-4; }), heights.end());
    auto height_of = [&heights](double z) {
        auto it = std::lower_bound(heights.begin(), heights.end(), z - 1e-4);
        return it == heights.begin() ? z : z - *(it - 1);
    };

    std::string out;
    out.reserve(gcode.size() + gcode.size() / 20);
    double z = 0., layer_z = NAN;
    for (size_t i = 0; i < lines.size(); ++ i) {
        const Move m = parse_move(lines[i]);
        if (! std::isnan(m.z))
            z = m.z;
        if (i >= first && extrudes(m) && (std::isnan(layer_z) || std::abs(z - layer_z) > 1e-4)) {
            layer_z = z;
            out += ";LAYER_CHANGE\n;Z:" + float_to_string_decimal_point(z, 3) + "\n;HEIGHT:" +
                   float_to_string_decimal_point(height_of(z), 3) + "\n";
        }
        out += lines[i];
        out += '\n';
    }
    return out;
}

} // namespace Slic3r
