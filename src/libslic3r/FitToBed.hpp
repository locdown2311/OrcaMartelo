#ifndef slic3r_FitToBed_hpp_
#define slic3r_FitToBed_hpp_

// Split a model that does not fit the printer into pieces that do, with connector positions on
// every cut face. This is the planning half: it decides where to cut and where the connectors go,
// working on the world-space mesh of one instance. Applying the plan to a ModelObject (cutting with
// Cut, adding the connectors, laying the pieces out) is the GUI's job.
//
// Cuts are planes perpendicular to a world axis. Each cut splits one piece into two, so a plan is a
// tree: pieces[0] is the whole model, and cut i splits pieces[cuts[i].piece] into pieces[cuts[i].lower]
// (the side below the plane) and pieces[cuts[i].upper]. The pieces that get printed are the leaves.

#include "libslic3r.h"
#include "BoundingBox.hpp"
#include "Point.hpp"
#include "TriangleMesh.hpp"

#include <vector>

namespace Slic3r {

struct FitToBedOptions
{
    // Printable volume (mm): bed X, bed Y, max height.
    Vec3d  volume_size { Vec3d::Zero() };
    // Clearance kept from every side of the printable volume (mm).
    double margin { 2. };
    // Give up beyond this many pieces.
    int    max_pieces { 16 };
    // Spacing of the candidate cut positions along an axis (mm).
    double step { 1. };
    // No piece thinner than this along the axis it was cut on (mm).
    double min_piece { 10. };
    // Radius of a connector and the wall it needs around it (mm): connectors stay at least
    // connector_radius + connector_wall away from the edge of the cut face.
    double connector_radius { 2.5 };
    double connector_wall   { 2. };
};

struct FitToBedCut
{
    // The piece this cut splits, and the two it becomes (indices into FitToBedPlan::pieces).
    int                piece { -1 };
    int                lower { -1 };
    int                upper { -1 };
    // World axis the cut is perpendicular to (0 = X, 1 = Y, 2 = Z) and where it crosses it (mm).
    int                axis { 2 };
    double             position { 0. };
    // Area of the cut face (mm^2); 0 when the plane runs through a gap and splits disjoint parts.
    double             section_area { 0. };
    // Connector centres on the cut face, world coordinates (empty when there is no face to join).
    std::vector<Vec3d> connectors;
};

struct FitToBedPiece
{
    BoundingBoxf3 bbox;      // world bounding box
    bool          fits { false };
    int           cut { -1 }; // the cut that splits this piece further; -1 for a leaf
};

enum class FitToBedStatus
{
    AlreadyFits,    // nothing to do
    Ok,             // every piece fits
    TooManyPieces,  // would need more than max_pieces
    NoVolume,       // the printable volume (minus the margins) is empty, or the mesh is
};

struct FitToBedPlan
{
    FitToBedStatus             status { FitToBedStatus::NoVolume };
    std::vector<FitToBedCut>   cuts;
    std::vector<FitToBedPiece> pieces;

    // The pieces that get printed.
    std::vector<int> leaves() const;
};

// True when a box of `size` fits `volume` in some orientation that keeps its faces axis-aligned
// (any of the six ways it can lie, each rotated by 90 degrees about Z).
bool fits_printable_volume(const Vec3d &size, const Vec3d &volume);

FitToBedPlan plan_fit_to_bed(const indexed_triangle_set &world_mesh, const FitToBedOptions &options);

} // namespace Slic3r

#endif // slic3r_FitToBed_hpp_
