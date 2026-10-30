#pragma once

#include <atomic>
#include <memory>
#include <string>

#include <wx/event.h>
#include <wx/timer.h>

namespace Slic3r {
namespace GUI {

class NotificationManager;

// Polls the Anycubic LAN bridge's notification log and shows print endings,
// pauses and printer errors as popups, so they are seen outside the Device tab.
// The Device tab lists the full log itself.
class AnycubicNotificationPoller : public wxEvtHandler
{
public:
    explicit AnycubicNotificationPoller(NotificationManager *notifications);
    ~AnycubicNotificationPoller() override;

private:
    void poll();
    void on_response(const std::string &body);

    NotificationManager              *m_notifications;
    wxTimer                           m_timer;
    std::shared_ptr<std::atomic_bool> m_alive;
    bool                              m_in_flight{false};
    // Id of the newest entry already seen; -1 until the first answer, which
    // only sets the baseline so old entries are not shown again.
    long long                         m_last_id{-1};
};

} // namespace GUI
} // namespace Slic3r
