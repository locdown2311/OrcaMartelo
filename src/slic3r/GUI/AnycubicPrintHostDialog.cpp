#include "PrintHostDialogs.hpp"
#include "../Utils/AnycubicLink.hpp"

#include <algorithm>
#include <cctype>
#include <limits>

#include <wx/panel.h>
#include <wx/sizer.h>
#include <wx/stattext.h>

#include <boost/algorithm/string.hpp>
#include <nlohmann/json.hpp>

#include "GUI.hpp"
#include "GUI_App.hpp"
#include "format.hpp"
#include "I18N.hpp"
#include "BitmapComboBox.hpp"
#include "ExtraRenderers.hpp"
#include "wxExtensions.hpp"
#include "libslic3r/AppConfig.hpp"
#include "slic3r/GUI/Widgets/StaticLine.hpp"
#include <wx/statbmp.h>
#include "nanosvg/nanosvg.h"
#include "nanosvg/nanosvgrast.h"

namespace fs = boost::filesystem;
using json = nlohmann::json;

namespace Slic3r {
namespace GUI {
namespace {

wxBitmap get_anycubic_spool_bitmap(const std::string& raw_color, int target_w, int target_h)
{
    std::string color_hex = raw_color;
    if (color_hex.empty()) color_hex = "#FDDB27";
    if (color_hex[0] != '#') color_hex = "#" + color_hex;
    if (color_hex.length() > 7) color_hex = color_hex.substr(0, 7);

    static std::map<std::pair<std::string, std::pair<int, int>>, wxBitmap> spool_cache;
    auto key = std::make_pair(color_hex, std::make_pair(target_w, target_h));
    auto it = spool_cache.find(key);
    if (it != spool_cache.end())
        return it->second;

    std::string svg = R"svg(<svg width="55" height="70" viewBox="0 0 55 70" fill="none" xmlns="http://www.w3.org/2000/svg">
<g id="spool-model">
<ellipse cx="10.6468" cy="34.9304" rx="10.6468" ry="34.9304" transform="matrix(-1 0 0 1 55 0)" fill="#D1D1D1" fill-opacity="0.25"/>
<path d="M44.3515 0.478516C43.1101 0.478516 41.8342 1.30526 40.5957 3.02439C39.3648 4.73202 38.2375 7.23458 37.2818 10.3705C35.3723 16.6345 34.1836 25.3178 34.1836 34.9295C34.1836 44.5422 35.3723 53.2255 37.2818 59.4896C38.2375 62.6254 39.3648 65.128 40.5957 66.8353C41.8342 68.5551 43.1101 69.381 44.3515 69.381C45.5919 69.381 46.8678 68.5542 48.1063 66.8353C49.3372 65.128 50.4655 62.6254 51.4212 59.4896C53.3307 53.2255 54.5184 44.5422 54.5184 34.9295C54.5184 25.3178 53.3307 16.6345 51.4212 10.3705C50.4655 7.23458 49.3372 4.73202 48.1063 3.02439C46.8669 1.30545 45.5919 0.478611 44.3515 0.478516Z" stroke="#9ca3af" stroke-width="1.2"/>
<path id="spool-wound-fill" d="M42.4334 3.73242C39.5451 3.73242 8.28125 3.73242 8.28125 3.73242C8.28125 3.73242 13.7817 19.5175 13.7817 35.6481C13.7817 51.7787 8.28125 67.5635 8.28125 67.5635C8.28125 67.5635 40.2324 67.5635 42.4334 67.5635C44.6343 67.5635 50.2968 52.8068 50.2968 35.6481C50.2968 18.4894 45.3216 3.73242 42.4334 3.73242Z" fill="%COLOR%"/>
<ellipse cx="10.6468" cy="34.9304" rx="10.6468" ry="34.9304" transform="matrix(-1 0 0 1 21.293 0.138672)" fill="#D1D1D1" fill-opacity="0.25"/>
<path d="M10.6445 0.617188C9.40307 0.617188 8.12717 1.44393 6.88865 3.16316C5.65781 4.87145 4.53049 7.37402 3.57477 10.5089C1.66523 16.773 0.476562 25.4563 0.476562 35.069C0.476562 44.6807 1.66523 53.3639 3.57477 59.629C4.53049 62.7639 5.65781 65.2665 6.88865 66.9748C8.12717 68.6936 9.40307 69.5204 10.6445 69.5204C11.8849 69.5204 13.1598 68.6936 14.3993 66.9748C15.6301 65.2665 16.7584 62.7639 17.7141 59.629C19.6237 53.3639 20.8114 44.6807 20.8114 35.069C20.8114 25.4563 19.6237 16.773 17.7141 10.5089C16.7584 7.37402 15.6301 4.87145 14.3993 3.16316C13.1598 1.44413 11.8849 0.617283 10.6445 0.617188Z" stroke="#E5E7EB" stroke-width="1.2"/>
<ellipse cx="10.3542" cy="35.0696" rx="4.43619" ry="13.0989" fill="black" fill-opacity="0.18"/>
<path d="M6.6875 15.5055L8.71227 10.4393L9.40165 12.1799L8.07059 15.5055H6.6875ZM11.5045 15.5055L11.0015 14.2668H9.28024L9.90025 12.7102H10.3859L9.08517 9.47959L9.78755 7.75L12.8919 15.5055H11.5045Z" fill="#D6AD7A" opacity="0.9"/>
</g>
</svg>)svg";
    boost::replace_all(svg, "%COLOR%", color_hex);

    std::vector<char> buf(svg.begin(), svg.end());
    buf.push_back('\0');
    NSVGimage* img = nsvgParse(buf.data(), "px", 96.0f);
    if (!img)
        return wxNullBitmap;

    NSVGrasterizer* rast = nsvgCreateRasterizer();
    if (!rast) {
        nsvgDelete(img);
        return wxNullBitmap;
    }

    float scale = std::min((float)target_w / img->width, (float)target_h / img->height);
    int w = (int)(img->width * scale + 0.5f);
    int h = (int)(img->height * scale + 0.5f);
    std::vector<unsigned char> rgba(w * h * 4, 0);
    nsvgRasterize(rast, img, 0, 0, scale, rgba.data(), w, h, w * 4);
    nsvgDeleteRasterizer(rast);
    nsvgDelete(img);

    wxImage wx_img(w, h);
    wx_img.InitAlpha();
    unsigned char* dst_rgb = wx_img.GetData();
    unsigned char* dst_alpha = wx_img.GetAlpha();
    for (int i = 0; i < w * h; ++i) {
        *dst_rgb++   = rgba[i * 4 + 0];
        *dst_rgb++   = rgba[i * 4 + 1];
        *dst_rgb++   = rgba[i * 4 + 2];
        *dst_alpha++ = rgba[i * 4 + 3];
    }

    wxBitmap bmp(wx_img);
    spool_cache[key] = bmp;
    return bmp;
}

wxColour anycubic_contrasting_text(const wxColour& background)
{
    return background.GetLuminance() < 0.60 ? *wxWHITE : wxColour("#303030");
}

long long anycubic_color_distance(const wxColour& lhs, const wxColour& rhs)
{
    const long long dr = static_cast<long long>(lhs.Red()) - static_cast<long long>(rhs.Red());
    const long long dg = static_cast<long long>(lhs.Green()) - static_cast<long long>(rhs.Green());
    const long long db = static_cast<long long>(lhs.Blue()) - static_cast<long long>(rhs.Blue());
    return dr * dr + dg * dg + db * db;
}

} // namespace

AnycubicPrintHostSendDialog::AnycubicPrintHostSendDialog(const fs::path& path,
                                                         PrintHostPostUploadActions post_actions,
                                                         const wxArrayString& groups,
                                                         const wxArrayString& storage_paths,
                                                         const wxArrayString& storage_names,
                                                         bool switch_to_device_tab,
                                                         const Slic3r::AnycubicLink* host,
                                                         std::vector<Slic3r::AnycubicMaterialSlot> slots,
                                                         std::vector<Slic3r::AnycubicToolFilament> project_filaments,
                                                         bool rinkhals_moonraker)
    : PrintHostSendDialog(path, post_actions, groups, storage_paths, storage_names, switch_to_device_tab)
    , m_host(host)
    , m_slots(std::move(slots))
    , m_project_filaments(std::move(project_filaments))
    , m_rinkhals_moonraker(rinkhals_moonraker)
{
}

void AnycubicPrintHostSendDialog::init()
{
    AppConfig* app_config = wxGetApp().app_config;
    auto load_bool = [app_config](const char* key, bool fallback) {
        if (!app_config || !app_config->has("recent", key))
            return fallback;
        const std::string value = app_config->get("recent", key);
        return value == "1" || value == "true";
    };
    m_auto_leveling = load_bool(CONFIG_KEY_LEVELING, true);
    m_resonance_compensation = load_bool(CONFIG_KEY_RESONANCE, false);
    m_flow_calibration = load_bool(CONFIG_KEY_FLOW, false);
    m_timelapse = load_bool(CONFIG_KEY_TIMELAPSE, false);
    m_pre_engage_filament = load_bool(CONFIG_KEY_PRE_ENGAGE, true);
    m_skip_first_toolchange = load_bool(CONFIG_KEY_SKIP_FIRST_TOOL_CHANGE, true) && m_pre_engage_filament;
    // Current built-in default script for Skip First Tool Change.
    m_skip_first_toolchange_script = "M83\nG28 X\nG1 E40 F300\nM106 S229\nM400 P2000\nG1 X20 F15000\nG28 X";
    // Earlier built-in defaults; a saved value equal to one of these means the user
    // never customized the script, so upgrade it to the current default.
    const std::string legacy_default_v1 = "M83\nG28 X\nG1 E12 F300";
    const std::string legacy_default_v2 = "M83\nG28 X\nG1 E40 F300\nM106 S229\nM400 P2000\nG1 X20 F7000\nG28 X";
    if (app_config && app_config->has("recent", CONFIG_KEY_SKIP_FIRST_TOOL_CHANGE_SCRIPT)) {
        std::string saved_script = app_config->get("recent", CONFIG_KEY_SKIP_FIRST_TOOL_CHANGE_SCRIPT);
        if (!saved_script.empty() && saved_script != legacy_default_v1 && saved_script != legacy_default_v2)
            m_skip_first_toolchange_script = saved_script;
    }
#ifdef ORCACUBIC_DEV_BUILD
    m_save_dev_copy = load_bool(CONFIG_KEY_SAVE_DEV_COPY, false);
#else
    m_save_dev_copy = false;
#endif

    SetTitle(_L("Remote Print"));
    SetMinSize(wxSize(FromDIP(580), FromDIP(380)));

    wxString recent_path = from_u8(app_config->get("recent", "printhost_path"));
    if (!recent_path.empty() && recent_path.Last() != '/')
        recent_path += '/';
    recent_path += m_path.filename().wstring();
    txt_filename->SetValue(recent_path);

    auto* file_sizer = new wxBoxSizer(wxVERTICAL);
    auto* file_label = new wxStaticText(this, wxID_ANY, _L("Print Job Name:"));
    file_label->SetFont(::Label::Head_12);
    file_sizer->Add(file_label, 0, wxBOTTOM, FromDIP(4));
    file_sizer->Add(txt_filename, 0, wxEXPAND);
    content_sizer->Add(file_sizer, 0, wxEXPAND | wxBOTTOM, FromDIP(10));

    auto* mapping_title = new StaticLine(this, false, _L("Color Mapping"));
    content_sizer->Add(mapping_title, 0, wxEXPAND | wxBOTTOM, FromDIP(10));

    if (m_project_filaments.empty()) {
        auto* empty_text = new wxStaticText(this, wxID_ANY, _L("Slice the plate first to map project colors."));
        empty_text->SetForegroundColour(wxColour(160, 160, 160));
        content_sizer->Add(empty_text, 0, wxLEFT | wxBOTTOM, FromDIP(8));
    } else if (m_slots.empty()) {
        auto* no_ace = new wxStaticText(this, wxID_ANY, _L("Printing directly with loaded filament (No ACE unit detected)."));
        no_ace->SetForegroundColour(wxColour(160, 160, 160));
        content_sizer->Add(no_ace, 0, wxLEFT | wxBOTTOM, FromDIP(8));
    } else {
        for (const auto& tool : m_project_filaments) {
            auto* row = new wxBoxSizer(wxHORIZONTAL);

            // Left spool column with 3D spool graphic
            auto* spool_col = new wxBoxSizer(wxVERTICAL);
            wxBitmap spool_bmp = get_anycubic_spool_bitmap(tool.color, FromDIP(48), FromDIP(60));
            if (!spool_bmp.IsOk()) {
                wxBitmap* fallback = get_extruder_color_icon(tool.color, "", FromDIP(24), FromDIP(24));
                if (fallback && fallback->IsOk())
                    spool_bmp = *fallback;
            }
            if (spool_bmp.IsOk()) {
                auto* bmp_ctrl = new wxStaticBitmap(this, wxID_ANY, spool_bmp);
                spool_col->Add(bmp_ctrl, 0, wxALIGN_CENTER_HORIZONTAL);
            }

            wxString type_str = from_u8(tool.type);
            wxString label_text = type_str.empty()
                ? wxString::Format("T%d", tool.tool_id)
                : wxString::Format(wxString::FromUTF8("T%d \xe2\x80\xa2 %s"), tool.tool_id, type_str);
            auto* name_label = new wxStaticText(this, wxID_ANY, label_text);
            name_label->SetFont(::Label::Head_11);
            spool_col->Add(name_label, 0, wxALIGN_CENTER_HORIZONTAL | wxTOP, FromDIP(2));

            std::string hex_disp = tool.color;
            if (!hex_disp.empty() && hex_disp[0] != '#') hex_disp = "#" + hex_disp;
            if (hex_disp.length() > 7) hex_disp = hex_disp.substr(0, 7);
            auto* hex_label = new wxStaticText(this, wxID_ANY, from_u8(hex_disp));
            hex_label->SetFont(::Label::Body_9);
            hex_label->SetForegroundColour(wxColour(156, 163, 175));
            spool_col->Add(hex_label, 0, wxALIGN_CENTER_HORIZONTAL);

            row->Add(spool_col, 0, wxALIGN_CENTER_VERTICAL | wxRIGHT, FromDIP(16));

            auto* arrow = new wxStaticText(this, wxID_ANY, wxString::FromUTF8("\xe2\x86\x92"));
            arrow->SetFont(::Label::Head_14);
            arrow->SetForegroundColour(wxColour(156, 163, 175));
            row->Add(arrow, 0, wxALIGN_CENTER_VERTICAL | wxRIGHT, FromDIP(16));

            auto* target_box = new wxBoxSizer(wxVERTICAL);
            auto* target_label = new wxStaticText(this, wxID_ANY, _L("Target Material Slot:"));
            target_label->SetFont(::Label::Body_10);
            target_label->SetForegroundColour(wxColour(156, 163, 175));
            target_box->Add(target_label, 0, wxBOTTOM, FromDIP(4));

            auto* combo = new BitmapComboBox(this, wxID_ANY, wxEmptyString, wxDefaultPosition, wxSize(FromDIP(260), -1), 0, nullptr, wxCB_READONLY);
            for (const auto& slot : m_slots) {
                wxBitmap* icon = get_extruder_color_icon(slot.color, "", FromDIP(16), FromDIP(16));
                const bool external = slot.source == "external" || slot.source == "external_mcb";
                const wxString source_name = external ? _L("External spool") :
                    (slot.source == "rack" ? wxString::Format(_L("Slot %d"), slot.box_slot + 1) :
                     wxString::Format(_L("ACE %d Slot %d"), slot.box_id + 1, slot.box_slot + 1));
                combo->Append(wxString::Format("%s - %s", source_name, from_u8(slot.type)), icon ? *icon : wxNullBitmap);
            }
            target_box->Add(combo, 0, wxEXPAND);
            row->Add(target_box, 0, wxALIGN_CENTER_VERTICAL);

            content_sizer->Add(row, 0, wxLEFT | wxBOTTOM, FromDIP(8));
            m_slot_combos.push_back(combo);
        }
        auto_assign_mappings();
    }

    auto add_start_cancel = [this]() {
        auto* start = add_button(wxID_YES, true, _L("Start Print"));
        start->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) {
            if (validate_before_close()) {
                post_upload_action = PrintHostPostUploadAction::StartPrint;
                EndModal(wxID_OK);
            }
        });
        add_button(wxID_CANCEL, false, _L("Cancel"));
        finalize();
        SetMinSize(wxSize(FromDIP(600), -1));
        Fit();
        SetSize(wxSize(FromDIP(620), -1));
        CenterOnParent();
    };

    if (m_rinkhals_moonraker) {
        auto* note = new wxStaticText(this, wxID_ANY,
            _L("Sending through Rinkhals Moonraker: auto leveling and calibrations follow the Moonraker app settings in Rinkhals, "
               "and Rinkhals loads the first mapped slot when no filament is loaded."));
        note->SetFont(::Label::Body_10);
        note->SetForegroundColour(wxColour(156, 163, 175));
        note->Wrap(FromDIP(480));
        content_sizer->AddSpacer(FromDIP(8));
        content_sizer->Add(note, 0, wxBOTTOM, FromDIP(8));
        add_start_cancel();
        return;
    }

    auto add_toggle = [this](const char* config_key, const wxString& label, const wxString& tooltip, bool* value_ptr, std::function<void(bool)> on_change = {}) -> ::CheckBox* {
        auto* row = new wxBoxSizer(wxHORIZONTAL);
        auto* checkbox = new ::CheckBox(this);
        checkbox->SetValue(value_ptr ? *value_ptr : false);
        checkbox->SetToolTip(tooltip);

        auto apply_toggle = [checkbox, config_key, value_ptr, on_change](bool checked) {
            checkbox->SetValue(checked);
            if (value_ptr)
                *value_ptr = checked;
            AppConfig* config = wxGetApp().app_config;
            if (config && config_key) {
                config->set("recent", config_key, bool(checked));
                config->save();
            }
            if (on_change)
                on_change(checked);
        };

        checkbox->Bind(wxEVT_TOGGLEBUTTON, [checkbox, apply_toggle](wxCommandEvent& event) {
            apply_toggle(checkbox->GetValue());
            event.Skip(); // Allow CheckBox's own handler to redraw the checked/unchecked bitmap.
        });
        auto* text = new wxStaticText(this, wxID_ANY, label);
        text->SetToolTip(tooltip);
        text->Bind(wxEVT_LEFT_DOWN, [checkbox, apply_toggle](wxMouseEvent&) {
            apply_toggle(!checkbox->GetValue());
        });

        row->Add(checkbox, 0, wxRIGHT | wxALIGN_CENTER_VERTICAL, FromDIP(8));
        row->Add(text, 0, wxALIGN_CENTER_VERTICAL);
        content_sizer->Add(row, 0, wxBOTTOM, FromDIP(6));
        return checkbox;
    };

    content_sizer->AddSpacer(FromDIP(8));
    auto* purge_title = new StaticLine(this, false, _L("Purge Reducer"));
    content_sizer->Add(purge_title, 0, wxEXPAND | wxBOTTOM, FromDIP(10));

    m_cb_skip_toolchange = nullptr;
    m_cb_pre_engage = add_toggle(CONFIG_KEY_PRE_ENGAGE, _L("Pre-engage Filament"), _L("Pre-engage the toolhead active channel to the starting tool slot before printing to prevent double purging."), &m_pre_engage_filament,
               [this](bool enabled) {
                   if (!enabled) {
                       m_skip_first_toolchange = false;
                       AppConfig* config = wxGetApp().app_config;
                       if (config) {
                           config->set("recent", CONFIG_KEY_SKIP_FIRST_TOOL_CHANGE, false);
                           config->save();
                       }
                       if (m_cb_skip_toolchange) {
                           m_cb_skip_toolchange->SetValue(false);
                           m_cb_skip_toolchange->Refresh();
                       }
                       if (m_btn_edit_script)
                           m_btn_edit_script->Enable(false);
                       m_show_custom_gcode = false;
                       if (m_custom_gcode_box) {
                           m_custom_gcode_box->Show(false);
                           Layout();
                           Fit();
                       }
                   } else {
                       if (m_btn_edit_script)
                           m_btn_edit_script->Enable(m_skip_first_toolchange);
                   }
               });

    {
        auto* row = new wxBoxSizer(wxHORIZONTAL);
        m_cb_skip_toolchange = new ::CheckBox(this);
        m_cb_skip_toolchange->SetValue(m_skip_first_toolchange);
        m_cb_skip_toolchange->SetToolTip(_L("Skip the initial tool change (e.g. T0) and run a custom G-code script."));

        auto* text = new wxStaticText(this, wxID_ANY, _L("Skip First Tool Change"));
        text->SetToolTip(_L("Skip the initial tool change (e.g. T0) and run a custom G-code script."));

        m_btn_edit_script = new ScalableButton(this, wxID_ANY, "edit", wxEmptyString, wxDefaultSize, wxDefaultPosition, wxBU_EXACTFIT | wxNO_BORDER, true, 16);
        m_btn_edit_script->SetToolTip(_L("Show/hide custom G-code script editor"));
        m_btn_edit_script->SetBackgroundColour(GetBackgroundColour());
        m_btn_edit_script->Enable(m_skip_first_toolchange);

        auto apply_skip_toggle = [this](bool checked) {
            m_cb_skip_toolchange->SetValue(checked);
            m_skip_first_toolchange = checked;
            AppConfig* config = wxGetApp().app_config;
            if (config) {
                config->set("recent", CONFIG_KEY_SKIP_FIRST_TOOL_CHANGE, bool(m_skip_first_toolchange));
                config->save();
            }
            if (m_btn_edit_script)
                m_btn_edit_script->Enable(m_skip_first_toolchange);
            if (!m_skip_first_toolchange && m_show_custom_gcode) {
                m_show_custom_gcode = false;
                if (m_custom_gcode_box) {
                    m_custom_gcode_box->Show(false);
                    Layout();
                    Fit();
                }
            }
        };

        m_cb_skip_toolchange->Bind(wxEVT_TOGGLEBUTTON, [this, apply_skip_toggle](wxCommandEvent& event) {
            apply_skip_toggle(m_cb_skip_toolchange->GetValue());
            event.Skip();
        });

        text->Bind(wxEVT_LEFT_DOWN, [this, apply_skip_toggle](wxMouseEvent&) {
            apply_skip_toggle(!m_cb_skip_toolchange->GetValue());
        });

        m_btn_edit_script->Bind(wxEVT_BUTTON, [this](wxCommandEvent&) {
            if (!m_skip_first_toolchange)
                return;
            m_show_custom_gcode = !m_show_custom_gcode;
            if (m_custom_gcode_box) {
                m_custom_gcode_box->Show(m_show_custom_gcode);
                Layout();
                Fit();
            }
        });

        row->Add(m_cb_skip_toolchange, 0, wxRIGHT | wxALIGN_CENTER_VERTICAL, FromDIP(8));
        row->Add(text, 0, wxRIGHT | wxALIGN_CENTER_VERTICAL, FromDIP(6));
        row->Add(m_btn_edit_script, 0, wxALIGN_CENTER_VERTICAL);
        content_sizer->Add(row, 0, wxBOTTOM, FromDIP(6));
    }

    m_custom_gcode_box = new wxBoxSizer(wxVERTICAL);
    m_txt_custom_gcode = new wxTextCtrl(this, wxID_ANY, wxString::FromUTF8(m_skip_first_toolchange_script),
        wxDefaultPosition, wxSize(FromDIP(450), FromDIP(130)), wxTE_MULTILINE
    #ifdef _WIN32
        | wxBORDER_SIMPLE
    #endif
    );
    m_txt_custom_gcode->SetToolTip(_L("Custom G-code script to execute in place of the first tool change."));
    m_txt_custom_gcode->Bind(wxEVT_TEXT, [this](wxCommandEvent& event) {
        m_skip_first_toolchange_script = event.GetString().ToStdString();
        AppConfig* config = wxGetApp().app_config;
        if (config) {
            config->set("recent", CONFIG_KEY_SKIP_FIRST_TOOL_CHANGE_SCRIPT, m_skip_first_toolchange_script);
            config->save();
        }
        event.Skip();
    });
    m_custom_gcode_box->Add(m_txt_custom_gcode, 0, wxLEFT | wxBOTTOM | wxEXPAND, FromDIP(24));
    content_sizer->Add(m_custom_gcode_box, 0, wxEXPAND);
    m_custom_gcode_box->Show(false);

    content_sizer->AddSpacer(FromDIP(8));
    auto* calibration_title = new StaticLine(this, false, _L("Calibration"));
    content_sizer->Add(calibration_title, 0, wxEXPAND | wxBOTTOM, FromDIP(10));

    m_cb_auto_leveling = add_toggle(CONFIG_KEY_LEVELING, _L("Auto Leveling"),
        _L("Probe the build plate and build a bed mesh before this print. The printer heats the bed to the first layer temperature before probing."),
        &m_auto_leveling);

    auto* leveling_note = new wxStaticText(this, wxID_ANY,
        _L("💡 Recommended: the bed is probed at the first layer temperature. Uncheck only if your start G-code builds the bed mesh itself."));
    leveling_note->SetFont(::Label::Body_10);
    leveling_note->SetForegroundColour(wxColour(245, 158, 11));
    // Wrap inside the content column: the 620 DIP dialog less the logo column (~110), the right margin
    // and the 24 DIP indent. Wrapped wider, the text control is squeezed and its last line is cut off.
    leveling_note->Wrap(FromDIP(450));
    leveling_note->InvalidateBestSize();
    auto* note_sizer = new wxBoxSizer(wxHORIZONTAL);
    note_sizer->AddSpacer(FromDIP(24));
    note_sizer->Add(leveling_note, 0);
    content_sizer->Add(note_sizer, 0, wxBOTTOM, FromDIP(8));
    m_cb_resonance = add_toggle(CONFIG_KEY_RESONANCE, _L("Resonance Compensation"), _L("Run vibration compensation before this print."), &m_resonance_compensation);
    m_cb_flow = add_toggle(CONFIG_KEY_FLOW, _L("Flow Calibration"), _L("Calibrate extrusion flow before this print."), &m_flow_calibration);
    m_cb_timelapse = add_toggle(CONFIG_KEY_TIMELAPSE, _L("Time-lapse"), _L("Capture a time-lapse while printing. The camera must be available."), &m_timelapse);
#ifdef ORCACUBIC_DEV_BUILD
    m_cb_save_dev_copy = add_toggle(CONFIG_KEY_SAVE_DEV_COPY, _L("Save dev G-code copy"), _L("Save a copy of the final post-processed G-code/3MF to the OrcaCubic repository directory (last_remote_print_processed.gcode) for inspection."), &m_save_dev_copy);
#endif

    add_start_cancel();
}

void AnycubicPrintHostSendDialog::auto_assign_mappings()
{
    for (size_t tool_idx = 0; tool_idx < m_project_filaments.size() && tool_idx < m_slot_combos.size(); ++tool_idx) {
        int best = wxNOT_FOUND;
        long long best_distance = std::numeric_limits<long long>::max();
        const wxColour project_color(from_u8(m_project_filaments[tool_idx].color));
        for (size_t slot_idx = 0; slot_idx < m_slots.size(); ++slot_idx) {
            if (!slot_matches_tool(m_slots[slot_idx], m_project_filaments[tool_idx]))
                continue;
            const long long distance = anycubic_color_distance(project_color, wxColour(from_u8(m_slots[slot_idx].color)));
            if (best == wxNOT_FOUND || distance < best_distance) {
                best = static_cast<int>(slot_idx);
                best_distance = distance;
            }
        }
        m_slot_combos[tool_idx]->SetSelection(best);
    }
}

bool AnycubicPrintHostSendDialog::slot_matches_tool(const AnycubicMaterialSlot& slot, const AnycubicToolFilament& tool) const
{
    return slot.loaded && Slic3r::normalize_anycubic_material(slot.type) == Slic3r::normalize_anycubic_material(tool.type);
}

bool AnycubicPrintHostSendDialog::validate_before_close()
{
    if (!m_rinkhals_moonraker && m_skip_first_toolchange && !m_pre_engage_filament) {
        show_error(this, _L("Skip First Tool Change requires Pre-engage Filament."));
        return false;
    }
    if (!m_rinkhals_moonraker && m_skip_first_toolchange && m_txt_custom_gcode) {
        m_skip_first_toolchange_script = m_txt_custom_gcode->GetValue().ToStdString();
    }
    if (m_project_filaments.empty()) {
        show_error(this, _L("Slice the plate before starting a remote print."));
        return false;
    }
    if (m_slots.empty()) {
        if (m_project_filaments.size() > 1) {
            show_error(this, _L("Multi-color prints require an ACE unit or material changer. Please connect an ACE unit or slice as a single-material print."));
            return false;
        }
        return true;
    }
    if (m_slot_combos.size() != m_project_filaments.size()) {
        show_error(this, _L("The color mapping could not be created."));
        return false;
    }
    for (size_t idx = 0; idx < m_slot_combos.size(); ++idx) {
        const int selection = m_slot_combos[idx]->GetSelection();
        if (selection == wxNOT_FOUND || selection >= static_cast<int>(m_slots.size()) || !slot_matches_tool(m_slots[selection], m_project_filaments[idx])) {
            show_error(this, _L("Each project color must be mapped to a material slot containing the same material type."));
            return false;
        }
    }
    if (m_rinkhals_moonraker) {
        for (const auto& tool : m_project_filaments)
            if (tool.tool_id < 0 || tool.tool_id >= static_cast<int>(m_slots.size())) {
                show_error(this, format_wxstr(_L("Filament %1% has no matching ACE tool. Rinkhals maps at most one filament per ACE slot."), tool.tool_id + 1));
                return false;
            }
    }
    if (m_project_filaments.size() > 1) {
        for (const auto* combo : m_slot_combos) {
            const int selection = combo->GetSelection();
            if (selection >= 0 && selection < static_cast<int>(m_slots.size()) &&
                (m_slots[selection].source == "external" || m_slots[selection].source == "external_mcb")) {
                show_error(this, _L("The external spool can only be used for a single-material print."));
                return false;
            }
        }
    }
    return true;
}

std::map<std::string, std::string> AnycubicPrintHostSendDialog::extendedInfo() const
{
    if (m_rinkhals_moonraker) {
        if (m_slots.empty())
            return {};
        // MMU_TTG_MAP MAP=<gate of T0>,<gate of T1>,... with one entry per gate; unused tools keep their own gate.
        std::vector<int> ttg_map(m_slots.size());
        for (size_t gate = 0; gate < ttg_map.size(); ++gate)
            ttg_map[gate] = static_cast<int>(gate);
        for (size_t idx = 0; idx < m_project_filaments.size() && idx < m_slot_combos.size(); ++idx) {
            const int tool     = m_project_filaments[idx].tool_id;
            const int selected = m_slot_combos[idx]->GetSelection();
            if (tool >= 0 && tool < static_cast<int>(ttg_map.size()) && selected >= 0 && selected < static_cast<int>(m_slots.size()))
                ttg_map[tool] = m_slots[selected].slot_id;
        }
        std::string map;
        for (const int gate : ttg_map)
            map += (map.empty() ? "" : ",") + std::to_string(gate);
        return {{"ttg_map", map}};
    }

    if (m_slots.empty()) {
        return {
            {"ams_mapping", "[]"},
            {"use_ams", "0"},
            {"auto_leveling", m_auto_leveling ? "1" : "0"},
            {"vibration_compensation", m_resonance_compensation ? "1" : "0"},
            {"flow_calibration", m_flow_calibration ? "1" : "0"},
            {"timelapse", m_timelapse ? "1" : "0"}
        };
    }

    std::vector<int> selections;
    bool use_ams = true;
    for (const auto* combo : m_slot_combos) {
        const int selected = combo->GetSelection();
        if (selected < 0 || selected >= static_cast<int>(m_slots.size()))
            return {};
        selections.push_back(m_slots[selected].slot_id);
        if (m_slots[selected].source == "external" || m_slots[selected].source == "external_mcb")
            use_ams = false;
    }

    auto color_array = [](const std::string& raw) {
        wxColour color(from_u8(raw));
        return json::array({color.Red(), color.Green(), color.Blue(), 255});
    };

    json mapping = json::array();
    std::set<int> mapped_ams_indices;
    std::set<int> used_paint_indices;

    for (const auto& entry : build_anycubic_ams_mapping(m_project_filaments, m_slots, selections)) {
        mapped_ams_indices.insert(entry.ams_index);
        used_paint_indices.insert(entry.paint_index);
        mapping.push_back({
            {"ams_index", entry.ams_index},
            {"paint_index", entry.paint_index},
            {"material_type", entry.material_type},
            {"ams_color", color_array(entry.ams_color)},
            {"paint_color", color_array(entry.paint_color)}
        });
    }

    // Anycubic firmware (gklib) expects a complete 4-slot mapping table.
    // For single-tool or partial multi-tool prints, fill out remaining slots
    // so the array always has 4 entries and the primary tool at paint_index 0
    // points explicitly to the user's chosen physical slot.
    if (use_ams && !mapping.empty() && mapping.size() < 4) {
        int next_paint_idx = 0;
        while (used_paint_indices.count(next_paint_idx))
            ++next_paint_idx;

        for (const auto& slot : m_slots) {
            if (mapping.size() >= 4)
                break;
            if (slot.slot_id < 0 || slot.source == "external" || slot.source == "external_mcb")
                continue;
            if (mapped_ams_indices.count(slot.slot_id))
                continue;

            mapping.push_back({
                {"ams_index", slot.slot_id},
                {"paint_index", next_paint_idx},
                {"material_type", slot.type},
                {"ams_color", color_array(slot.color)},
                {"paint_color", color_array(slot.color)}
            });
            mapped_ams_indices.insert(slot.slot_id);
            used_paint_indices.insert(next_paint_idx);
            while (used_paint_indices.count(next_paint_idx))
                ++next_paint_idx;
        }
    }

    int initial_slot = 0;
    if (!mapping.empty() && mapping[0].contains("ams_index") && mapping[0]["ams_index"].is_number_integer()) {
        initial_slot = mapping[0]["ams_index"].get<int>();
    }

    return {
        {"ams_mapping", mapping.dump()},
        {"use_ams", use_ams ? "1" : "0"},
        {"auto_leveling", m_auto_leveling ? "1" : "0"},
        {"vibration_compensation", m_resonance_compensation ? "1" : "0"},
        {"flow_calibration", m_flow_calibration ? "1" : "0"},
        {"timelapse", m_timelapse ? "1" : "0"},
        {"pre_engage_filament", m_pre_engage_filament ? "1" : "0"},
        {"skip_first_toolchange", m_skip_first_toolchange ? "1" : "0"},
        {"skip_first_toolchange_script", m_skip_first_toolchange_script},
        {"save_dev_copy", m_save_dev_copy ? "1" : "0"},
        {"initial_slot", std::to_string(initial_slot)}
    };
}

void AnycubicPrintHostSendDialog::EndModal(int ret)
{
    AppConfig* config = wxGetApp().app_config;
    if (config && !m_rinkhals_moonraker) {
        if (m_txt_custom_gcode) {
            m_skip_first_toolchange_script = m_txt_custom_gcode->GetValue().ToStdString();
        }
        if (m_cb_auto_leveling) m_auto_leveling = m_cb_auto_leveling->GetValue();
        if (m_cb_resonance) m_resonance_compensation = m_cb_resonance->GetValue();
        if (m_cb_flow) m_flow_calibration = m_cb_flow->GetValue();
        if (m_cb_timelapse) m_timelapse = m_cb_timelapse->GetValue();
        if (m_cb_pre_engage) m_pre_engage_filament = m_cb_pre_engage->GetValue();
        if (m_cb_skip_toolchange) m_skip_first_toolchange = m_cb_skip_toolchange->GetValue();
#ifdef ORCACUBIC_DEV_BUILD
        if (m_cb_save_dev_copy) m_save_dev_copy = m_cb_save_dev_copy->GetValue();
#endif
        config->set("recent", CONFIG_KEY_LEVELING, bool(m_auto_leveling));
        config->set("recent", CONFIG_KEY_RESONANCE, bool(m_resonance_compensation));
        config->set("recent", CONFIG_KEY_FLOW, bool(m_flow_calibration));
        config->set("recent", CONFIG_KEY_TIMELAPSE, bool(m_timelapse));
        config->set("recent", CONFIG_KEY_PRE_ENGAGE, bool(m_pre_engage_filament));
        config->set("recent", CONFIG_KEY_SKIP_FIRST_TOOL_CHANGE, bool(m_skip_first_toolchange));
        config->set("recent", CONFIG_KEY_SKIP_FIRST_TOOL_CHANGE_SCRIPT, m_skip_first_toolchange_script);
#ifdef ORCACUBIC_DEV_BUILD
        config->set("recent", CONFIG_KEY_SAVE_DEV_COPY, bool(m_save_dev_copy));
#endif
        config->save();
    }
    PrintHostSendDialog::EndModal(ret);
}

} // namespace GUI
} // namespace Slic3r
