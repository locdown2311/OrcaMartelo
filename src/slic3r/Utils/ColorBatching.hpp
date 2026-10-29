#pragma once

#include <string>

namespace Slic3r {

class PrintConfig;

// Passes the G-code file through Color Batching (github.com/shawn-makes-stuff/Color-Batching), the program at
// config.color_batching_program, in place, and puts back what the slicer added around the filament changes
// that the rewrite drops (restore_filament_change_extras()). Returns false, leaving the file as it is, when no
// program is set or the print changes no filament. Throws Slic3r::RuntimeError with the program's output when
// it fails.
bool run_color_batching(const std::string &gcode_path, const PrintConfig &config);

} // namespace Slic3r
