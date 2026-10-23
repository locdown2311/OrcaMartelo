#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_floating_point.hpp>

#include "libslic3r/Config.hpp"
#include "libslic3r/PrintConfig.hpp"
#include "libslic3r/Slicing.hpp"

#include <cstddef>
#include <vector>

using namespace Slic3r;
using Catch::Matchers::WithinAbs;

// A 20 mm tall object at 0.2 mm layers, no raft.
static SlicingParameters plain_slicing_parameters()
{
    SlicingParameters params;
    params.valid                            = true;
    params.layer_height                     = 0.2;
    params.first_print_layer_height         = 0.2;
    params.first_object_layer_height        = 0.2;
    params.min_layer_height                 = 0.07;
    params.max_layer_height                 = 0.3;
    params.object_print_z_min               = 0.;
    params.object_print_z_max               = 20.;
    params.object_print_z_uncompensated_max = 20.;
    params.object_shrinkage_compensation_z  = 1.; // no Z shrinkage compensation
    return params;
}

TEST_CASE("A height range prints at its own layer height", "[Slicing]")
{
    const SlicingParameters params = plain_slicing_parameters();
    t_layer_config_ranges   ranges;
    ModelConfig             range_config;
    range_config.set_key_value("layer_height", new ConfigOptionFloat(0.1));
    ranges[{ 5., 10. }].assign_config(std::move(range_config));

    const std::vector<coordf_t> profile = layer_height_profile_from_ranges(params, ranges);
    const std::vector<coordf_t> layers  = generate_object_layers(params, profile, false);
    // generate_object_layers() returns (bottom, top) pairs.
    REQUIRE(layers.size() % 2 == 0);
    size_t inside = 0, outside = 0;
    for (size_t i = 0; i < layers.size(); i += 2) {
        const double z = 0.5 * (layers[i] + layers[i + 1]), h = layers[i + 1] - layers[i];
        INFO("layer at " << z << " is " << h << " mm");
        if (z > 5.05 && z < 9.95) {
            CHECK_THAT(h, WithinAbs(0.1, 1e-6));
            ++ inside;
        } else if (z < 4.95 || z > 10.05) {
            CHECK_THAT(h, WithinAbs(0.2, 1e-6));
            ++ outside;
        }
    }
    // 5 mm at 0.1 mm and 15 mm at 0.2 mm, give or take the layers at the range's ends.
    CHECK(inside >= 48);
    CHECK(outside >= 73);
    CHECK_THAT(layers.back(), WithinAbs(20., 1e-6));
}
