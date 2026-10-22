#include "FitToBed.hpp"

#include "ClipperUtils.hpp"
#include "ExPolygon.hpp"
#include "Polygon.hpp"
#include "TriangleMeshSlicer.hpp"

#include <algorithm>
#include <cmath>
#include <limits>

namespace Slic3r {

std::vector<int> FitToBedPlan::leaves() const
{
    std::vector<int> out;
    for (int i = 0; i < int(pieces.size()); ++ i)
        if (pieces[i].cut < 0)
            out.push_back(i);
    return out;
}

bool fits_printable_volume(const Vec3d &size, const Vec3d &volume)
{
    // Which extent of the box stands up: the other two lie on the bed, either way round.
    for (int up = 0; up < 3; ++ up) {
        const double a = size((up + 1) % 3), b = size((up + 2) % 3);
        if (size(up) <= volume.z() + EPSILON &&
            ((a <= volume.x() + EPSILON && b <= volume.y() + EPSILON) || (a <= volume.y() + EPSILON && b <= volume.x() + EPSILON)))
            return true;
    }
    return false;
}

namespace {

// Rotation taking world axis `axis` onto +Z, so slicing at z = t cuts perpendicular to that axis
// at coordinate t: X -> Z is (x, y, z) -> (-z, y, x), Y -> Z is (x, y, z) -> (x, -z, y).
Transform3d axis_to_z(int axis)
{
    Transform3d t = Transform3d::Identity();
    if (axis == 0)
        t.linear() << 0, 0, -1,  0, 1, 0,  1, 0, 0;
    else if (axis == 1)
        t.linear() << 1, 0, 0,  0, 0, -1,  0, 1, 0;
    return t;
}

double expolygons_area(const ExPolygons &expolys)
{
    double a = 0.;
    for (const ExPolygon &e : expolys)
        a += e.area();
    return a * SCALING_FACTOR * SCALING_FACTOR;
}

// The longest extent along `axis` that still lets a box with the other two extents of `size` fit,
// or 0 when those two do not fit whatever the extent along `axis` is.
double longest_fitting_extent(const Vec3d &size, int axis, const Vec3d &volume)
{
    double best = 0.;
    for (const double candidate : { volume.x(), volume.y(), volume.z() }) {
        Vec3d s = size;
        s(axis) = candidate;
        if (fits_printable_volume(s, volume))
            best = std::max(best, candidate);
    }
    return best;
}

// Connector centres on a cut face, in the face's 2D frame (scaled): far enough inside the face to
// leave a wall around each, spread out by farthest-point sampling from the point nearest the centre.
std::vector<Point> place_connectors(const ExPolygons &face, const FitToBedOptions &options)
{
    const ExPolygons inner = offset_ex(face, -float(scale_(options.connector_radius + options.connector_wall)));
    if (inner.empty())
        return {};
    const double area = expolygons_area(face);
    const size_t count = area <= 400. ? 1 : area <= 1500. ? 2 : area <= 4000. ? 3 : 4;

    // Candidates on a grid inside the usable region, plus each island's own centre when it is inside.
    const BoundingBox bb   = get_extents(inner);
    const coord_t     grid = std::max<coord_t>(scale_(1.), std::max(bb.size().x(), bb.size().y()) / 40);
    std::vector<Point> candidates;
    for (coord_t y = bb.min.y(); y <= bb.max.y(); y += grid)
        for (coord_t x = bb.min.x(); x <= bb.max.x(); x += grid)
            if (const Point p(x, y); std::any_of(inner.begin(), inner.end(), [&p](const ExPolygon &e) { return e.contains(p); }))
                candidates.push_back(p);
    for (const ExPolygon &e : inner)
        if (const Point c = e.contour.centroid(); e.contains(c))
            candidates.push_back(c);
    if (candidates.empty())
        return {};

    const Point centre = bb.center();
    std::vector<Point> out;
    out.push_back(*std::min_element(candidates.begin(), candidates.end(),
        [&centre](const Point &a, const Point &b) { return (a - centre).cast<double>().squaredNorm() < (b - centre).cast<double>().squaredNorm(); }));
    // Two connectors closer than this would merge into one hole.
    const double min_spacing = scale_(2. * options.connector_radius + options.connector_wall);
    while (out.size() < count) {
        double best_d = -1.;
        Point  best;
        for (const Point &c : candidates) {
            double d = std::numeric_limits<double>::max();
            for (const Point &o : out)
                d = std::min(d, (c - o).cast<double>().norm());
            if (d > best_d) {
                best_d = d;
                best   = c;
            }
        }
        if (best_d < min_spacing)
            break;
        out.push_back(best);
    }
    return out;
}

// A piece is the part of the model inside an axis-aligned box: every cut is perpendicular to an
// axis, so the region a piece occupies is just its parent's region with one bound moved. Pieces are
// never cut out of the mesh (a cut mesh's caps leave edges a second cut cannot follow); sections and
// extents are measured on the whole model and clipped to the region instead.
struct Region
{
    Vec3d min { Vec3d::Constant(-std::numeric_limits<double>::max()) };
    Vec3d max { Vec3d::Constant( std::numeric_limits<double>::max()) };
};

// Bounding box of the part of `its` inside `region`: each triangle clipped to the box.
BoundingBoxf3 clipped_bounding_box(const indexed_triangle_set &its, const Region &region)
{
    BoundingBoxf3 bb;
    std::vector<Vec3d> poly, next;
    for (const stl_triangle_vertex_indices &f : its.indices) {
        poly = { its.vertices[f(0)].cast<double>(), its.vertices[f(1)].cast<double>(), its.vertices[f(2)].cast<double>() };
        for (int axis = 0; axis < 3 && ! poly.empty(); ++ axis)
            for (const int side : { 0, 1 }) {
                // Keep the side of the plane inside the region (Sutherland-Hodgman).
                const double bound = side == 0 ? region.min(axis) : region.max(axis);
                auto inside = [&](const Vec3d &p) { return side == 0 ? p(axis) >= bound : p(axis) <= bound; };
                next.clear();
                for (size_t i = 0; i < poly.size(); ++ i) {
                    const Vec3d &a = poly[i], &b = poly[(i + 1) % poly.size()];
                    if (inside(a))
                        next.push_back(a);
                    if (inside(a) != inside(b))
                        next.push_back(a + (b - a) * ((bound - a(axis)) / (b(axis) - a(axis))));
                }
                poly.swap(next);
                if (poly.empty())
                    break;
            }
        for (const Vec3d &p : poly)
            bb.merge(p);
    }
    return bb;
}

// The region's footprint on a slicing plane perpendicular to `axis`, in the 2D frame axis_to_z()
// gives that plane (X: (u, v) = (-z, y); Y: (x, -z); Z: (x, y)), scaled. Unbounded sides are clamped
// to something far larger than any printer.
Polygon region_rectangle(const Region &region, int axis)
{
    auto clamp = [](double v) { return std::clamp(v, -1e5, 1e5); };
    double u0, u1, v0, v1;
    if (axis == 0) {      u0 = -clamp(region.max.z()); u1 = -clamp(region.min.z()); v0 = clamp(region.min.y()); v1 = clamp(region.max.y()); }
    else if (axis == 1) { u0 =  clamp(region.min.x()); u1 =  clamp(region.max.x()); v0 = -clamp(region.max.z()); v1 = -clamp(region.min.z()); }
    else {                u0 =  clamp(region.min.x()); u1 =  clamp(region.max.x()); v0 = clamp(region.min.y()); v1 = clamp(region.max.y()); }
    Polygon rect;
    rect.points = { Point::new_scale(u0, v0), Point::new_scale(u1, v0), Point::new_scale(u1, v1), Point::new_scale(u0, v1) };
    return rect;
}

struct Planner
{
    const FitToBedOptions      &options;
    const indexed_triangle_set &mesh;
    Vec3d                       volume;     // printable volume minus the margins
    FitToBedPlan               &plan;
    size_t                      leaves { 1 };

    // Split pieces[index] (the model inside `region`) until every piece fits. False when it gave up.
    bool split(int index, const Region &region)
    {
        const BoundingBoxf3 bb   = plan.pieces[index].bbox;
        const Vec3d         size = bb.size();
        if (fits_printable_volume(size, volume)) {
            plan.pieces[index].fits = true;
            return true;
        }

        // The axis to cut: the one needing the fewest pieces along it for the other two extents to
        // fit; when no single axis can do that (two extents too large), the axis that overflows the
        // most, cut to the largest the volume allows, and the next level cuts the rest.
        int    axis  = -1;
        double limit = 0.;
        size_t count = std::numeric_limits<size_t>::max();
        for (int a = 0; a < 3; ++ a) {
            const double l = longest_fitting_extent(size, a, volume);
            if (l <= options.min_piece)
                continue;
            const size_t k = size_t(std::ceil(size(a) / l - EPSILON));
            if (k >= 2 && k < count) {
                axis  = a;
                limit = l;
                count = k;
            }
        }
        if (axis < 0) {
            const double largest = volume.maxCoeff();
            double       worst   = 0.;
            for (int a = 0; a < 3; ++ a)
                if (size(a) / largest > worst) {
                    worst = size(a) / largest;
                    axis  = a;
                }
            limit = largest;
            count = size_t(std::ceil(size(axis) / limit - EPSILON));
        }
        if (leaves + 1 > size_t(options.max_pieces)) {
            plan.status = FitToBedStatus::TooManyPieces;
            return false;
        }

        // Where along the axis: the first piece is between what leaves the rest to `count - 1`
        // pieces and the limit itself, and never thinner than min_piece on either side. The face
        // with the least area wins (less to glue, less to hide), nudged towards equal pieces.
        const double length = size(axis);
        const double lo     = std::max({ options.min_piece, length - double(count - 1) * limit });
        const double hi     = std::min(limit, length - options.min_piece);
        std::vector<float> zs;
        for (double d = lo; d <= hi + EPSILON; d += options.step)
            zs.push_back(float(bb.min(axis) + d));
        if (zs.empty())
            zs.push_back(float(bb.min(axis) + 0.5 * (lo + hi)));

        MeshSlicingParamsEx params;
        params.trafo = axis_to_z(axis);
        std::vector<ExPolygons> sections = slice_mesh_ex(mesh, zs, params);
        const Polygons rect { region_rectangle(region, axis) };
        for (ExPolygons &section : sections)
            section = intersection_ex(section, rect);

        std::vector<double> areas(zs.size());
        double mean_area = 0.;
        for (size_t i = 0; i < zs.size(); ++ i)
            mean_area += (areas[i] = expolygons_area(sections[i]));
        mean_area /= double(zs.size());
        const double ideal = length / double(count);
        size_t best = 0;
        double best_score = std::numeric_limits<double>::max();
        for (size_t i = 0; i < zs.size(); ++ i) {
            const double balance = std::abs((zs[i] - bb.min(axis)) - ideal) / length;
            const double score   = areas[i] + 0.25 * mean_area * balance;
            if (score < best_score) {
                best_score = score;
                best       = i;
            }
        }

        Region lower_region = region, upper_region = region;
        lower_region.max(axis) = zs[best];
        upper_region.min(axis) = zs[best];
        const BoundingBoxf3 lower_bb = clipped_bounding_box(mesh, lower_region);
        const BoundingBoxf3 upper_bb = clipped_bounding_box(mesh, upper_region);
        if (! lower_bb.defined || ! upper_bb.defined) {
            // A plane with nothing on one side cannot help; nothing else would either.
            plan.status = FitToBedStatus::NoVolume;
            return false;
        }

        FitToBedCut cut;
        cut.piece        = index;
        cut.axis         = axis;
        cut.position     = zs[best];
        cut.section_area = areas[best];
        const Transform3d from_z = params.trafo.inverse();
        for (const Point &p : place_connectors(sections[best], options))
            cut.connectors.push_back(from_z * Vec3d(unscale<double>(p.x()), unscale<double>(p.y()), double(zs[best])));

        const int cut_index = int(plan.cuts.size());
        plan.pieces[index].cut = cut_index;
        cut.lower = int(plan.pieces.size());
        plan.pieces.push_back({ lower_bb, false, -1 });
        cut.upper = int(plan.pieces.size());
        plan.pieces.push_back({ upper_bb, false, -1 });
        plan.cuts.push_back(std::move(cut));
        ++ leaves;

        const int lower_index = plan.cuts[cut_index].lower, upper_index = plan.cuts[cut_index].upper;
        return split(lower_index, lower_region) && split(upper_index, upper_region);
    }
};

} // namespace

FitToBedPlan plan_fit_to_bed(const indexed_triangle_set &world_mesh, const FitToBedOptions &options)
{
    FitToBedPlan plan;
    const Vec3d volume = options.volume_size - Vec3d(2. * options.margin, 2. * options.margin, options.margin);
    if (world_mesh.indices.empty() || volume.minCoeff() <= options.min_piece)
        return plan;   // NoVolume

    Planner planner{ options, world_mesh, volume, plan };
    plan.pieces.push_back({ clipped_bounding_box(world_mesh, Region{}), false, -1 });
    if (fits_printable_volume(plan.pieces.front().bbox.size(), volume)) {
        plan.pieces.front().fits = true;
        plan.status = FitToBedStatus::AlreadyFits;
        return plan;
    }
    plan.status = FitToBedStatus::Ok;
    if (! planner.split(0, Region{}) && plan.status == FitToBedStatus::Ok)
        plan.status = FitToBedStatus::NoVolume;
    return plan;
}

} // namespace Slic3r
