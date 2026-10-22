#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include "libslic3r/FitToBed.hpp"
#include "libslic3r/TriangleMesh.hpp"

#include <algorithm>

using namespace Slic3r;
using Catch::Matchers::WithinAbs;

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
