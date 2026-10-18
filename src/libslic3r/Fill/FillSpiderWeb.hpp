#ifndef slic3r_FillSpiderWeb_hpp_
#define slic3r_FillSpiderWeb_hpp_

#include "libslic3r/ExPolygon.hpp"
#include "FillBase.hpp"
#include <utility>
#include "libslic3r/Polyline.hpp"

namespace Slic3r {

class FillSpiderWeb : public Fill
{
public:
    ~FillSpiderWeb() override = default;
    bool is_self_crossing() override { return true; }

protected:
    Fill* clone() const override { return new FillSpiderWeb(*this); }
    void _fill_surface_single(
        const FillParams                &params, 
        unsigned int                     thickness_layers,
        const std::pair<float, Point>   &direction, 
        ExPolygon                        expolygon,
        Polylines                       &polylines_out) override;

    bool no_sort() const override { return false; }
};

} // namespace Slic3r

#endif // slic3r_FillSpiderWeb_hpp_
