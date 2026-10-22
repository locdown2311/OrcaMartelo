#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include "libslic3r/GCode/GCodeProcessor.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/Print.hpp"

#include "test_helpers.hpp"

#include <string>

using namespace Slic3r;
using Catch::Matchers::WithinAbs;

// A 20 mm cube with one cut plane through its middle, tilted about Y: the part above the plane is a
// segment sliced along the plane's normal, printed after the root (the part below).
static void init_tilted_cube(Model &model, Print &print, const Vec3d &normal)
{
    ModelObject *object = model.add_object();
    object->name        = "tilted_cube";
    object->add_volume(Test::cube(20.), ModelVolumeType::MODEL_PART, false);
    object->add_instance()->set_offset(Vec3d(100., 100., 0.));
    object->ensure_on_bed();
    CutPlane cp;
    cp.point  = object->raw_mesh_bounding_box().center();   // object-local, like every cut plane
    cp.normal = normal.normalized();
    object->cut_planes.enabled = true;
    object->cut_planes.planes.push_back(cp);

    DynamicPrintConfig config = DynamicPrintConfig::full_print_config();
    config.set_deserialize_strict({
        { "enable_nonplanar", "1" },
        { "print_sequence",   "by object" },
        { "layer_height",     "0.3" },
    });
    print.apply(model, config);
}

TEST_CASE("A tilted segment announces its build direction to the G-code viewer", "[Multiplanar]")
{
    const Vec3d normal = Vec3d(0.3, 0., 1.).normalized();
    Model model;
    Print print;
    init_tilted_cube(model, print, normal);
    REQUIRE(print.objects().size() == 2);

    GCodeProcessorResult result;
    const std::string gcode = Test::gcode(print, &result);

    CHECK(gcode.find(";" + GCodeProcessor::Tool_Axis_Tag) != std::string::npos);
    // The palette holds +Z (the root) and the segment's direction.
    REQUIRE(result.tool_axes.size() == 2);
    const Vec3f axis = result.tool_axes[1];
    CHECK_THAT(axis.x(), WithinAbs(normal.x(), 1e-4));
    CHECK_THAT(axis.y(), WithinAbs(normal.y(), 1e-4));
    CHECK_THAT(axis.z(), WithinAbs(normal.z(), 1e-4));

    size_t tilted_extrusions = 0, planar_extrusions = 0;
    for (const GCodeProcessorResult::MoveVertex &move : result.moves)
        if (move.type == EMoveType::Extrude)
            ++ (move.tool_axis_id == 1 ? tilted_extrusions : planar_extrusions);
    CHECK(tilted_extrusions > 0);
    CHECK(planar_extrusions > 0);
}

TEST_CASE("A horizontal cut plane leaves the G-code viewer at +Z", "[Multiplanar]")
{
    Model model;
    Print print;
    init_tilted_cube(model, print, Vec3d::UnitZ());
    REQUIRE(print.objects().size() == 2);

    GCodeProcessorResult result;
    const std::string gcode = Test::gcode(print, &result);

    CHECK(gcode.find(";" + GCodeProcessor::Tool_Axis_Tag) == std::string::npos);
    CHECK(result.tool_axes.size() == 1);
}
