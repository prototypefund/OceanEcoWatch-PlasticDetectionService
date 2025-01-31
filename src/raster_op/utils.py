import io
import logging

import numpy as np
import rasterio
from shapely.geometry import box

from src._types import BoundingBox, HeightWidth
from src.models import DownloadResponse, Raster

LOGGER = logging.getLogger(__name__)


def update_bounds(meta, new_bounds: BoundingBox) -> dict:
    minx, _, _, maxy = new_bounds
    transform = meta["transform"]
    new_transform = rasterio.Affine(
        transform.a, transform.b, minx, transform.d, transform.e, maxy
    )
    meta["transform"] = new_transform
    return meta


def update_window_meta(meta, image: np.ndarray) -> dict:
    window_meta = meta.copy()
    window_meta.update(
        {
            "count": image.shape[0],
            "height": image.shape[1],
            "width": image.shape[2],
        }
    )
    return window_meta


def create_raster(
    content: bytes,
    image: np.ndarray,
    bounds: BoundingBox,
    meta: dict,
    padding_size: HeightWidth,
) -> Raster:
    bands = [i + 1 for i in range(image.shape[0])]

    return Raster(
        content=content,
        size=HeightWidth(image.shape[1], image.shape[2]),
        dtype=image.dtype,
        crs=meta["crs"].to_epsg(),
        bands=bands,
        resolution=(meta["transform"].a),
        geometry=box(*bounds),
        padding_size=padding_size,
    )


def write_image(image: np.ndarray, meta: dict) -> bytes:
    buffer = io.BytesIO()
    with rasterio.open(buffer, "w+", **meta) as mem_dst:
        mem_dst.write(image)

    return buffer.getvalue()


def create_raster_from_download_response(image: DownloadResponse) -> Raster:
    with rasterio.open(io.BytesIO(image.content)) as src:
        np_image = src.read().copy()
        meta = src.meta.copy()
        bounds = BoundingBox(*src.bounds)
    return create_raster(
        content=image.content,
        image=np_image,
        bounds=bounds,
        meta=meta,
        padding_size=HeightWidth(0, 0),
    )
