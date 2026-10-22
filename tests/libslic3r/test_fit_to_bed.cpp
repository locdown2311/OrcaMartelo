#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include "libslic3r/FitToBed.hpp"
#include "libslic3r/Model.hpp"
#include "libslic3r/TriangleMesh.hpp"

#include <algorithm>
#include <cmath>

using namespace Slic3r;
using Catch::Matchers::WithinAbs;
using Catch::Matchers::WithinRel;

// The Kobra 3 V2's printable volume; the default margin keeps 2 mm clear of every side.
static FitToBedOptions kobra_options()
{
    FitToBedOptions options;
    options.volume_size = Vec3d(250., 250., 260.);
    return options;
}

static indexed_triangle_set box(double x, double y, double z)
{
    return its_make_cube(x, y, z);
}

static void require_every_leaf_fits(const FitToBedPlan &plan, const FitToBedOptions &options)
{
    const Vec3d volume = options.volume_size - Vec3d(2. * options.margin, 2. * options.margin, options.margin);
    for (int leaf : plan.leaves()) {
        INFO("piece " << leaf << " size " << plan.pieces[leaf].bbox.size().transpose());
        CHECK(plan.pieces[leaf].fits);
        CHECK(fits_printable_volume(plan.pieces[leaf].bbox.size(), volume));
    }
}

TEST_CASE("A box fits the volume when any of its faces can lie on the bed", "[FitToBed]")
{
    const Vec3d volume(250., 250., 260.);
    CHECK(fits_printable_volume(Vec3d(100., 100., 100.), volume));
    // Too tall standing up, fine lying on its side.
    CHECK(fits_printable_volume(Vec3d(50., 50., 300.), Vec3d(250., 320., 260.)));
    CHECK_FALSE(fits_printable_volume(Vec3d(300., 300., 300.), volume));
}

TEST_CASE("A model that already fits is not cut", "[FitToBed]")
{
    const FitToBedPlan plan = plan_fit_to_bed(box(100., 100., 100.), kobra_options());
    CHECK(plan.status == FitToBedStatus::AlreadyFits);
    CHECK(plan.cuts.empty());
    CHECK(plan.leaves().size() == 1);
}

TEST_CASE("A box too long in one direction is cut once across that direction", "[FitToBed]")
{
    // 400 mm along X: two pieces of at most 246 mm, one cut perpendicular to X.
    const FitToBedOptions options = kobra_options();
    const FitToBedPlan    plan    = plan_fit_to_bed(box(400., 100., 50.), options);
    REQUIRE(plan.status == FitToBedStatus::Ok);
    REQUIRE(plan.cuts.size() == 1);
    CHECK(plan.cuts.front().axis == 0);
    CHECK(plan.leaves().size() == 2);
    require_every_leaf_fits(plan, options);
    // A solid box cut across has a 100 x 50 mm face to join.
    CHECK_THAT(plan.cuts.front().section_area, WithinAbs(5000., 50.));
    CHECK_FALSE(plan.cuts.front().connectors.empty());
}

TEST_CASE("A box too large in two directions ends in pieces that all fit", "[FitToBed]")
{
    const FitToBedOptions options = kobra_options();
    const FitToBedPlan    plan    = plan_fit_to_bed(box(400., 400., 50.), options);
    REQUIRE(plan.status == FitToBedStatus::Ok);
    // 400 x 400 needs two pieces along each of X and Y.
    CHECK(plan.leaves().size() == 4);
    require_every_leaf_fits(plan, options);
}

TEST_CASE("A model taller than the volume is laid down instead of being cut", "[FitToBed]")
{
    // A wide, low printer: 250 mm standing up does not fit 200 mm of height, lying down it does.
    FitToBedOptions options;
    options.volume_size = Vec3d(300., 300., 200.);
    const FitToBedPlan plan = plan_fit_to_bed(box(40., 40., 250.), options);
    CHECK(plan.status == FitToBedStatus::AlreadyFits);
}

TEST_CASE("A cut through a gap needs no connectors", "[FitToBed]")
{
    // Two 150 mm blocks with a 20 mm gap between them along X: the cheapest cut is the gap.
    indexed_triangle_set mesh = box(150., 80., 40.);
    indexed_triangle_set second = box(150., 80., 40.);
    for (stl_vertex &v : second.vertices)
        v.x() += 170.f;
    its_merge(mesh, second);

    const FitToBedOptions options = kobra_options();
    const FitToBedPlan    plan    = plan_fit_to_bed(mesh, options);
    REQUIRE(plan.status == FitToBedStatus::Ok);
    REQUIRE(plan.cuts.size() == 1);
    CHECK_THAT(plan.cuts.front().section_area, WithinAbs(0., 1e-6));
    CHECK(plan.cuts.front().connectors.empty());
    require_every_leaf_fits(plan, options);
}

TEST_CASE("Connectors stay inside the cut face with room for their wall", "[FitToBed]")
{
    const FitToBedOptions options = kobra_options();
    const FitToBedPlan    plan    = plan_fit_to_bed(box(400., 100., 50.), options);
    REQUIRE(plan.cuts.size() == 1);
    const FitToBedCut &cut   = plan.cuts.front();
    const double       inset = options.connector_radius + options.connector_wall;
    for (const Vec3d &c : cut.connectors) {
        CHECK_THAT(c.x(), WithinAbs(cut.position, 1e-3));
        CHECK(c.y() >= inset - 1e-3);
        CHECK(c.y() <= 100. - inset + 1e-3);
        CHECK(c.z() >= inset - 1e-3);
        CHECK(c.z() <= 50. - inset + 1e-3);
    }
}

TEST_CASE("Planning stops at the piece limit", "[FitToBed]")
{
    FitToBedOptions options = kobra_options();
    options.max_pieces = 2;
    const FitToBedPlan plan = plan_fit_to_bed(box(800., 100., 50.), options);
    CHECK(plan.status == FitToBedStatus::TooManyPieces);
}

TEST_CASE("Connectors keep clear of the later cuts across their face", "[FitToBed]")
{
    const FitToBedOptions options = kobra_options();
    const FitToBedPlan    plan    = plan_fit_to_bed(box(400., 400., 50.), options);
    REQUIRE(plan.status == FitToBedStatus::Ok);
    const double inset = options.connector_radius + options.connector_wall;
    size_t checked = 0;
    for (size_t i = 0; i < plan.cuts.size(); ++ i)
        for (size_t j = i + 1; j < plan.cuts.size(); ++ j) {
            const FitToBedCut   &later = plan.cuts[j];
            const BoundingBoxf3 &bb    = plan.pieces[later.piece].bbox;
            if (later.axis == plan.cuts[i].axis)
                continue;
            for (const Vec3d &c : plan.cuts[i].connectors) {
                // Only a later cut whose plane reaches the connector.
                bool reaches = true;
                for (int a = 0; a < 3; ++ a)
                    if (a != later.axis && (c(a) < bb.min(a) - 1e-3 || c(a) > bb.max(a) + 1e-3))
                        reaches = false;
                if (! reaches)
                    continue;
                INFO("cut " << i << " connector " << c.transpose() << ", later cut " << j << " at " << later.position);
                CHECK(std::abs(c(later.axis) - later.position) >= inset - 1e-3);
                ++ checked;
            }
        }
    // 400 x 400 is cut across one axis, then each half across the other, right through the first face.
    CHECK(checked > 0);
}

// A model object made of one box, its single instance at `offset`.
static ModelObject *add_box_object(Model &model, const Vec3d &size, const Vec3d &offset)
{
    ModelObject *object = model.add_object();
    object->name        = "box";
    object->add_volume(TriangleMesh(box(size.x(), size.y(), size.z())));
    object->add_instance()->set_offset(offset);
    object->ensure_on_bed();
    return object;
}

static indexed_triangle_set world_mesh(const ModelObject &object)
{
    TriangleMesh mesh = object.raw_mesh();
    mesh.transform(object.instances.front()->get_matrix());
    return mesh.its;
}

static double solid_volume(const ModelObject &object)
{
    double v = 0.;
    for (const ModelVolume *volume : object.volumes)
        if (volume->is_model_part() && ! volume->is_cut_connector()) {
            v += volume->mesh().stats().volume;
            INFO(object.name << " / " << volume->name);
            CHECK(its_num_open_edges(volume->mesh().its) == 0);
        }
    return v;
}

TEST_CASE("Applying a plan cuts the object into pieces that fit, with pegs and holes", "[FitToBed]")
{
    const FitToBedOptions options = kobra_options();
    Model                 model;
    ModelObject          *object = add_box_object(model, Vec3d(400., 100., 50.), Vec3d(150., 120., 0.));
    const FitToBedPlan    plan   = plan_fit_to_bed(world_mesh(*object), options);
    REQUIRE(plan.status == FitToBedStatus::Ok);
    REQUIRE(plan.cuts.size() == 1);
    const size_t connectors = plan.cuts.front().connectors.size();
    REQUIRE(connectors > 0);

    Model                 out;
    FitToBedJointOptions  joint;
    joint.type = FitToBedJoint::Plug;
    const ModelObjectPtrs pieces = apply_fit_to_bed_plan(*object, 0, plan, options, joint, out);
    REQUIRE(pieces.size() == 2);

    const Vec3d volume = options.volume_size - Vec3d(2. * options.margin, 2. * options.margin, options.margin);
    double      total  = 0.;
    size_t      pegs = 0, holes = 0;
    for (const ModelObject *piece : pieces) {
        const BoundingBoxf3 bb = piece->instance_bounding_box(0);
        INFO(piece->name << " size " << bb.size().transpose());
        CHECK(bb.size().x() <= volume.x() + 1e-3);
        CHECK(bb.size().y() <= volume.y() + 1e-3);
        CHECK(bb.size().z() <= volume.z() + 1e-3);
        CHECK_THAT(bb.min.z(), WithinAbs(0., 1e-3));
        CHECK(piece->is_cut());
        total += solid_volume(*piece);
        for (const ModelVolume *v : piece->volumes)
            if (v->is_cut_connector())
                ++ (v->is_model_part() ? pegs : holes);
    }
    // Nothing of the box is lost; one piece has the pegs, the other the holes.
    CHECK_THAT(total, WithinRel(400. * 100. * 50., 1e-3));
    CHECK(pegs == connectors);
    CHECK(holes == connectors);
    CHECK(pieces[0]->cut_id.is_equal(pieces[1]->cut_id));
    // Leaves come lower side first. The piece with the pegs (sticking out along X) lies as it was;
    // the one with the holes stands on its 100 x 50 cut face, being about 200 mm long.
    CHECK_THAT(pieces[0]->instance_bounding_box(0).size().z(), WithinAbs(50., 1e-3));
    CHECK(pieces[1]->instance_bounding_box(0).size().z() > 150.);
}

TEST_CASE("Applying a plan with dowels adds one dowel per connector", "[FitToBed]")
{
    const FitToBedOptions options = kobra_options();
    Model                 model;
    ModelObject          *object = add_box_object(model, Vec3d(400., 400., 50.), Vec3d(100., 100., 0.));
    const FitToBedPlan    plan   = plan_fit_to_bed(world_mesh(*object), options);
    REQUIRE(plan.status == FitToBedStatus::Ok);
    size_t connectors = 0;
    for (const FitToBedCut &cut : plan.cuts)
        connectors += cut.connectors.size();

    Model                 out;
    FitToBedJointOptions  joint;
    joint.type = FitToBedJoint::Dowel;
    const ModelObjectPtrs objects = apply_fit_to_bed_plan(*object, 0, plan, options, joint, out);
    REQUIRE(objects.size() == plan.leaves().size() + connectors);

    const Vec3d volume = options.volume_size - Vec3d(2. * options.margin, 2. * options.margin, options.margin);
    double      total  = 0.;
    for (size_t i = 0; i < plan.leaves().size(); ++ i) {
        const BoundingBoxf3 bb = objects[i]->instance_bounding_box(0);
        INFO(objects[i]->name << " size " << bb.size().transpose());
        CHECK(fits_printable_volume(bb.size(), volume));
        CHECK(bb.size().x() <= volume.x() + 1e-3);
        CHECK(bb.size().y() <= volume.y() + 1e-3);
        total += solid_volume(*objects[i]);
    }
    CHECK_THAT(total, WithinRel(400. * 400. * 50., 1e-3));
}
