#include "FillSpiderWeb.hpp"
#include "../ClipperUtils.hpp"
#include "../ExPolygon.hpp"
#include "../Surface.hpp"
#include "FillBase.hpp"
#include "FillCornerSmoothing.hpp"
#include "libslic3r/BoundingBox.hpp"
#include "libslic3r/Point.hpp"
#include "libslic3r/Polyline.hpp"
#include "libslic3r/Polygon.hpp"
#include "libslic3r/ShortestPath.hpp"

#include <cmath>
#include <algorithm>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace Slic3r {

void FillSpiderWeb::_fill_surface_single(
    const FillParams                &params, 
    unsigned int                     thickness_layers,
    const std::pair<float, Point>   &direction, 
    ExPolygon                        expolygon,
    Polylines                       &polylines_out)
{
    BoundingBox bbox = expolygon.contour.bounding_box();
    Point center = bbox.center();

    double dx = std::max(std::abs(bbox.max.x() - center.x()), std::abs(bbox.min.x() - center.x()));
    double dy = std::max(std::abs(bbox.max.y() - center.y()), std::abs(bbox.min.y() - center.y()));
    double max_r = std::sqrt(dx * dx + dy * dy);

    if (max_r <= 0)
        return;

    coord_t min_spacing = scale_(this->spacing) * params.multiline;
    coord_t distance = coord_t(min_spacing / std::max(0.01f, params.density));

    if (min_spacing <= 0 || distance <= 0)
        return;

    if (params.density > 0.9999f && !params.dont_adjust) {
        distance = this->_adjust_solid_spacing(bbox.size()(0), distance);
        this->spacing = unscale<double>(distance);
    }

    // Number of radial spokes: 16 spokes by default (22.5 deg intervals)
    const int num_spokes = 16;
    const double d_theta = (2.0 * M_PI) / double(num_spokes);

    double base_angle = direction.first;
    // Alternate spoke angles slightly on alternating layers for 3D interlocking strength
    if (!this->dont_alternate_fill_direction && (this->layer_id % 2 != 0)) {
        base_angle += 0.5 * d_theta;
    }

    Polylines all_polylines;

    // 1. Generate radial spokes radiating outwards from center
    for (int i = 0; i < num_spokes; ++i) {
        double theta = base_angle + double(i) * d_theta;
        Point p_outer(
            center.x() + coord_t(std::round(max_r * 1.08 * std::cos(theta))),
            center.y() + coord_t(std::round(max_r * 1.08 * std::sin(theta)))
        );
        Polyline spoke;
        spoke.points = { center, p_outer };
        all_polylines.emplace_back(std::move(spoke));
    }

    // 2. Generate concentric scalloped web rings
    const double max_ring_r = max_r * 1.05;
    for (double r = double(distance); r <= max_ring_r; r += double(distance)) {
        Polygon ring;
        ring.points.reserve(num_spokes * 2);

        // Sag factor between spokes (8% inward droop creates the authentic orb-weaver spiderweb look)
        const double sag_factor = (r > 2.0 * double(distance)) ? 0.92 : 0.96;

        for (int i = 0; i < num_spokes; ++i) {
            double theta = base_angle + double(i) * d_theta;
            Point p_vertex(
                center.x() + coord_t(std::round(r * std::cos(theta))),
                center.y() + coord_t(std::round(r * std::sin(theta)))
            );
            ring.points.push_back(p_vertex);

            double mid_theta = base_angle + (double(i) + 0.5) * d_theta;
            Point p_sag(
                center.x() + coord_t(std::round(r * sag_factor * std::cos(mid_theta))),
                center.y() + coord_t(std::round(r * sag_factor * std::sin(mid_theta)))
            );
            ring.points.push_back(p_sag);
        }

        if (ring.points.size() >= 3) {
            all_polylines.emplace_back(ring.split_at_first_point());
        }
    }

    // Smooth corners if enabled
    if (params.smooth_factor > 0) {
        for (Polyline &pl : all_polylines) {
            smooth_polyline_corners(pl, params.smooth_factor, scaled<double>(params.resolution));
        }
    }

    // Apply multiline offset if requested
    multiline_fill(all_polylines, params, spacing);

    // Clip all spokes and web rings to the expolygon boundary
    all_polylines = intersection_pl(std::move(all_polylines), expolygon);

    // Chain and connect infill paths for smooth, continuous printing
    chain_or_connect_infill(std::move(all_polylines), expolygon, polylines_out, this->spacing, params);
}

} // namespace Slic3r
