#include "FitToBedDialog.hpp"

#include "GUI.hpp"
#include "GUI_App.hpp"
#include "I18N.hpp"
#include "format.hpp"
#include "MainFrame.hpp"
#include "MsgDialog.hpp"
#include "Plater.hpp"
#include "Jobs/ArrangeJob.hpp"
#include "Jobs/Job.hpp"
#include "Jobs/Worker.hpp"
#include "Widgets/CheckBox.hpp"
#include "Widgets/ComboBox.hpp"
#include "Widgets/DialogButtons.hpp"
#include "Widgets/Label.hpp"
#include "Widgets/SpinInput.hpp"
#include "Widgets/TextInput.hpp"

#include "libslic3r/AppConfig.hpp"
#include "libslic3r/BoundingBox.hpp"
#include "libslic3r/BuildVolume.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/ObjectID.hpp"
#include "libslic3r/libslic3r.h"

#include <wx/busyinfo.h>
#include <wx/button.h>
#include <wx/sizer.h>
#include <wx/stattext.h>
#include <wx/string.h>
#include <wx/textctrl.h>
#include <wx/utils.h>

#include <algorithm>
#include <memory>
#include <string>

namespace Slic3r { namespace GUI {

Vec3d printable_volume_size()
{
    return wxGetApp().plater()->build_volume().bounding_volume().size();
}

bool instance_fits_printable_volume(const ModelObject &object, int instance)
{
    if (instance < 0 || instance >= int(object.instances.size()))
        return true;
    return fits_printable_volume(object.instance_bounding_box(size_t(instance)).size(), printable_volume_size());
}

namespace {

// The app config keys keeping the dialog's choices between uses.
constexpr const char *CFG_MAX_PIECES = "fit_to_bed_max_pieces";
constexpr const char *CFG_JOINT      = "fit_to_bed_joint";
constexpr const char *CFG_DIAMETER   = "fit_to_bed_connector_diameter";
constexpr const char *CFG_DEPTH      = "fit_to_bed_connector_depth";
constexpr const char *CFG_CLEARANCE  = "fit_to_bed_clearance";
constexpr const char *CFG_PLACE      = "fit_to_bed_place_on_cut_face";
constexpr const char *CFG_ARRANGE    = "fit_to_bed_arrange";

double config_double(const char *key, double fallback)
{
    const std::string value = wxGetApp().app_config->get(key);
    double            out   = fallback;
    return ! value.empty() && wxString(value).ToCDouble(&out) ? out : fallback;
}

bool config_bool(const char *key, bool fallback)
{
    const std::string value = wxGetApp().app_config->get(key);
    return value.empty() ? fallback : value == "1" || value == "true";
}

// A length typed in mm, with either decimal separator; `fallback` when it does not parse or is out of range.
double parse_mm(const ::TextInput *input, double fallback, double min, double max)
{
    wxString text = input->GetTextCtrl()->GetValue();
    text.Replace(",", ".");
    double value = 0.;
    return text.Trim().Trim(false).ToCDouble(&value) && value >= min && value <= max ? value : fallback;
}

wxString format_mm(double value) { return wxString::FromCDouble(value); }

} // namespace

FitToBedDialog::FitToBedDialog(wxWindow *parent, const ModelObject &object, int instance, const Vec3d &volume)
    : DPIDialog(parent ? parent : static_cast<wxWindow *>(wxGetApp().mainframe), wxID_ANY, _L("Split to fit"), wxDefaultPosition, wxDefaultSize,
                wxCAPTION | wxCLOSE_BOX)
{
    SetBackgroundColour(*wxWHITE);
    SetFont(Label::Body_14);

    TriangleMesh mesh = object.raw_mesh();
    mesh.transform(object.instances[instance]->get_matrix());
    m_mesh = std::move(mesh.its);
    m_bbox = object.instance_bounding_box(size_t(instance));

    m_options.volume_size      = volume;
    m_options.max_pieces       = std::clamp(int(config_double(CFG_MAX_PIECES, 16.)), 2, 64);
    m_options.connector_radius = 0.5 * config_double(CFG_DIAMETER, 5.);
    const std::string joint    = wxGetApp().app_config->get(CFG_JOINT);
    m_joint.type               = joint == "dowel" ? FitToBedJoint::Dowel : joint == "none" ? FitToBedJoint::None : FitToBedJoint::Plug;
    m_joint.depth              = config_double(CFG_DEPTH, 6.);
    m_joint.clearance          = config_double(CFG_CLEARANCE, 0.2);
    m_joint.depth_clearance    = m_joint.clearance;
    m_joint.keep_paint         = wxGetApp().app_config->get_bool("keep_painting");
    m_joint.place_on_cut_face  = config_bool(CFG_PLACE, true);
    m_arrange                  = config_bool(CFG_ARRANGE, true);

    auto *sizer = new wxBoxSizer(wxVERTICAL);

    const Vec3d size = m_bbox.size();
    auto *intro = new wxStaticText(this, wxID_ANY,
        // TRN %1% to %3%: the object's size, %4% to %6%: the printer's printable volume, all in mm.
        format_wxstr(_L("The object measures %1% × %2% × %3% mm and the printable volume %4% × %5% × %6% mm. "
                        "It can be cut into pieces that fit, joined by connectors."),
                     int(std::round(size.x())), int(std::round(size.y())), int(std::round(size.z())),
                     int(std::round(volume.x())), int(std::round(volume.y())), int(std::round(volume.z()))));
    intro->Wrap(FromDIP(420));
    sizer->Add(intro, 0, wxEXPAND | wxALL, FromDIP(10));

    auto *grid = new wxFlexGridSizer(2, FromDIP(6), FromDIP(20));
    grid->AddGrowableCol(1);
    auto add_row = [this, grid](const wxString &label, wxWindow *control) {
        grid->Add(new wxStaticText(this, wxID_ANY, label), 0, wxALIGN_CENTER_VERTICAL);
        grid->Add(control, 0, wxALIGN_CENTER_VERTICAL);
    };
    const wxSize input_size(FromDIP(120), -1);

    m_max_pieces = new SpinInput(this, wxEmptyString, "", wxDefaultPosition, input_size, wxSP_ARROW_KEYS, 2, 64, m_options.max_pieces);
    add_row(_L("Maximum number of pieces"), m_max_pieces);

    m_joint_type = new ::ComboBox(this, wxID_ANY, wxEmptyString, wxDefaultPosition, wxSize(FromDIP(220), -1), 0, nullptr, wxCB_READONLY);
    m_joint_type->Append(_L("Plug (peg and hole)"));
    m_joint_type->Append(_L("Dowel (separate pin)"));
    m_joint_type->Append(_L("None (glue the faces)"));
    m_joint_type->SetSelection(m_joint.type == FitToBedJoint::Plug ? 0 : m_joint.type == FitToBedJoint::Dowel ? 1 : 2);
    add_row(_L("Connector"), m_joint_type);

    auto make_mm_input = [this, &input_size](double value) {
        auto *input = new ::TextInput(this, format_mm(value), _L("mm"), "", wxDefaultPosition, input_size, wxTE_PROCESS_ENTER);
        input->Bind(wxEVT_TEXT_ENTER, [this](wxCommandEvent &) { read_inputs(); });
        input->Bind(wxEVT_KILL_FOCUS, [this](wxFocusEvent &e) { read_inputs(); e.Skip(); });
        return input;
    };
    m_diameter = make_mm_input(2. * m_options.connector_radius);
    add_row(_L("Connector diameter"), m_diameter);
    m_depth = make_mm_input(m_joint.depth);
    add_row(_L("Connector depth"), m_depth);
    m_clearance = make_mm_input(m_joint.clearance);
    m_clearance->SetToolTip(_L("Extra room in the holes, on the diameter and on the depth, so the connectors go in."));
    add_row(_L("Clearance"), m_clearance);

    m_place_on_cut = new ::CheckBox(this);
    m_place_on_cut->SetValue(m_joint.place_on_cut_face);
    m_place_on_cut->SetToolTip(_L("Stand each piece on its largest flat cut face, when it fits and is not too tall for it."));
    add_row(_L("Place pieces on a cut face"), m_place_on_cut);

    m_arrange_cb = new ::CheckBox(this);
    m_arrange_cb->SetValue(m_arrange);
    add_row(_L("Arrange the plates afterwards"), m_arrange_cb);

    sizer->Add(grid, 0, wxEXPAND | wxLEFT | wxRIGHT, FromDIP(10));

    m_summary = new wxStaticText(this, wxID_ANY, wxEmptyString);
    sizer->Add(m_summary, 0, wxEXPAND | wxALL, FromDIP(10));

    m_max_pieces->Bind(wxEVT_SPINCTRL, [this](wxCommandEvent &) { read_inputs(); });
    m_joint_type->Bind(wxEVT_COMBOBOX, [this](wxCommandEvent &) { read_inputs(); });

    m_buttons = new DialogButtons(this, {"OK", "Cancel"});
    m_buttons->GetOK()->SetLabel(_L("Split"));
    m_buttons->GetOK()->Bind(wxEVT_BUTTON, [this](wxCommandEvent &) {
        read_inputs();
        if (m_plan.status != FitToBedStatus::Ok)
            return;
        m_joint.place_on_cut_face = m_place_on_cut->GetValue();
        m_arrange                 = m_arrange_cb->GetValue();
        save_to_config();
        EndModal(wxID_OK);
    });
    m_buttons->GetCANCEL()->Bind(wxEVT_BUTTON, [this](wxCommandEvent &) { EndModal(wxID_CANCEL); });
    sizer->Add(m_buttons, 0, wxEXPAND);

    replan();

    SetSizerAndFit(sizer);
    CenterOnParent();
    wxGetApp().UpdateDlgDarkUI(this);
}

void FitToBedDialog::read_inputs()
{
    const int    max_pieces = std::clamp(m_max_pieces->GetValue(), 2, 64);
    const double radius     = 0.5 * parse_mm(m_diameter, 2. * m_options.connector_radius, 1., 50.);
    const int    joint      = m_joint_type->GetSelection();
    m_joint.type            = joint == 1 ? FitToBedJoint::Dowel : joint == 2 ? FitToBedJoint::None : FitToBedJoint::Plug;
    m_joint.depth           = parse_mm(m_depth, m_joint.depth, 1., 100.);
    m_joint.clearance       = parse_mm(m_clearance, m_joint.clearance, 0., 2.);
    m_joint.depth_clearance = m_joint.clearance;
    // Show what was taken, a rejected entry included.
    m_diameter->GetTextCtrl()->ChangeValue(format_mm(2. * radius));
    m_depth->GetTextCtrl()->ChangeValue(format_mm(m_joint.depth));
    m_clearance->GetTextCtrl()->ChangeValue(format_mm(m_joint.clearance));

    const bool enable = m_joint.type != FitToBedJoint::None;
    m_diameter->Enable(enable);
    m_depth->Enable(enable);
    m_clearance->Enable(enable);

    if (max_pieces != m_options.max_pieces || radius != m_options.connector_radius) {
        m_options.max_pieces       = max_pieces;
        m_options.connector_radius = radius;
        replan();
    } else
        update_summary();
}

void FitToBedDialog::replan()
{
    wxBusyCursor wait;
    m_plan = plan_fit_to_bed(m_mesh, m_options);
    update_summary();
}

void FitToBedDialog::update_summary()
{
    wxString text;
    switch (m_plan.status) {
    case FitToBedStatus::AlreadyFits:
        text = _L("The object already fits the printable volume.");
        break;
    case FitToBedStatus::TooManyPieces:
        text = format_wxstr(_L_PLURAL("The object needs more than %1% piece. Raise the limit or scale the object down.",
                                      "The object needs more than %1% pieces. Raise the limit or scale the object down.", m_options.max_pieces),
                            m_options.max_pieces);
        break;
    case FitToBedStatus::NoVolume:
        text = _L("The object cannot be split to fit this printer.");
        break;
    case FitToBedStatus::Ok: {
        const size_t pieces = m_plan.leaves().size();
        text = format_wxstr(_L_PLURAL("The object will be cut into %1% piece:", "The object will be cut into %1% pieces:", pieces), pieces);
        static const char axes[] = { 'X', 'Y', 'Z' };
        for (size_t i = 0; i < m_plan.cuts.size(); ++ i) {
            const FitToBedCut &cut   = m_plan.cuts[i];
            const wxString     axis  = wxString(axes[cut.axis]);
            const double       at    = cut.position - m_bbox.min(cut.axis);
            const size_t       count = m_joint.type == FitToBedJoint::None ? 0 : cut.connectors.size();
            text += "\n";
            if (cut.section_area <= 0.)
                // TRN %1%: cut number, %2%: axis letter, %3%: distance in mm from the object's side.
                text += format_wxstr(_L("Cut %1% across %2%, %3% mm from the object's side: through a gap, nothing to join"), i + 1, axis, int(std::round(at)));
            else
                // TRN %1%: cut number, %2%: axis letter, %3%: distance in mm from the object's side, %4%: face area, %5%: number of connectors.
                text += format_wxstr(_L_PLURAL("Cut %1% across %2%, %3% mm from the object's side: a face of %4% cm² and %5% connector",
                                               "Cut %1% across %2%, %3% mm from the object's side: a face of %4% cm² and %5% connectors", count),
                                     i + 1, axis, int(std::round(at)), int(std::round(cut.section_area / 100.)), count);
        }
        break;
    }
    }
    m_summary->SetLabel(text);
    m_summary->Wrap(FromDIP(420));
    m_buttons->GetOK()->Enable(m_plan.status == FitToBedStatus::Ok);
    if (GetSizer() != nullptr) {
        Layout();
        GetSizer()->SetSizeHints(this);
    }
}

void FitToBedDialog::save_to_config() const
{
    AppConfig &config = *wxGetApp().app_config;
    config.set(CFG_MAX_PIECES, std::to_string(m_options.max_pieces));
    config.set(CFG_JOINT, m_joint.type == FitToBedJoint::Dowel ? "dowel" : m_joint.type == FitToBedJoint::None ? "none" : "plug");
    config.set(CFG_DIAMETER, into_u8(format_mm(2. * m_options.connector_radius)));
    config.set(CFG_DEPTH, into_u8(format_mm(m_joint.depth)));
    config.set(CFG_CLEARANCE, into_u8(format_mm(m_joint.clearance)));
    config.set(CFG_PLACE, m_joint.place_on_cut_face ? "1" : "0");
    config.set(CFG_ARRANGE, m_arrange ? "1" : "0");
}

void FitToBedDialog::on_dpi_changed(const wxRect &)
{
    m_max_pieces->Rescale();
    m_joint_type->Rescale();
    m_diameter->Rescale();
    m_depth->Rescale();
    m_clearance->Rescale();
    m_place_on_cut->Rescale();
    m_arrange_cb->Rescale();
    GetSizer()->SetSizeHints(this);
    Refresh();
}

void split_object_to_fit_bed(size_t obj_idx, int instance)
{
    Plater *plater = wxGetApp().plater();
    Model  &model  = plater->model();
    if (obj_idx >= model.objects.size())
        return;
    ModelObject *object = model.objects[obj_idx];
    if (instance < 0 || instance >= int(object->instances.size()))
        instance = 0;

    FitToBedDialog dlg(plater, *object, instance, printable_volume_size());
    if (dlg.ShowModal() != wxID_OK)
        return;

    Model           pieces_model;
    ModelObjectPtrs pieces;
    {
        wxBusyCursor wait;
        pieces = apply_fit_to_bed_plan(*object, instance, dlg.plan(), dlg.options(), dlg.joint(), pieces_model);
    }
    if (pieces.empty()) {
        show_error(plater, _L("The object could not be split."));
        return;
    }

    {
        Plater::TakeSnapshot snapshot(plater, _u8L("Split to fit"));
        const CutObjectBase  cut_id = pieces.front()->cut_id;
        plater->apply_cut_object_to_model(obj_idx, pieces);
        // Objects cut from the same original earlier keep agreeing with the pieces on the cut id.
        for (ModelObject *other : plater->model().objects)
            if (other->is_cut() && other->cut_id.has_same_id(cut_id) && ! other->cut_id.is_equal(cut_id))
                other->cut_id.copy(cut_id);
        // Arranging is part of the same undo step: no snapshot of its own.
        if (dlg.arrange() && plater->get_ui_job_worker().is_idle()) {
            plater->set_prepare_state(Job::PREPARE_STATE_DEFAULT);
            replace_job(plater->get_ui_job_worker(), std::make_unique<ArrangeJob>());
        }
    }
}

}} // namespace Slic3r::GUI
