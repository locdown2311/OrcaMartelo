#pragma once

// "Split to fit": cut an object too large for the printer into pieces that fit, joined by
// connectors (libslic3r/FitToBed.hpp does the planning and the cutting). The dialog shows the plan
// for the chosen options; split_object_to_fit_bed() opens it and applies the result as one undo step.

#include "GUI_Utils.hpp"

#include "libslic3r/FitToBed.hpp"
#include "libslic3r/Point.hpp"
#include "libslic3r/TriangleMesh.hpp"

#include <cstddef>

class wxStaticText;
class TextInput;
class ComboBox;
class CheckBox;
class SpinInput;

namespace Slic3r {

class ModelObject;

namespace GUI {

class DialogButtons;

// Size of the printer's printable volume.
Vec3d printable_volume_size();

// True when the instance fits the printable volume in some orientation that keeps its box axis-aligned.
bool instance_fits_printable_volume(const ModelObject &object, int instance);

// Open the dialog for instance `instance` of model object `obj_idx` and split it as chosen there.
void split_object_to_fit_bed(size_t obj_idx, int instance);

class FitToBedDialog : public DPIDialog
{
public:
    FitToBedDialog(wxWindow *parent, const ModelObject &object, int instance, const Vec3d &volume);

    const FitToBedPlan         &plan() const { return m_plan; }
    const FitToBedOptions      &options() const { return m_options; }
    const FitToBedJointOptions &joint() const { return m_joint; }
    bool                        arrange() const { return m_arrange; }

protected:
    void on_dpi_changed(const wxRect &suggested_rect) override;

private:
    // Read the inputs into the options; replan when what the plan depends on changed.
    void read_inputs();
    void replan();
    void update_summary();
    void save_to_config() const;

    indexed_triangle_set m_mesh;    // the instance's world mesh
    BoundingBoxf3        m_bbox;
    FitToBedOptions      m_options;
    FitToBedJointOptions m_joint;
    FitToBedPlan         m_plan;
    bool                 m_arrange { true };

    SpinInput     *m_max_pieces { nullptr };
    ::ComboBox    *m_joint_type { nullptr };
    ::TextInput   *m_diameter { nullptr };
    ::TextInput   *m_depth { nullptr };
    ::TextInput   *m_clearance { nullptr };
    ::CheckBox    *m_place_on_cut { nullptr };
    ::CheckBox    *m_arrange_cb { nullptr };
    wxStaticText  *m_summary { nullptr };
    DialogButtons *m_buttons { nullptr };
};

} // namespace GUI
} // namespace Slic3r
