#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include "libslic3r/BoundingBox.hpp"
#include "libslic3r/Point.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/WipeTowerPlacement.hpp"

#include <optional>

using namespace Slic3r;
using Catch::Matchers::WithinAbs;

// A 255 x 255 bed with a 40 x 30 part in the middle, 20 mm high, and a 30 x 30 tower with a 3 mm brim.
static WipeTowerPlacementInput make_input(NozzleWiperPosition wiper)
{
    WipeTowerPlacementInput in;
    in.bed          = BoundingBoxf(Vec2d(0., 0.), Vec2d(255., 255.));
    in.parts        = BoundingBoxf(Vec2d(107.5, 112.5), Vec2d(147.5, 142.5));
    in.parts_height = 20.;
    in.tower_size   = Vec2d(30., 30.);
    in.tower_brim   = 3.;
    in.wiper        = wiper;
    in.gap          = 15.;
    in.current      = Vec2d(10., 10.);
    return in;
}

TEST_CASE("The tower goes beside the parts on the wiper's side", "[WipeTowerPlacement]") {
    SECTION("a wiper at the right end of X: right of the parts, centred on them in Y") {
        const std::optional<Vec2d> pos = place_wipe_tower_beside_parts(make_input(NozzleWiperPosition::Right));
        REQUIRE(pos);
        CHECK_THAT(pos->x(), WithinAbs(147.5 + 15. + 3., 1e-9));
        CHECK_THAT(pos->y(), WithinAbs(127.5 - 15., 1e-9));
    }
    SECTION("at the left end: left of them") {
        const std::optional<Vec2d> pos = place_wipe_tower_beside_parts(make_input(NozzleWiperPosition::Left));
        REQUIRE(pos);
        CHECK_THAT(pos->x(), WithinAbs(107.5 - 15. - 3. - 30., 1e-9));
    }
    SECTION("at the back right: behind them, at their right end") {
        const std::optional<Vec2d> pos = place_wipe_tower_beside_parts(make_input(NozzleWiperPosition::RearRight));
        REQUIRE(pos);
        CHECK_THAT(pos->y(), WithinAbs(142.5 + 15. + 3., 1e-9));
        CHECK_THAT(pos->x(), WithinAbs(147.5 - 30., 1e-9));
    }
    SECTION("with no wiper set, it stays on the side it is on, at the gap") {
        WipeTowerPlacementInput in = make_input(NozzleWiperPosition::None);
        in.current = Vec2d(120., 220.); // behind the parts
        const std::optional<Vec2d> pos = place_wipe_tower_beside_parts(in);
        REQUIRE(pos);
        CHECK_THAT(pos->y(), WithinAbs(142.5 + 15. + 3., 1e-9));
    }
}

TEST_CASE("Without room on the wiper's side the tower goes to the next side", "[WipeTowerPlacement]") {
    WipeTowerPlacementInput in = make_input(NozzleWiperPosition::Right);
    in.parts = BoundingBoxf(Vec2d(150., 100.), Vec2d(240., 140.)); // too near the right edge
    const std::optional<Vec2d> pos = place_wipe_tower_beside_parts(in);
    REQUIRE(pos);
    // Behind the parts, at their right end.
    CHECK_THAT(pos->y(), WithinAbs(140. + 15. + 3., 1e-9));
    CHECK_THAT(pos->x(), WithinAbs(240. - 30., 1e-9));

    // The tower always stays on the bed with its brim.
    in.parts = BoundingBoxf(Vec2d(5., 5.), Vec2d(250., 250.));
    CHECK_FALSE(place_wipe_tower_beside_parts(in));
}

TEST_CASE("A bed slinger braces a tall part from near the bed's middle in Y", "[WipeTowerPlacement]") {
    WipeTowerPlacementInput in = make_input(NozzleWiperPosition::Right);
    in.parts       = BoundingBoxf(Vec2d(80., 60.), Vec2d(140., 240.)); // long in Y, its centre at 150
    in.bed_slinger = true;
    in.parts_height = 100.;
    const std::optional<Vec2d> tall = place_wipe_tower_beside_parts(in);
    REQUIRE(tall);
    // The bed's centre, 127.5, is within the part's extent: the tower is centred there.
    CHECK_THAT(tall->y() + 15., WithinAbs(127.5, 1e-9));
    // A low part keeps it centred on the part.
    in.parts_height = 10.;
    const std::optional<Vec2d> low = place_wipe_tower_beside_parts(in);
    REQUIRE(low);
    CHECK_THAT(low->y() + 15., WithinAbs(150., 1e-9));
}
