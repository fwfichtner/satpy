"""Set CF-compliant spatial and temporal coordinates."""

import logging
import warnings
from collections import defaultdict
from contextlib import suppress

import numpy as np
import xarray as xr
from dask.base import tokenize
from pyresample.geometry import AreaDefinition, SwathDefinition

logger = logging.getLogger(__name__)


EPOCH = u"seconds since 1970-01-01 00:00:00"


def add_xy_coords_attrs(dataarray):
    """Add relevant attributes to x, y coordinates."""
    # If there are no coords, return dataarray
    if not dataarray.coords.keys() & {"x", "y", "crs"}:
        return dataarray
    # If projected area
    if _is_projected(dataarray):
        dataarray = _add_xy_projected_coords_attrs(dataarray)
    else:
        dataarray = _add_xy_geographic_coords_attrs(dataarray)
    if "crs" in dataarray.coords:
        dataarray = dataarray.drop_vars("crs")
    return dataarray


def _is_projected(dataarray):
    """Guess whether data are projected or not."""
    crs = _try_to_get_crs(dataarray)
    if crs:
        return crs.is_projected
    units = _try_get_units_from_coords(dataarray)
    if units:
        if units.endswith("m"):
            return True
        if units.startswith("degrees"):
            return False
    logger.warning("Failed to tell if data are projected. Assuming yes.")
    return True


def _is_area(dataarray):
     if isinstance(dataarray.attrs["area"], AreaDefinition):
        return True
     else:
        return False


def _is_swath(dataarray):
     if isinstance(dataarray.attrs["area"], SwathDefinition):
        return True
     else:
        return False


def _try_to_get_crs(dataarray):
    """Try to get a CRS from attributes."""
    if "area" in dataarray.attrs:
        if _is_area(dataarray):
            return dataarray.attrs["area"].crs
        if not _is_swath(dataarray):
            logger.warning(
                f"Could not tell CRS from area of type {type(dataarray.attrs['area']).__name__:s}. "
                "Assuming projected CRS.")
    if "crs" in dataarray.coords:
        return dataarray.coords["crs"].item()


def _try_get_units_from_coords(dataarray):
    """Try to retrieve coordinate x/y units."""
    for c in ["x", "y"]:
        with suppress(KeyError):
            # If the data has only 1 dimension, it has only one of x or y coords
            if "units" in dataarray.coords[c].attrs:
                return dataarray.coords[c].attrs["units"]


def _add_xy_projected_coords_attrs(dataarray, x="x", y="y"):
    """Add relevant attributes to x, y coordinates of a projected CRS."""
    if x in dataarray.coords:
        dataarray[x].attrs["standard_name"] = "projection_x_coordinate"
        dataarray[x].attrs["units"] = "m"
    if y in dataarray.coords:
        dataarray[y].attrs["standard_name"] = "projection_y_coordinate"
        dataarray[y].attrs["units"] = "m"
    return dataarray


def _add_xy_geographic_coords_attrs(dataarray, x="x", y="y"):
    """Add relevant attributes to x, y coordinates of a geographic CRS."""
    if x in dataarray.coords:
        dataarray[x].attrs["standard_name"] = "longitude"
        dataarray[x].attrs["units"] = "degrees_east"
    if y in dataarray.coords:
        dataarray[y].attrs["standard_name"] = "latitude"
        dataarray[y].attrs["units"] = "degrees_north"
    return dataarray


def set_cf_time_info(dataarray, epoch):
    """Set CF time attributes and encoding.

    It expand the DataArray with a time dimension if does not yet exists.

    The function assumes

        - that x and y dimensions have at least shape > 1
        - the time coordinate has size 1

    """
    if epoch is None:
        epoch = EPOCH

    dataarray["time"].encoding["units"] = epoch
    dataarray["time"].attrs["standard_name"] = "time"
    dataarray["time"].attrs.pop("bounds", None)

    if "time" not in dataarray.dims and dataarray["time"].size not in dataarray.shape:
        dataarray = dataarray.expand_dims("time")

    return dataarray


def _is_lon_or_lat_dataarray(dataarray):
    """Check if the DataArray represents the latitude or longitude coordinate."""
    return dataarray.attrs.get("standard_name", "") in ("longitude", "latitude")


def has_projection_coords(dict_datarrays):
    """Check if DataArray collection has a "longitude" or "latitude" DataArray."""
    for dataarray in dict_datarrays.values():
        if _is_lon_or_lat_dataarray(dataarray):
            return True
    return False


def _get_is_nondimensional_coords_dict(dict_dataarrays):
    tokens = defaultdict(set)
    for dataarray in dict_dataarrays.values():
        for coord_name in dataarray.coords:
            if not _is_lon_or_lat_dataarray(dataarray[coord_name]) and coord_name not in dataarray.dims:
                tokens[coord_name].add(tokenize(dataarray[coord_name].data))
    coords_unique = dict([(coord_name, len(tokens) == 1) for coord_name, tokens in tokens.items()])
    return coords_unique


def _warn_if_pretty_but_not_unique(pretty, coord_name):
    """Warn if coordinates cannot be pretty-formatted due to non-uniqueness."""
    if pretty:
        warnings.warn(
            f'Cannot pretty-format "{coord_name}" coordinates because they are '
            'not identical among the given datasets',
            stacklevel=2
        )


def _rename_coords(dict_dataarrays, coord_name):
    """Rename coordinates in the datasets."""
    for name, dataarray in dict_dataarrays.items():
        if coord_name in dataarray.coords:
            rename = {coord_name: f"{name}_{coord_name}"}
            dict_dataarrays[name] = dataarray.rename(rename)
    return dict_dataarrays


def ensure_unique_nondimensional_coords(dict_dataarrays, pretty=False):
    """Make non-dimensional coordinates unique among all datasets.

    Non-dimensional coordinates, such as scanline timestamps,
    may occur in multiple datasets with the same name and dimension
    but different values.

    In order to avoid conflicts, prepend the dataset name to the coordinate name.
    If a non-dimensional coordinate is unique among all datasets and ``pretty=True``,
    its name will not be modified.

    Since all datasets must have the same projection coordinates,
    this is not applied to latitude and longitude.

    Args:
        datas (dict):
            Dictionary of (dataset name, dataset)
        pretty (bool):
            Don't modify coordinate names, if possible. Makes the file prettier, but possibly less consistent.

    Returns:
        Dictionary holding the updated datasets

    """
    # Determine which non-dimensional coordinates are unique
    # - coords_unique has structure: {coord_name: True/False}
    is_coords_unique_dict = _get_is_nondimensional_coords_dict(dict_dataarrays)

    # Prepend dataset name, if not unique or no pretty-format desired
    new_dict_dataarrays = dict_dataarrays.copy()
    for coord_name, unique in is_coords_unique_dict.items():
        if not pretty or not unique:
            _warn_if_pretty_but_not_unique(pretty, coord_name)
            new_dict_dataarrays = _rename_coords(new_dict_dataarrays, coord_name)
    return new_dict_dataarrays


def check_unique_projection_coords(dict_dataarrays):
    """Check that all datasets share the same projection coordinates x/y."""
    unique_x = set()
    unique_y = set()
    for dataarray in dict_dataarrays.values():
        if "y" in dataarray.dims:
            token_y = tokenize(dataarray["y"].data)
            unique_y.add(token_y)
        if "x" in dataarray.dims:
            token_x = tokenize(dataarray["x"].data)
            unique_x.add(token_x)
    if len(unique_x) > 1 or len(unique_y) > 1:
        raise ValueError("Datasets to be saved in one file (or one group) must have identical projection coordinates."
                         "Please group them by area or save them in separate files.")



def add_coordinates_attrs_coords(dict_dataarrays):
    """Add to DataArrays the coordinates specified in the 'coordinates' attribute.

    It deal with the 'coordinates' attributes indicating lat/lon coords
    The 'coordinates' attribute is dropped from each DataArray

    If the `coordinates` attribute of a data array links to other dataarrays in the scene, for example
    `coordinates='lon lat'`, add them as coordinates to the data array and drop that attribute.

    In the final call to `xr.Dataset.to_netcdf()` all coordinate relations will be resolved
    and the `coordinates` attributes be set automatically.
    """
    for dataarray_name in dict_dataarrays.keys():
        dict_dataarrays = _add_declared_coordinates(dict_dataarrays,
                                                    dataarray_name=dataarray_name)
        # Drop 'coordinates' attribute in any case to avoid conflicts in xr.Dataset.to_netcdf()
        dict_dataarrays[dataarray_name].attrs.pop("coordinates", None)
    return dict_dataarrays


def _add_declared_coordinates(dict_dataarrays, dataarray_name):
    """Add declared coordinates to the dataarray if they exist."""
    dataarray = dict_dataarrays[dataarray_name]
    declared_coordinates = _get_coordinates_list(dataarray)
    for coord in declared_coordinates:
        if coord not in dataarray.coords:
            dict_dataarrays = _try_add_coordinate(dict_dataarrays,
                                                  dataarray_name=dataarray_name,
                                                  coord=coord)
    return dict_dataarrays


def _try_add_coordinate(dict_dataarrays, dataarray_name, coord):
    """Try to add a coordinate to the dataarray, warn if not possible."""
    try:
        dataarray_dims = set(dict_dataarrays[dataarray_name].dims)
        coordinate_dims = set(dict_dataarrays[coord].dims)
        dimensions_to_squeeze = list(coordinate_dims - dataarray_dims)
        dict_dataarrays[dataarray_name][coord] = dict_dataarrays[coord].squeeze(dimensions_to_squeeze, drop=True)
    except KeyError:
        warnings.warn(
            f'Coordinate "{coord}" referenced by dataarray {dataarray_name} does not '
            'exist, dropping reference.',
            stacklevel=2
        )
    return dict_dataarrays


def _get_coordinates_list(dataarray):
    """Return a list with the coordinates names specified in the 'coordinates' attribute."""
    declared_coordinates = dataarray.attrs.get("coordinates", [])
    if isinstance(declared_coordinates, str):
        declared_coordinates = declared_coordinates.split(" ")
    return declared_coordinates


def add_time_bounds_dimension(ds, time="time"):
    """Add time bound dimension to xr.Dataset."""
    start_times = []
    end_times = []
    for _var_name, data_array in ds.items():
        start_times.append(data_array.attrs.get("start_time", None))
        end_times.append(data_array.attrs.get("end_time", None))

    start_time = min(start_time for start_time in start_times
                     if start_time is not None)
    end_time = min(end_time for end_time in end_times
                   if end_time is not None)
    ds["time_bnds"] = xr.DataArray([[np.datetime64(start_time),
                                     np.datetime64(end_time)]],
                                   dims=["time", "bnds_1d"])
    ds[time].attrs["bounds"] = "time_bnds"
    ds[time].attrs["standard_name"] = "time"
    return ds
