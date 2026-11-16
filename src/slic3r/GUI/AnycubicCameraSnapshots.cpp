#include "AnycubicCameraSnapshots.hpp"

#include <algorithm>
#include <chrono>
#include <utility>

#include <boost/log/trivial.hpp>
#include <nlohmann/json.hpp>
#include <wx/gdicmn.h>
#include <wx/image.h>
#include <wx/mstream.h>

extern "C" {
#include <libavformat/avformat.h>
}

#include "AVVideoDecoder.hpp"
#include "GUI_App.hpp"
#include "slic3r/Utils/AnycubicLink.hpp"

namespace Slic3r {
namespace GUI {

namespace {

constexpr const char *BRIDGE           = "http://127.0.0.1:18988";
constexpr int         RETRY_MS         = 5000;
constexpr int         MAX_WIDTH        = 1280;
// The first frames after joining a live H.264 stream can be partly gray; a few frames later the picture is whole.
constexpr int         FRAMES_TO_SKIP   = 2;
constexpr auto        CAPTURE_DEADLINE = std::chrono::seconds(12);

struct Interrupt
{
    const std::atomic_bool               *alive;
    std::chrono::steady_clock::time_point deadline;
};

int interrupt_capture(void *opaque)
{
    const auto *interrupt = static_cast<const Interrupt *>(opaque);
    return !*interrupt->alive || std::chrono::steady_clock::now() > interrupt->deadline;
}

} // namespace

std::string capture_video_frame_jpeg(const std::string &url, int max_width, std::string &error, const std::atomic_bool &alive)
{
    if (avformat_network_init() < 0) {
        error = "network initialization failed";
        return {};
    }
    AVFormatContext *format = avformat_alloc_context();
    if (format == nullptr) {
        avformat_network_deinit();
        error = "out of memory";
        return {};
    }
    Interrupt interrupt{&alive, std::chrono::steady_clock::now() + CAPTURE_DEADLINE};
    format->interrupt_callback = {&interrupt_capture, &interrupt};

    AVDictionary *options = nullptr;
    av_dict_set(&options, "probesize", "5000000", 0);
    av_dict_set(&options, "analyzeduration", "2000000", 0);
    av_dict_set(&options, "rw_timeout", "5000000", 0);
    av_dict_set(&options, "timeout", "5000000", 0);
    const int opened = avformat_open_input(&format, url.c_str(), nullptr, &options); // Frees format on failure.
    av_dict_free(&options);

    wxImage image;
    wxSize  frame_size;
    if (opened < 0) {
        error = "could not open the camera stream";
    } else if (avformat_find_stream_info(format, nullptr) < 0) {
        error = "the camera stream has no readable video";
    } else if (const int stream = av_find_best_stream(format, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0); stream < 0) {
        error = "the camera stream has no video";
    } else {
        AVVideoDecoder decoder;
        AVPacket      *packet = av_packet_alloc();
        if (packet == nullptr || decoder.open(*format->streams[stream]->codecpar) < 0) {
            error = "the camera's video format cannot be decoded";
        } else {
            int decoded = 0;
            while (decoded <= FRAMES_TO_SKIP && av_read_frame(format, packet) >= 0) {
                if (packet->stream_index == stream && decoder.decode(*packet) == 0 && decoder.toWxImage(image, wxDefaultSize)) {
                    frame_size = decoder.decoded_frame_size();
                    ++decoded;
                }
                av_packet_unref(packet);
            }
            if (!image.IsOk())
                error = "no frame arrived from the camera";
        }
        av_packet_free(&packet);
    }
    if (opened >= 0)
        avformat_close_input(&format);
    avformat_network_deinit();
    if (!image.IsOk())
        return {};

    // toWxImage() pads the width to a multiple of 16 with black columns.
    if (frame_size.x > 0 && frame_size.y > 0 && frame_size.x <= image.GetWidth() && frame_size.y <= image.GetHeight())
        image = image.GetSubImage(wxRect(wxPoint(0, 0), frame_size));
    if (max_width > 0 && image.GetWidth() > max_width)
        image.Rescale(max_width, std::max(1, image.GetHeight() * max_width / image.GetWidth()), wxIMAGE_QUALITY_HIGH);
    image.SetOption(wxIMAGE_OPTION_QUALITY, 85);
    wxMemoryOutputStream jpeg;
    if (!image.SaveFile(jpeg, wxBITMAP_TYPE_JPEG)) {
        error = "the frame could not be saved as JPEG";
        return {};
    }
    std::string bytes(size_t(jpeg.GetLength()), '\0');
    jpeg.CopyTo(bytes.data(), bytes.size());
    return bytes;
}

AnycubicCameraSnapshots::AnycubicCameraSnapshots() : m_alive(std::make_shared<std::atomic_bool>(true))
{
    m_retry.SetOwner(this);
    Bind(wxEVT_TIMER, [this](wxTimerEvent &) { wait_for_request(); });
    m_retry.StartOnce(RETRY_MS);
}

AnycubicCameraSnapshots::~AnycubicCameraSnapshots()
{
    *m_alive = false;
    m_retry.Stop();
    if (m_wait)
        m_wait->cancel();
    if (m_capture.joinable())
        m_capture.join();
}

void AnycubicCameraSnapshots::wait_for_request()
{
    if (!*m_alive || m_wait)
        return;
    if (anycubic_lan_bridge_host().empty()) {
        m_retry.StartOnce(RETRY_MS);
        return;
    }
    auto alive = m_alive;
    // id > 0: a picture to take; otherwise wait again at once after an idle answer, or later after an error.
    auto done = [this, alive](long long id, std::string url, bool idle) {
        wxGetApp().CallAfter([this, alive, id, url = std::move(url), idle]() {
            if (!*alive)
                return;
            m_wait.reset();
            if (id > 0)
                take(id, url);
            else if (idle)
                wait_for_request();
            else
                m_retry.StartOnce(RETRY_MS);
        });
    };
    // The bridge holds this request for up to 20 seconds until a snapshot is asked for.
    m_wait = Http::get(std::string(BRIDGE) + "/camera/snapshot/wait")
                 .header("X-OrcaCubic-Token", anycubic_lan_bridge_token())
                 .timeout_connect(2)
                 .timeout_max(30)
                 .on_complete([done](std::string body, unsigned status) {
                     if (status == 200) {
                         try {
                             const auto request = nlohmann::json::parse(body);
                             done(request.value("id", 0LL), request.value("url", std::string()), true);
                             return;
                         } catch (const std::exception &) {}
                     }
                     done(0, {}, status == 204);
                 })
                 .on_error([done](std::string, std::string, unsigned) { done(0, {}, false); })
                 .perform();
}

void AnycubicCameraSnapshots::take(long long id, std::string url)
{
    if (m_capture.joinable())
        m_capture.join(); // The previous capture posted its answer before this request could arrive.
    auto alive = m_alive;
    m_capture = std::thread([this, alive, id, url = std::move(url)]() {
        std::string       error;
        const std::string jpeg = capture_video_frame_jpeg(url, MAX_WIDTH, error, *alive);
        if (!*alive)
            return;
        if (jpeg.empty())
            BOOST_LOG_TRIVIAL(warning) << "Anycubic camera snapshot failed: " << error;
        std::string answer = std::string(BRIDGE) + "/camera/snapshot?id=" + std::to_string(id);
        if (jpeg.empty())
            answer += "&error=" + Http::url_encode(error);
        Http::post(answer)
            .header("X-OrcaCubic-Token", anycubic_lan_bridge_token())
            .header("Content-Type", "image/jpeg")
            .set_post_body(jpeg)
            .timeout_connect(2)
            .timeout_max(5)
            .perform_sync();
        wxGetApp().CallAfter([this, alive]() {
            if (*alive)
                wait_for_request();
        });
    });
}

} // namespace GUI
} // namespace Slic3r
