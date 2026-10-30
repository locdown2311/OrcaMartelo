#include "AnycubicNotifications.hpp"

#include <algorithm>
#include <string>

#include <boost/format.hpp>
#include <boost/log/trivial.hpp>
#include <nlohmann/json.hpp>
#include <wx/app.h>
#include <wx/string.h>

#include "GUI_App.hpp"
#include "I18N.hpp"
#include "MainFrame.hpp"
#include "Notebook.hpp"
#include "NotificationManager.hpp"
#include "slic3r/Utils/AnycubicLink.hpp"
#include "slic3r/Utils/Http.hpp"

namespace Slic3r {
namespace GUI {

namespace {

constexpr int POLL_INTERVAL_MS = 5000;

bool device_tab_shown()
{
    MainFrame *frame = wxGetApp().mainframe;
    if (frame == nullptr || frame->m_tabpanel == nullptr)
        return false;
    const wxString page = frame->m_tabpanel->GetSelectedPageName();
    return page == TAB_ID_MONITOR || page == TAB_ID_MONITOR_WEB;
}

} // namespace

AnycubicNotificationPoller::AnycubicNotificationPoller(NotificationManager *notifications)
    : m_notifications(notifications), m_alive(std::make_shared<std::atomic_bool>(true))
{
    m_timer.SetOwner(this);
    Bind(wxEVT_TIMER, [this](wxTimerEvent &) { poll(); });
    m_timer.Start(POLL_INTERVAL_MS);
}

AnycubicNotificationPoller::~AnycubicNotificationPoller()
{
    m_timer.Stop();
    *m_alive = false;
}

void AnycubicNotificationPoller::poll()
{
    if (m_in_flight || anycubic_lan_bridge_host().empty())
        return;
    m_in_flight = true;
    const std::string url = "http://127.0.0.1:18988/notifications?since=" + std::to_string(std::max(m_last_id, 0LL));
    auto alive = m_alive;
    auto finish = [this, alive](std::string body) {
        wxGetApp().CallAfter([this, alive, body = std::move(body)]() {
            if (!*alive)
                return;
            m_in_flight = false;
            if (!body.empty())
                on_response(body);
        });
    };
    Http::get(url)
        .header("X-OrcaCubic-Token", anycubic_lan_bridge_token())
        .timeout_connect(2)
        .timeout_max(4)
        .on_complete([finish](std::string body, unsigned status) { finish(status == 200 ? std::move(body) : std::string()); })
        .on_error([finish](std::string, std::string, unsigned) { finish({}); })
        .perform();
}

void AnycubicNotificationPoller::on_response(const std::string &body)
{
    nlohmann::json data;
    try {
        data = nlohmann::json::parse(body);
    } catch (const std::exception &e) {
        BOOST_LOG_TRIVIAL(warning) << "Anycubic notifications: " << e.what();
        return;
    }
    const long long last_id = data.value("last_id", 0LL);
    // A restarted bridge (another printer) or a cleared log starts over.
    const bool baseline = m_last_id < 0 || last_id < m_last_id;
    const long long seen = m_last_id;
    m_last_id = last_id;
    if (baseline || m_notifications == nullptr || device_tab_shown() || !data.contains("notifications"))
        return;

    using Level = NotificationManager::NotificationLevel;
    for (const auto &n : data["notifications"]) {
        if (!n.is_object() || n.value("id", 0LL) <= seen)
            continue;
        const std::string event   = n.value("event", "");
        const std::string kind    = n.value("kind", "");
        const std::string job     = n.value("job", "");
        const std::string message = n.value("message", "");
        std::string       text;
        Level             level = Level::WarningNotificationLevel;
        if (event == "finished") {
            text  = (boost::format(_u8L("Print finished: %1%")) % job).str();
            level = Level::ImportantNotificationLevel;
        } else if (event == "stopped") {
            text = (boost::format(_u8L("Print stopped: %1%")) % job).str();
        } else if (event == "paused") {
            text = (boost::format(_u8L("Print paused: %1%")) % job).str();
        } else if (event == "failed") {
            text  = (boost::format(_u8L("Print failed: %1%")) % job).str();
            level = Level::ErrorNotificationLevel;
        } else if (event == "printer_error" && kind == "error") {
            // TRN %1% is the printer's error code, %2% the printer's own message.
            text  = (boost::format(_u8L("Printer error %1%: %2%")) % n.value("code", 0) % message).str();
            level = Level::ErrorNotificationLevel;
        } else {
            continue;
        }
        m_notifications->push_notification(NotificationType::CustomNotification, level, text, _u8L("Open Device tab"),
                                           [](wxEvtHandler *) {
                                               if (MainFrame *frame = wxGetApp().mainframe)
                                                   frame->select_tab(wxString(TAB_ID_MONITOR));
                                               return true;
                                           });
    }
}

} // namespace GUI
} // namespace Slic3r
