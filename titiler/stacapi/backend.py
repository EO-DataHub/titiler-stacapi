"""TiTiler.PgSTAC custom Mosaic Backend and Custom STACReader."""

import json
from typing import Any, Dict, List, Optional, Tuple, Type

import attr
import rasterio
from cachetools import TTLCache, cached
from cachetools.keys import hashkey
from cogeo_mosaic.backends import BaseBackend
from cogeo_mosaic.errors import NoAssetFoundError
from cogeo_mosaic.mosaic import MosaicJSON
from geojson_pydantic import Point, Polygon
from geojson_pydantic.geometries import Geometry
from morecantile import Tile, TileMatrixSet
from pystac_client import Client
from rasterio.crs import CRS
from rasterio.warp import transform, transform_bounds
from rio_tiler.constants import WEB_MERCATOR_TMS, WGS84_CRS
from rio_tiler.errors import InvalidAssetName
from rio_tiler.io import Reader
from rio_tiler.io.base import BaseReader, MultiBaseReader
from rio_tiler.models import ImageData
from rio_tiler.mosaic import mosaic_reader
from rio_tiler.types import AssetInfo, BBox

from titiler.stacapi.settings import CacheSettings, RetrySettings, STACAPISettings
from titiler.stacapi.utils import retry

cache_config = CacheSettings()
retry_config = RetrySettings()
stacapi_config = STACAPISettings()


@attr.s
class CustomSTACReader(MultiBaseReader):
    """Simplified STAC Reader.

    Inputs should be in form of:
    {
        "id": "IAMASTACITEM",
        "collection": "mycollection",
        "bbox": (0, 0, 10, 10),
        "assets": {
            "COG": {
                "href": "https://somewhereovertherainbow.io/cog.tif"
            }
        }
    }

    """

    input: Dict[str, Any] = attr.ib()
    tms: TileMatrixSet = attr.ib(default=WEB_MERCATOR_TMS)
    minzoom: int = attr.ib()
    maxzoom: int = attr.ib()

    reader: Type[BaseReader] = attr.ib(default=Reader)
    reader_options: Dict = attr.ib(factory=dict)

    ctx: Any = attr.ib(default=rasterio.Env)

    def __attrs_post_init__(self) -> None:
        """Set reader spatial infos and list of valid assets."""
        self.bounds = self.input["bbox"]
        self.crs = WGS84_CRS  # Per specification STAC items are in WGS84
        self.assets = list(self.input["assets"])

    @minzoom.default
    def _minzoom(self):
        return self.tms.minzoom

    @maxzoom.default
    def _maxzoom(self):
        return self.tms.maxzoom

    def _get_asset_info(self, asset: str) -> AssetInfo:
        """Validate asset names and return asset's url.

        Args:
            asset (str): STAC asset name.

        Returns:
            str: STAC asset href.

        """
        if asset not in self.assets:
            raise InvalidAssetName(
                f"{asset} is not valid. Should be one of {self.assets}"
            )

        asset_info = self.input["assets"][asset]
        info = AssetInfo(
            url=asset_info["href"],
            env={},
        )

        if header_size := asset_info.get("file:header_size"):
            info["env"]["GDAL_INGESTED_BYTES_AT_OPEN"] = header_size

        if bands := asset_info.get("raster:bands"):
            stats = [
                (b["statistics"]["minimum"], b["statistics"]["maximum"])
                for b in bands
                if {"minimum", "maximum"}.issubset(b.get("statistics", {}))
            ]
            if len(stats) == len(bands):
                info["dataset_statistics"] = stats

        return info


@attr.s
class STACAPIBackend(BaseBackend):
    """STACAPI Mosaic Backend."""

    # STAC API URL
    input: str = attr.ib()

    # Because we are not using mosaicjson we are not limited to the WebMercator TMS
    tms: TileMatrixSet = attr.ib(default=WEB_MERCATOR_TMS)
    minzoom: int = attr.ib()
    maxzoom: int = attr.ib()

    # Use Custom STAC reader (outside init)
    reader: Type[CustomSTACReader] = attr.ib(init=False, default=CustomSTACReader)
    reader_options: Dict = attr.ib(factory=dict)

    # default values for bounds
    bounds: BBox = attr.ib(default=(-180, -90, 180, 90))

    crs: CRS = attr.ib(default=WGS84_CRS)
    geographic_crs: CRS = attr.ib(default=WGS84_CRS)

    # The reader is read-only (outside init)
    mosaic_def: MosaicJSON = attr.ib(init=False)

    _backend_name = "STACAPI"

    def __attrs_post_init__(self) -> None:
        """Post Init."""
        # Construct a FAKE mosaicJSON
        # mosaic_def has to be defined.
        # we set `tiles` to an empty list.
        self.mosaic_def = MosaicJSON(
            mosaicjson="0.0.3",
            name=self.input,
            bounds=self.bounds,
            minzoom=self.minzoom,
            maxzoom=self.maxzoom,
            tiles={},
        )

    @minzoom.default
    def _minzoom(self):
        return self.tms.minzoom

    @maxzoom.default
    def _maxzoom(self):
        return self.tms.maxzoom

    def write(self, overwrite: bool = True) -> None:
        """This method is not used but is required by the abstract class."""
        pass

    def update(self) -> None:
        """We overwrite the default method."""
        pass

    def _read(self) -> MosaicJSON:
        """This method is not used but is required by the abstract class."""
        pass

    def assets_for_tile(self, x: int, y: int, z: int, **kwargs: Any) -> List[Dict]:
        """Retrieve assets for tile."""
        bbox = self.tms.bounds(Tile(x, y, z))
        return self.get_assets(Polygon.from_bounds(*bbox), **kwargs)

    def assets_for_point(
        self,
        lng: float,
        lat: float,
        coord_crs: CRS = WGS84_CRS,
        **kwargs: Any,
    ) -> List[Dict]:
        """Retrieve assets for point."""
        if coord_crs != WGS84_CRS:
            xs, ys = transform(coord_crs, WGS84_CRS, [lng], [lat])
            lng, lat = xs[0], ys[0]

        return self.get_assets(Point(type="Point", coordinates=(lng, lat)), **kwargs)

    def assets_for_bbox(
        self,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
        coord_crs: CRS = WGS84_CRS,
        **kwargs: Any,
    ) -> List[Dict]:
        """Retrieve assets for bbox."""
        if coord_crs != WGS84_CRS:
            xmin, ymin, xmax, ymax = transform_bounds(
                coord_crs,
                WGS84_CRS,
                xmin,
                ymin,
                xmax,
                ymax,
            )

        return self.get_assets(Polygon.from_bounds(xmin, ymin, xmax, ymax), **kwargs)

    @cached(  # type: ignore
        TTLCache(maxsize=cache_config.maxsize, ttl=cache_config.ttl),
        key=lambda self, geom, search_query, **kwargs: hashkey(
            self.input, str(geom), json.dumps(search_query), **kwargs
        ),
    )
    @retry(
        tries=retry_config.retry,
        delay=retry_config.delay,
        # exceptions=(httpx.HTTPError,),
    )
    def get_assets(
        self,
        geom: Geometry,
        search_query: Optional[Dict] = None,
        fields: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Find assets."""
        search_query = search_query or {}
        fields = fields or ["assets", "id", "bbox", "collection"]

        catalog = Client.open(self.input)

        params = {
            **search_query,
            "intersects": geom.model_dump_json(exclude_none=True),
            "fields": fields,
        }
        params.pop("bbox", None)

        results = catalog.search(**params)
        return list(results.items_as_dicts())

    @property
    def _quadkeys(self) -> List[str]:
        return []

    def tile(
        self,
        tile_x: int,
        tile_y: int,
        tile_z: int,
        search_query: Optional[Dict] = None,
        **kwargs: Any,
    ) -> Tuple[ImageData, List[str]]:
        """Get Tile from multiple observation."""
        mosaic_assets = self.assets_for_tile(
            tile_x,
            tile_y,
            tile_z,
            search_query=search_query,
        )

        if not mosaic_assets:
            raise NoAssetFoundError(
                f"No assets found for tile {tile_z}-{tile_x}-{tile_y}"
            )

        def _reader(
            item: Dict[str, Any], x: int, y: int, z: int, **kwargs: Any
        ) -> ImageData:
            with self.reader(item, tms=self.tms, **self.reader_options) as src_dst:
                return src_dst.tile(x, y, z, **kwargs)

        return mosaic_reader(mosaic_assets, _reader, tile_x, tile_y, tile_z, **kwargs)

    def point(
        self,
        lon: float,
        lat: float,
        coord_crs: CRS = WGS84_CRS,
        search_query: Optional[Dict] = None,
        **kwargs: Any,
    ) -> List:
        """Get Point value from multiple observation."""
        raise NotImplementedError

    def part(
        self,
        bbox: BBox,
        dst_crs: Optional[CRS] = None,
        bounds_crs: CRS = WGS84_CRS,
        search_query: Optional[Dict] = None,
        **kwargs: Any,
    ) -> Tuple[ImageData, List[str]]:
        """Create an Image from multiple items for a bbox."""
        raise NotImplementedError

    def feature(
        self,
        shape: Dict,
        shape_crs: CRS = WGS84_CRS,
        max_size: int = 1024,
        search_query: Optional[Dict] = None,
        **kwargs: Any,
    ) -> Tuple[ImageData, List[str]]:
        """Create an Image from multiple items for a GeoJSON feature."""
        raise NotImplementedError
