#include "AnycubicMcpBridge.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <optional>
#include <utility>
#include <vector>

#include <boost/filesystem.hpp>
#include <boost/format.hpp>
#include <boost/log/trivial.hpp>
#include <wx/base64.h>
#include <wx/gdicmn.h>
#include <wx/image.h>
#include <wx/mstream.h>

extern "C" {
#include <libavformat/avformat.h>
}

#include "libslic3r/Config.hpp"
#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/Preset.hpp"
#include "libslic3r/PresetBundle.hpp"
#include "libslic3r/Print.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Utils.hpp"
#include "AVVideoDecoder.hpp"
#include "Event.hpp"
#include "format.hpp"
#include "GLToolbar.hpp"
#include "GUI.hpp"
#include "GUI_App.hpp"
#include "I18N.hpp"
#include "MainFrame.hpp"
#include "MsgDialog.hpp"
#include "PartPlate.hpp"
#include "Plater.hpp"
#include "Tab.hpp"
#include "slic3r/Utils/AnycubicLink.hpp"

namespace Slic3r {
namespace GUI {

using nlohmann::json;
namespace fs = boost::filesystem;

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

namespace {

constexpr int SLICE_POLL_MS = 500;
// Fifteen minutes of polling, for a large plate.
constexpr int SLICE_MAX_TICKS = 15 * 60 * 1000 / SLICE_POLL_MS;
// How long slicing may take to start after the slice event before a plate with no result counts as failed.
constexpr int SLICE_START_TICKS = 6;

json ok(json result) { return {{"ok", true}, {"result", std::move(result)}}; }
json failed(const std::string &error) { return {{"ok", false}, {"error", error}}; }

Preset::Type preset_type(const std::string &name)
{
    if (name == "printer")
        return Preset::TYPE_PRINTER;
    if (name == "print")
        return Preset::TYPE_PRINT;
    if (name == "filament")
        return Preset::TYPE_FILAMENT;
    return Preset::TYPE_INVALID;
}

PresetCollection &presets(Preset::Type type)
{
    PresetBundle &bundle = *wxGetApp().preset_bundle;
    return type == Preset::TYPE_PRINTER ? bundle.printers : type == Preset::TYPE_PRINT ? bundle.prints : bundle.filaments;
}

// The plate the request names (1-based "plate"), or the current one.
PartPlate *requested_plate(const json &args, int &index, std::string &error)
{
    PartPlateList &plates = wxGetApp().plater()->get_partplate_list();
    index = plates.get_curr_plate_index();
    if (args.contains("plate") && args["plate"].is_number_integer()) {
        const int number = args["plate"].get<int>();
        if (number < 1 || number > plates.get_plate_count()) {
            error = (boost::format("There is no plate %1%; the project has %2%") % number % plates.get_plate_count()).str();
            return nullptr;
        }
        index = number - 1;
    }
    return plates.get_plate(index);
}

// The estimate of a sliced plate, or null when it has none.
json plate_estimate(PartPlate &plate)
{
    if (!plate.is_slice_result_ready_for_print())
        return nullptr;
    json estimate;
    if (const GCodeProcessorResult *result = plate.get_slice_result()) {
        const double seconds = result->print_statistics.modes[size_t(PrintEstimatedStatistics::ETimeMode::Normal)].time;
        estimate["estimated_minutes"] = int(std::lround(seconds / 60.));
        estimate["estimated_time"]    = get_time_dhms(float(seconds));
    }
    PrintBase *print = nullptr;
    plate.get_print(&print, nullptr, nullptr);
    if (const auto *fff = dynamic_cast<const Print *>(print); fff != nullptr) {
        const PrintStatistics &stats  = fff->print_statistics();
        estimate["filament_weight_g"] = std::round(stats.total_weight * 100.) / 100.;
        estimate["filament_length_m"] = std::round(stats.total_used_filament / 10.) / 100.;
        estimate["estimated_cost"]    = std::round(stats.total_cost * 100.) / 100.;
        estimate["filament_changes"]  = stats.total_toolchanges;
    }
    return estimate;
}

json slicer_state()
{
    PresetBundle &bundle = *wxGetApp().preset_bundle;
    Plater       *plater = wxGetApp().plater();
    json          state;
    state["printer_preset"]          = bundle.printers.get_edited_preset().name;
    state["process_preset"]          = bundle.prints.get_edited_preset().name;
    state["process_preset_modified"] = bundle.prints.get_edited_preset().is_dirty;

    const auto *colours = bundle.project_config.option<ConfigOptionStrings>("filament_colour");
    json        filaments = json::array();
    for (size_t i = 0; i < bundle.filament_presets.size(); ++i) {
        const Preset *preset = bundle.filaments.find_preset(bundle.filament_presets[i]);
        const auto   *types  = preset != nullptr ? preset->config.option<ConfigOptionStrings>("filament_type") : nullptr;
        filaments.push_back({{"filament", i + 1},
                             {"preset", bundle.filament_presets[i]},
                             {"type", types != nullptr && !types->values.empty() ? types->values.front() : ""},
                             {"color", colours != nullptr && i < colours->values.size() ? colours->values[i] : ""}});
    }
    state["filaments"] = filaments;

    PartPlateList &plates = plater->get_partplate_list();
    const Model   &model  = plater->model();
    json           list   = json::array();
    for (int p = 0; p < plates.get_plate_count(); ++p) {
        PartPlate *plate = plates.get_plate(p);
        json       objects = json::array();
        for (int o = 0; o < int(model.objects.size()); ++o)
            if (plate->contain_instance(o, 0))
                objects.push_back(model.objects[o]->name);
        list.push_back({{"plate", p + 1}, {"objects", objects}, {"estimate", plate_estimate(*plate)}});
    }
    state["plates"]        = list;
    state["current_plate"] = plates.get_curr_plate_index() + 1;
    return state;
}

json list_presets(const json &args)
{
    const Preset::Type type = preset_type(args.value("type", ""));
    if (type == Preset::TYPE_INVALID)
        return failed("type must be printer, print or filament");
    json names = json::array();
    for (const Preset &preset : presets(type))
        if (preset.is_visible && (type == Preset::TYPE_PRINTER || preset.is_compatible))
            names.push_back(preset.name);
    return ok({{"type", args["type"]}, {"selected", presets(type).get_edited_preset().name}, {"presets", names}});
}

json select_preset(const json &args)
{
    const Preset::Type type = preset_type(args.value("type", ""));
    if (type == Preset::TYPE_INVALID)
        return failed("type must be printer, print or filament");
    PresetBundle &bundle = *wxGetApp().preset_bundle;
    std::string   name   = args.value("name", "");
    if (presets(type).find_preset(name) == nullptr)
        name = bundle.get_preset_name_by_alias(type, name);
    const Preset *preset = presets(type).find_preset(name);
    if (preset == nullptr || !preset->is_visible)
        return failed("No preset named " + args.value("name", std::string()) + "; slicer_list_presets lists them");

    Plater *plater = wxGetApp().plater();
    if (type == Preset::TYPE_FILAMENT) {
        const int filament = args.contains("filament") && args["filament"].is_number_integer() ? args["filament"].get<int>() : 1;
        if (filament < 1 || filament > int(bundle.filament_presets.size()))
            return failed((boost::format("There is no filament %1%; the project has %2%") % filament % bundle.filament_presets.size()).str());
        // As choosing it in the sidebar's filament list does.
        bundle.set_filament_preset(size_t(filament - 1), name);
        plater->update_project_dirty_from_presets();
        bundle.export_selections(*wxGetApp().app_config);
        plater->sidebar().update_dynamic_filament_list();
        plater->sidebar().update_presets(Preset::TYPE_FILAMENT);
        plater->on_filament_change(size_t(filament - 1));
        return ok({{"type", "filament"}, {"filament", filament}, {"selected", name}});
    }
    Tab *tab = wxGetApp().get_tab(type);
    if (tab == nullptr || !tab->select_preset(name))
        return failed("OrcaMartelo did not switch to " + name);
    plater->on_config_change(bundle.full_config());
    return ok({{"type", args["type"]}, {"selected", presets(type).get_edited_preset().name}});
}

json get_settings(const json &args)
{
    const DynamicPrintConfig config = wxGetApp().preset_bundle->full_config();
    json values = json::object(), unknown = json::array();
    for (const auto &key : args.value("keys", json::array())) {
        const std::string k = key.is_string() ? key.get<std::string>() : std::string();
        if (config.has(k))
            values[k] = config.opt_serialize(k);
        else
            unknown.push_back(k);
    }
    return ok({{"settings", values}, {"unknown_keys", unknown}});
}

json set_settings(const json &args)
{
    const json settings = args.value("settings", json::object());
    if (!settings.is_object() || settings.empty())
        return failed("settings must name at least one key");
    const Preset::Type types[] = {Preset::TYPE_PRINT, Preset::TYPE_FILAMENT, Preset::TYPE_PRINTER};
    auto owner = [](Preset::Type type, const std::string &key) {
        const auto &keys = type == Preset::TYPE_PRINT ? Preset::print_options() : type == Preset::TYPE_FILAMENT ? Preset::filament_options() : Preset::printer_options();
        return std::find(keys.begin(), keys.end(), key) != keys.end();
    };
    std::vector<std::pair<Preset::Type, DynamicPrintConfig>> edits;
    json changed = json::object();
    for (const auto &item : settings.items()) {
        const std::string key = item.key();
        const json       &raw = item.value();
        const auto type = std::find_if(std::begin(types), std::end(types), [&](Preset::Type t) { return owner(t, key); });
        if (type == std::end(types))
            return failed("Unknown setting: " + key);
        auto edit = std::find_if(edits.begin(), edits.end(), [&](const auto &e) { return e.first == *type; });
        if (edit == edits.end())
            edit = edits.insert(edits.end(), {*type, presets(*type).get_edited_preset().config});
        DynamicPrintConfig &config = edit->second;
        std::string value = raw.is_string() ? raw.get<std::string>() : raw.is_boolean() ? (raw.get<bool>() ? "1" : "0") : raw.dump();
        // A per-extruder or per-variant list given one value takes it for every entry.
        if (const auto *vector = dynamic_cast<const ConfigOptionVectorBase *>(config.option(key));
            vector != nullptr && vector->size() > 1 && value.find(',') == std::string::npos && value.find(';') == std::string::npos) {
            std::string repeated = value;
            for (size_t i = 1; i < vector->size(); ++i)
                repeated += "," + value;
            value = repeated;
        }
        try {
            ConfigSubstitutionContext context(ForwardCompatibilitySubstitutionRule::Disable);
            config.set_deserialize(key, value, context);
        } catch (const std::exception &e) {
            return failed("Invalid value for " + key + ": " + e.what());
        }
        changed[key] = config.opt_serialize(key);
    }
    // As editing the fields in the settings tabs: the presets show as modified, unsaved.
    for (const auto &[type, config] : edits)
        if (Tab *tab = wxGetApp().get_tab(type))
            tab->load_config(config);
    wxGetApp().plater()->on_config_change(wxGetApp().preset_bundle->full_config());
    return ok({{"changed", changed}});
}

json load_models(const json &args)
{
    std::vector<fs::path> paths;
    for (const auto &path : args.value("paths", json::array())) {
        const fs::path file(path.is_string() ? path.get<std::string>() : std::string());
        if (!fs::is_regular_file(file))
            return failed("No such file: " + file.string());
        paths.push_back(file);
    }
    if (paths.empty())
        return failed("paths must list at least one file");
    Plater                   *plater = wxGetApp().plater();
    const std::vector<size_t> loaded = plater->load_files(paths, LoadStrategy::LoadModel);
    json                      names  = json::array();
    for (size_t index : loaded)
        if (index < plater->model().objects.size())
            names.push_back(plater->model().objects[index]->name);
    if (names.empty())
        return failed("OrcaMartelo did not load any object from those files");
    return ok({{"loaded", names}, {"current_plate", plater->get_partplate_list().get_curr_plate_index() + 1}});
}

// The question shown for an action, or empty for one this version does not know (it is refused).
wxString confirm_question(const json &a)
{
    const std::string action = a.value("action", "");
    auto number = [&](const char *key) -> std::optional<double> {
        return a.contains(key) && a[key].is_number() ? std::optional<double>(a[key].get<double>()) : std::nullopt;
    };
    auto text = [&](const char *key) { return a.contains(key) && a[key].is_string() ? from_u8(a[key].get<std::string>()) : wxString(); };
    auto mm   = [](double v) { return wxString::FromCDouble(std::abs(v), 1); };

    if (action == "stop_print")
        // TRN %1% is the file being printed, %2% how much of it is done, such as "45%".
        return format_wxstr(_L("Stop the print %1%? It is %2% done and cannot be resumed."), text("job"),
                            wxString::Format("%d%%", int(number("progress").value_or(0))));
    if (action == "set_temperature") {
        wxString lines;
        if (auto v = number("nozzle"))
            lines += (*v > 0 ? format_wxstr(_L("Set the nozzle to %1% °C."), int(*v)) : _L("Turn the nozzle heater off.")) + "\n";
        if (auto v = number("bed"))
            lines += (*v > 0 ? format_wxstr(_L("Set the bed to %1% °C."), int(*v)) : _L("Turn the bed heater off.")) + "\n";
        return lines.Trim();
    }
    if (action == "home_axes")
        return format_wxstr(_L("Home the %1% axes."), text("axes"));
    if (action == "move_axis")
        // TRN %1% is an axis letter (X, Y or Z), %2% a distance, such as "-10.0".
        return format_wxstr(_L("Move the %1% axis by %2% mm."), text("axis"), wxString::FromCDouble(number("distance").value_or(0), 1));
    if (action == "extrude")
        return number("length").value_or(0) >= 0 ? format_wxstr(_L("Extrude %1% mm of filament."), mm(number("length").value_or(0)))
                                                 : format_wxstr(_L("Retract %1% mm of filament."), mm(number("length").value_or(0)));
    if (action == "run_gcode") {
        wxArrayString lines = wxSplit(text("gcode"), '\n');
        wxString      shown;
        for (size_t i = 0; i < lines.size() && i < 20; ++i)
            shown += lines[i] + "\n";
        if (lines.size() > 20)
            shown += "...\n";
        return _L("Run this G-code on the printer:") + "\n\n" + shown.Trim();
    }
    if (action == "load_filament")
        // TRN %1% is the ACE slot number, %2% its material, such as "PETG".
        return format_wxstr(_L("Load the filament of ACE slot %1% (%2%)."), int(number("slot").value_or(0)), text("material"));
    if (action == "unload_filament")
        return format_wxstr(_L("Unload the filament of ACE slot %1%."), int(number("slot").value_or(0)));
    if (action == "start_print")
        return format_wxstr(_L("Start printing %1%."), text("file"));
    if (action == "print_plate") {
        wxString question = format_wxstr(_L("Print plate %1% on the printer."), int(number("plate").value_or(0)));
        if (auto minutes = number("minutes"); minutes && *minutes > 0)
            question += "\n" + format_wxstr(_L("Estimated time: %1%"), from_u8(get_time_dhms(float(*minutes * 60.))));
        if (auto cost = number("cost"); cost && *cost > 0)
            question += "\n" + format_wxstr(_L("Estimated cost: %1%"), wxString::FromCDouble(*cost, 2));
        return question;
    }
    if (action == "clear_plate")
        return _L("Delete every object of the project.");
    return {};
}

bool ask_user(const json &args)
{
    const wxString question = confirm_question(args);
    MainFrame     *frame    = wxGetApp().mainframe;
    if (question.empty() || frame == nullptr)
        return false;
    if (frame->IsIconized())
        frame->Iconize(false);
    frame->Raise();
    frame->RequestUserAttention();
    MessageDialog dialog(frame, _L("An AI assistant connected over MCP asks to do this:") + "\n\n" + question + "\n\n" + _L("Allow it?"),
                         _L("AI assistant request"), wxYES_NO | wxICON_WARNING);
    return dialog.ShowModal() == wxID_YES;
}

std::string safe_file_stem(const wxString &name)
{
    std::string stem;
    for (char c : into_u8(name))
        stem += std::isalnum(static_cast<unsigned char>(c)) || c == '-' || c == '_' ? c : '_';
    return stem.empty() ? std::string("OrcaMartelo") : stem.substr(0, 60);
}

} // namespace

AnycubicMcpBridge::AnycubicMcpBridge() : m_alive(std::make_shared<std::atomic_bool>(true))
{
    m_retry.SetOwner(this, wxID_HIGHEST + 1);
    m_slice_timer.SetOwner(this, wxID_HIGHEST + 2);
    Bind(wxEVT_TIMER, [this](wxTimerEvent &) { wait_for_request(); }, m_retry.GetId());
    Bind(wxEVT_TIMER, [this](wxTimerEvent &) { check_slice(); }, m_slice_timer.GetId());
    m_retry.StartOnce(RETRY_MS);
}

AnycubicMcpBridge::~AnycubicMcpBridge()
{
    *m_alive = false;
    m_retry.Stop();
    m_slice_timer.Stop();
    if (m_wait)
        m_wait->cancel();
    if (m_capture.joinable())
        m_capture.join();
}

void AnycubicMcpBridge::wait_for_request()
{
    if (!*m_alive || m_wait)
        return;
    if (anycubic_lan_bridge_host().empty()) {
        m_retry.StartOnce(RETRY_MS);
        return;
    }
    auto alive = m_alive;
    // A request (id > 0) runs after the poll is re-armed, so a dialog or a slice does not hold up the next one.
    auto done = [this, alive](long long id, std::string kind, json args, bool idle) {
        wxGetApp().CallAfter([this, alive, id, kind = std::move(kind), args = std::move(args), idle]() {
            if (!*alive)
                return;
            m_wait.reset();
            if (id > 0 || idle)
                wait_for_request();
            else
                m_retry.StartOnce(RETRY_MS);
            if (id > 0)
                run(id, kind, args);
        });
    };
    // The bridge holds this request for up to 20 seconds until there is something to do.
    m_wait = Http::get(std::string(BRIDGE) + "/slicer/wait")
                 .header("X-OrcaCubic-Token", anycubic_lan_bridge_token())
                 .timeout_connect(2)
                 .timeout_max(30)
                 .on_complete([done](std::string body, unsigned status) {
                     if (status == 200) {
                         try {
                             const json request = json::parse(body);
                             done(request.value("id", 0LL), request.value("kind", std::string()), request.value("args", json::object()), true);
                             return;
                         } catch (const std::exception &) {}
                     }
                     done(0, {}, {}, status == 204);
                 })
                 .on_error([done](std::string, std::string, unsigned) { done(0, {}, {}, false); })
                 .perform();
}

void AnycubicMcpBridge::answer(long long id, const json &answer)
{
    Http::post(std::string(BRIDGE) + "/slicer/result?id=" + std::to_string(id))
        .header("X-OrcaCubic-Token", anycubic_lan_bridge_token())
        .header("Content-Type", "application/json")
        .set_post_body(answer.dump())
        .timeout_connect(2)
        .timeout_max(10)
        .perform();
}

void AnycubicMcpBridge::run(long long id, const std::string &kind, const json &args)
{
    if (kind == "camera_snapshot")
        return take_snapshot(id, args.value("url", std::string()));
    if (kind == "slice" || kind == "export_plate")
        return start_slice(id, args, kind == "export_plate");
    json result;
    try {
        if (kind == "confirm")
            result = ok({{"allowed", ask_user(args)}});
        else if (kind == "get_state")
            result = ok(slicer_state());
        else if (kind == "list_presets")
            result = list_presets(args);
        else if (kind == "select_preset")
            result = select_preset(args);
        else if (kind == "get_settings")
            result = get_settings(args);
        else if (kind == "set_settings")
            result = set_settings(args);
        else if (kind == "load_models")
            result = load_models(args);
        else if (kind == "arrange") {
            wxGetApp().plater()->arrange();
            result = ok({{"status", "arranging"}});
        } else if (kind == "clear_plate") {
            wxGetApp().plater()->delete_all_objects_from_model();
            result = ok({{"status", "deleted"}});
        } else
            result = failed("This OrcaMartelo version does not know " + kind);
    } catch (const std::exception &e) {
        result = failed(e.what());
    }
    if (*m_alive)
        answer(id, result);
}

void AnycubicMcpBridge::start_slice(long long id, const json &args, bool export_gcode)
{
    if (m_slice.id != 0)
        return answer(id, failed("OrcaMartelo is already slicing for another request"));
    int         index = 0;
    std::string error;
    PartPlate  *plate = requested_plate(args, index, error);
    if (plate == nullptr)
        return answer(id, failed(error));
    if (!plate->has_printable_instances())
        return answer(id, failed((boost::format("Plate %1% has nothing to print") % (index + 1)).str()));
    Plater *plater = wxGetApp().plater();
    if (plater->get_partplate_list().get_curr_plate_index() != index)
        plater->select_plate(index);
    m_slice = {id, index, export_gcode, 0};
    // Sliced already and unchanged since: the result is ready.
    if (!plate->is_slice_result_ready_for_print())
        wxPostEvent(plater, SimpleEvent(EVT_GLTOOLBAR_SLICE_PLATE));
    m_slice_timer.Start(SLICE_POLL_MS);
    check_slice();
}

void AnycubicMcpBridge::check_slice()
{
    if (m_slice.id == 0) {
        m_slice_timer.Stop();
        return;
    }
    Plater    *plater  = wxGetApp().plater();
    PartPlate *plate   = plater->get_partplate_list().get_plate(m_slice.plate);
    const bool slicing = plater->is_background_process_slicing();
    ++m_slice.ticks;
    json result;
    if (plate == nullptr)
        result = failed("The plate was removed while slicing");
    else if (!slicing && plate->is_slice_result_ready_for_print()) {
        json estimate     = plate_estimate(*plate);
        estimate["plate"] = m_slice.plate + 1;
        result            = ok(estimate);
        if (m_slice.export_gcode) {
            try {
                const fs::path    source(plate->get_tmp_gcode_path());
                const std::string filename = safe_file_stem(plater->get_project_name()) + "_plate" + std::to_string(m_slice.plate + 1) + ".gcode";
                const fs::path    target   = fs::temp_directory_path() / fs::unique_path("orcamartelo-mcp-%%%%%%%%.gcode");
                fs::copy_file(source, target, fs::copy_options::overwrite_existing);
                result["result"]["path"]     = target.string();
                result["result"]["filename"] = filename;
            } catch (const std::exception &e) {
                result = failed(std::string("Could not export the G-code: ") + e.what());
            }
        }
    } else if (!slicing && m_slice.ticks > SLICE_START_TICKS)
        result = failed("Slicing stopped without a result. OrcaMartelo shows why, such as an object outside the plate or a setting conflict.");
    else if (m_slice.ticks > SLICE_MAX_TICKS)
        result = failed("Slicing took too long");
    else
        return;
    const long long id = m_slice.id;
    m_slice            = {};
    m_slice_timer.Stop();
    answer(id, result);
}

void AnycubicMcpBridge::take_snapshot(long long id, std::string url)
{
    if (m_capture.joinable())
        m_capture.join(); // Snapshots run one at a time; the previous one has posted its answer.
    auto alive = m_alive;
    m_capture  = std::thread([alive, id, url = std::move(url)]() {
        std::string       error;
        const std::string jpeg = capture_video_frame_jpeg(url, MAX_WIDTH, error, *alive);
        if (!*alive)
            return;
        json answer;
        if (jpeg.empty()) {
            BOOST_LOG_TRIVIAL(warning) << "Anycubic camera snapshot failed: " << error;
            answer = failed("The camera picture failed: " + error);
        } else
            answer = ok({{"jpeg_base64", into_u8(wxBase64Encode(jpeg.data(), jpeg.size()))}});
        Http::post(std::string(BRIDGE) + "/slicer/result?id=" + std::to_string(id))
            .header("X-OrcaCubic-Token", anycubic_lan_bridge_token())
            .header("Content-Type", "application/json")
            .set_post_body(answer.dump())
            .timeout_connect(2)
            .timeout_max(10)
            .perform_sync();
    });
}

} // namespace GUI
} // namespace Slic3r
