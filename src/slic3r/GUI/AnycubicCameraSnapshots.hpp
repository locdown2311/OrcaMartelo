#pragma once

#include <atomic>
#include <memory>
#include <string>
#include <thread>

#include <wx/event.h>
#include <wx/timer.h>

#include "slic3r/Utils/Http.hpp"

namespace Slic3r {
namespace GUI {

// Takes camera pictures for the Anycubic LAN bridge's MCP server. The bridge reads
// MJPEG cameras itself, but the printer's own camera streams H.264 in FLV, which
// only the slicer can decode (FFmpeg). This waits on the bridge for a request,
// decodes one frame of the stream and posts it back as a JPEG.
class AnycubicCameraSnapshots : public wxEvtHandler
{
public:
    AnycubicCameraSnapshots();
    ~AnycubicCameraSnapshots() override;

private:
    void wait_for_request();
    void take(long long id, std::string url);

    wxTimer                           m_retry;
    std::shared_ptr<std::atomic_bool> m_alive;
    Http::Ptr                         m_wait;
    std::thread                       m_capture;
};

// A JPEG of a frame of the video stream at url, at most max_width wide. Empty, with
// error set, when no frame could be read; it gives up once alive turns false.
std::string capture_video_frame_jpeg(const std::string &url, int max_width, std::string &error, const std::atomic_bool &alive);

} // namespace GUI
} // namespace Slic3r
