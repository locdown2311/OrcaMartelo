#pragma once

#include <atomic>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>
#include <wx/event.h>
#include <wx/timer.h>

#include "slic3r/Utils/Http.hpp"

namespace Slic3r {
namespace GUI {

// Runs the requests the Anycubic LAN bridge's MCP server cannot answer itself
// (docs/HLSD/anycubic-mcp.md): a camera frame the bundled Python cannot decode, a
// confirmation the user gives in a dialog, and the slicer tools (presets,
// settings, models, slicing). It holds a long poll on the bridge's /slicer/wait,
// runs each request on the GUI thread and posts the answer to /slicer/result.
class AnycubicMcpBridge : public wxEvtHandler
{
public:
    AnycubicMcpBridge();
    ~AnycubicMcpBridge() override;

private:
    void wait_for_request();
    void run(long long id, const std::string &kind, const nlohmann::json &args);
    void answer(long long id, const nlohmann::json &answer);
    void take_snapshot(long long id, std::string url);
    // Slices a plate when needed and answers its estimate, or sends it to the printer when send is set.
    void start_slice(long long id, const nlohmann::json &args, bool send);
    void check_slice();

    wxTimer                           m_retry;
    wxTimer                           m_slice_timer;
    std::shared_ptr<std::atomic_bool> m_alive;
    Http::Ptr                         m_wait;
    std::thread                       m_capture;

    // The slice an MCP request waits for; id 0 when none.
    struct PendingSlice
    {
        long long        id{0};
        int              plate{0};
        int              ticks{0};
        bool             send{false};
        std::vector<int> slot_ids; // For send: the ACE slot of each filament of the plate.
    } m_slice;
};

// A JPEG of a frame of the video stream at url, at most max_width wide. Empty, with
// error set, when no frame could be read; it gives up once alive turns false.
std::string capture_video_frame_jpeg(const std::string &url, int max_width, std::string &error, const std::atomic_bool &alive);

} // namespace GUI
} // namespace Slic3r
