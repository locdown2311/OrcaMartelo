#pragma once

// The parts of a filament change that a printer does on its own and the G-code does not show: the flush
// (printer_flushes_at_filament_change) and the long retraction before the cut that the slicer adds for it.

#include <cstddef>
#include <string>

namespace Slic3r {

class PrintConfig;

// The volume (mm³) the printer flushes changing from one filament to the other, the flushing volume of the
// pair times the flushing multiplier; 0 unless the printer flushes and the prime tower does not.
double printer_flush_volume(const PrintConfig &config, size_t from_filament, size_t to_filament);

// The comment telling the G-code processor that flush, as GCodeProcessor::External_Purge_Tag; empty without one.
std::string printer_flush_tag(const PrintConfig &config, size_t from_filament, size_t to_filament);

// G-code rewritten by another program after export (Color Batching) loses what the slicer added around its
// filament changes: before each change after the first, the long retraction when cut the slicer makes itself
// (relative extrusion only), and after it the printer flush comment. Both put back, once.
std::string restore_filament_change_extras(const std::string &gcode, const PrintConfig &config);

} // namespace Slic3r
