#include "FilamentChangeFlush.hpp"

#include "../LocalesUtils.hpp"
#include "../PrintConfig.hpp"
#include "../libslic3r.h"

#include <cctype>
#include <cmath>
#include <string_view>
#include <sstream>
#include <vector>

namespace Slic3r {

double printer_flush_volume(const PrintConfig &config, size_t from_filament, size_t to_filament)
{
    if (! config.printer_flushes_at_filament_change || (config.enable_prime_tower && config.purge_in_prime_tower) || from_filament == to_filament)
        return 0.;
    const size_t              filaments   = config.filament_diameter.size();
    const std::vector<double> matrix      = get_flush_volumes_matrix(config.flush_volumes_matrix.values, 0, config.nozzle_diameter.size());
    const ConfigOptionFloats &multipliers = config.prime_volume_mode == PrimeVolumeMode::pvmFast ? config.flush_multiplier_fast : config.flush_multiplier;
    if (from_filament >= filaments || to_filament >= filaments || matrix.size() < filaments * filaments)
        return 0.;
    return std::max(0., matrix[from_filament * filaments + to_filament] * (multipliers.empty() ? 1. : multipliers.get_at(0)));
}

std::string printer_flush_tag(const PrintConfig &config, size_t from_filament, size_t to_filament)
{
    const double volume = printer_flush_volume(config, from_filament, to_filament);
    const double area   = to_filament < config.filament_diameter.size() ? 0.25 * M_PI * sqr(config.filament_diameter.get_at(to_filament)) : 0.;
    if (volume <= 0. || area <= 0.)
        return {};
    // GCodeProcessor::External_Purge_Tag, with the length of filament flushed.
    return "; EXTERNAL_PURGE " + float_to_string_decimal_point(volume / area, 3) + "\n";
}

// The long retraction the slicer makes before a cut for this filament (Extruder::slicer_retracts_when_cut()).
static double cut_retraction(const PrintConfig &config, size_t filament)
{
    // get_at() falls back to the first value, as for a printer option with one value for every filament.
    if (! config.single_extruder_multi_material || config.long_retractions_when_cut.empty() || config.retraction_distances_when_cut.empty() ||
        ! config.long_retractions_when_cut.get_at(filament) || config.retraction_distances_when_cut.get_at(filament) <= 0.)
        return 0.;
    const std::string &change = config.change_filament_gcode.value;
    if (change.find("retraction_distance_when_cut") != std::string::npos || change.find("retraction_distances_when_cut") != std::string::npos)
        return 0.;
    return config.retraction_distances_when_cut.get_at(filament);
}

// The filament a line selects, "T<n>" with nothing but a comment after it; -1 for any other line.
static int tool_of(const std::string &line)
{
    if (line.size() < 2 || line[0] != 'T' || ! std::isdigit(static_cast<unsigned char>(line[1])))
        return -1;
    size_t i = 1;
    int    tool = 0;
    for (; i < line.size() && std::isdigit(static_cast<unsigned char>(line[i])); ++ i)
        tool = tool * 10 + (line[i] - '0');
    for (; i < line.size() && (line[i] == ' ' || line[i] == '\t' || line[i] == '\r'); ++ i) ;
    return i == line.size() || line[i] == ';' ? tool : -1;
}

// The length a line retracting the filament alone ("G1 E-<length>", maybe a feed rate) retracts; 0 for any other.
static double retraction_of(const std::string &line)
{
    if (line.rfind("G1 E-", 0) != 0)
        return 0.;
    size_t       used   = 0;
    const double length = string_to_double_decimal_point(std::string_view(line).substr(5), &used);
    size_t       next   = 5 + used;
    for (; next < line.size() && line[next] == ' '; ++ next) ;
    return used > 0 && (next == line.size() || line[next] == 'F' || line[next] == ';' || line[next] == '\r') ? length : 0.;
}

std::string restore_filament_change_extras(const std::string &gcode, const PrintConfig &config)
{
    std::istringstream in(gcode);
    std::vector<std::string> lines;
    for (std::string line; std::getline(in, line);)
        lines.emplace_back(std::move(line));
    const bool relative_e = config.use_relative_e_distances;
    std::string out;
    out.reserve(gcode.size() + gcode.size() / 50);
    int current = -1;
    for (size_t i = 0; i < lines.size(); ++ i) {
        const int tool = tool_of(lines[i]);
        if (tool >= 0 && current >= 0 && tool != current) {
            const double retraction = relative_e ? cut_retraction(config, size_t(current)) : 0.;
            // Not when the line before already makes it.
            if (retraction > 0. && (i == 0 || std::abs(retraction_of(lines[i - 1]) - retraction) > EPSILON)) {
                const double speed = config.retraction_speed.empty() ? 0. : config.retraction_speed.get_at(current);
                out += "G1 E-" + float_to_string_decimal_point(retraction, 3);
                if (speed > 0.)
                    out += " F" + float_to_string_decimal_point(60. * speed, 0);
                out += " ; retract before the cut\n";
            }
            out += lines[i];
            out += '\n';
            if (i + 1 >= lines.size() || lines[i + 1].rfind("; EXTERNAL_PURGE", 0) != 0)
                out += printer_flush_tag(config, size_t(current), size_t(tool));
            current = tool;
            continue;
        }
        if (tool >= 0)
            current = tool;
        out += lines[i];
        out += '\n';
    }
    return out;
}

} // namespace Slic3r
