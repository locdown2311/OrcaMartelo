#include "ColorBatching.hpp"

#include "BoostProcessCompat.hpp"

#include "libslic3r/Exception.hpp"
#include "libslic3r/GCode/FilamentChangeFlush.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/format.hpp"
#include "slic3r/GUI/I18N.hpp"

#include <boost/filesystem.hpp>
#include <boost/log/trivial.hpp>
#include <boost/nowide/convert.hpp>
#include <boost/nowide/fstream.hpp>

#include <iterator>
#include <sstream>
#include <string>

namespace Slic3r {

namespace fs = boost::filesystem;

static std::string read_file(const fs::path &path)
{
    boost::nowide::ifstream in(path.string(), std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(in), std::istreambuf_iterator<char>());
}

bool run_color_batching(const std::string &gcode_path, const PrintConfig &config)
{
    const std::string program = config.color_batching_program.value;
    if (program.empty() || config.filament_diameter.size() < 2)
        return false;
    const fs::path exe = fs::path(boost::nowide::widen(program));
    if (! fs::exists(exe))
        throw Slic3r::RuntimeError(format(_u8L("The Color Batching program does not exist: %1%"), program));

    const fs::path input  = fs::path(boost::nowide::widen(gcode_path));
    const fs::path output = input.parent_path() / (input.stem().wstring() + L".color-batching" + input.extension().wstring());
    std::string    log;
    int            exit_code = -1;
    try {
        namespace bp = boost::process;
        bp::ipstream out;
        // The program reads its color-batching.toml beside itself.
        // Arguments in the platform's own characters, wide on Windows, so any path reaches the program as is.
#ifdef _WIN32
        const std::wstring output_flag = L"-o";
#else
        const std::string output_flag = "-o";
#endif
        bp::child child(exe, input.native(), output_flag, output.native(), bp::start_dir(exe.parent_path()),
#ifdef _WIN32
                        bp::windows::create_no_window,
#endif
                        (bp::std_out & bp::std_err) > out);
        for (std::string line; std::getline(out, line);)
            log += line + "\n";
        child.wait();
        exit_code = child.exit_code();
    } catch (const std::exception &ex) {
        throw Slic3r::RuntimeError(format(_u8L("Failed starting the Color Batching program %1%: %2%"), program, ex.what()));
    }
    BOOST_LOG_TRIVIAL(info) << "Color Batching on " << gcode_path << " exited with " << exit_code << ":\n" << log;
    if (exit_code != 0 || ! fs::exists(output)) {
        boost::system::error_code ec;
        fs::remove(output, ec);
        throw Slic3r::RuntimeError(format(_u8L("Color Batching failed on the G-code:\n%1%"), log));
    }

    const std::string batched = restore_filament_change_extras(read_file(output), config);
    boost::system::error_code ec;
    fs::remove(output, ec);
    {
        boost::nowide::ofstream file(gcode_path, std::ios::binary | std::ios::trunc);
        file << batched;
        if (! file)
            throw Slic3r::RuntimeError(format(_u8L("Failed writing the G-code from Color Batching to %1%"), gcode_path));
    }
    return true;
}

} // namespace Slic3r
