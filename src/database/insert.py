import io
import logging
from typing import Iterable, Optional

from geoalchemy2 import WKBElement
from geoalchemy2.shape import from_shape
from shapely.geometry import box
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session

from src import config
from src.aws import s3
from src.database.models import (
    Image,
    Job,
    JobStatus,
    PredictionRaster,
    PredictionVector,
    SceneClassificationVector,
)
from src.geo_utils import reproject_geometry
from src.models import DownloadResponse, Raster, Vector

LOGGER = logging.getLogger(__name__)


class Insert:
    def __init__(self, session: Session):
        self.session = session

    def insert_image(
        self,
        download_response: DownloadResponse,
        raster: Raster,
        image_url: str,
        job_id: int,
        satellite_id: int,
    ) -> Image:
        target_crs = 4326
        transformed_geometry = reproject_geometry(
            raster.geometry, raster.crs, target_crs
        )
        image = Image(
            image_id=download_response.image_id,
            satellite_id=satellite_id,
            image_url=image_url,
            timestamp=download_response.timestamp,
            dtype=str(raster.dtype),
            crs=raster.crs,
            resolution=raster.resolution,
            image_width=raster.size[0],
            image_height=raster.size[1],
            bbox=from_shape(transformed_geometry, srid=target_crs),
            job_id=job_id,
        )
        self.session.add(image)
        self.session.commit()
        return image

    def insert_prediction_raster(
        self, raster: Raster, image_id: int, raster_url: str
    ) -> PredictionRaster:
        prediction_raster = PredictionRaster(
            raster_url=raster_url,
            dtype=str(raster.dtype),
            image_width=raster.size[0],
            image_height=raster.size[1],
            bbox=from_shape(raster.geometry, srid=raster.crs),
            image_id=image_id,
        )
        self.session.add(prediction_raster)
        self.session.commit()
        return prediction_raster

    def insert_prediction_vectors(
        self, vectors: Iterable[Vector], raster_id: int
    ) -> list[PredictionVector]:
        prediction_vectors = [
            PredictionVector(
                v.pixel_value, from_shape(v.geometry, srid=v.crs), raster_id
            )
            for v in vectors
        ]
        self.session.bulk_save_objects(prediction_vectors)
        self.session.commit()
        return prediction_vectors

    def insert_scls_vectors(
        self, vectors: Iterable[Vector], image_id: int
    ) -> list[SceneClassificationVector]:
        scls_vectors = [
            SceneClassificationVector(
                v.pixel_value, from_shape(v.geometry, srid=v.crs), image_id
            )
            for v in vectors
        ]

        inserted_vectors = []
        for vector in scls_vectors:
            try:
                self.session.add(vector)
                self.session.commit()
                inserted_vectors.append(vector)
            except IntegrityError:
                self.session.rollback()
                LOGGER.info(f"Duplicate vector skipped: {vector.geometry}")

        return inserted_vectors


class InsertJob:
    def __init__(self, insert: Insert):
        self.insert = insert

    def insert_all(
        self,
        job_id: int,
        satellite_id: int,
        model_name: str,
        download_response: DownloadResponse,
        image: Raster,
        pred_raster: Raster,
        vectors: Iterable[Vector],
    ) -> Optional[tuple[Image, PredictionRaster, PredictionVector]]:
        unique_id = f"{download_response.bbox}/{download_response.image_id}"
        image_url = s3.stream_to_s3(
            io.BytesIO(download_response.content),
            config.S3_BUCKET_NAME,
            f"images/{unique_id}.tif",
        )

        image_db = self.insert.insert_image(
            download_response, image, image_url, job_id, satellite_id
        )
        LOGGER.info(f"Inserted image {unique_id} into database")
        pred_raster_url = s3.stream_to_s3(
            io.BytesIO(pred_raster.content),
            config.S3_BUCKET_NAME,
            f"predictions/{model_name}/{unique_id}.tif",
        )
        prediction_raster_db = self.insert.insert_prediction_raster(
            pred_raster, image_db.id, pred_raster_url
        )
        LOGGER.info(f"Inserted prediction raster for image {unique_id} into database")
        prediction_vectors_db = self.insert.insert_prediction_vectors(
            vectors, prediction_raster_db.id
        )
        LOGGER.info(f"Inserted prediction vectors for image {unique_id} into database")

        return image_db, prediction_raster_db, prediction_vectors_db


def set_init_job_status(db_session: Session, job_id: int) -> Job:
    job = db_session.query(Job).filter(Job.id == job_id).first()

    if job is None:
        update_job_status(db_session, job_id, JobStatus.FAILED)
        raise NoResultFound("Job not found")

    else:
        LOGGER.info(f"Updating job {job_id} to in progress")
        update_job_status(db_session, job_id, JobStatus.IN_PROGRESS)
    return job


def update_job_status(db_session: Session, job_id: int, status: JobStatus):
    db_session.query(Job).filter(Job.id == job_id).update({"status": status})
    db_session.commit()


def image_in_db(
    db_session: Session, download_response: DownloadResponse, job_id: int
) -> bool:
    bbox = box(*download_response.bbox)
    bbox_geom_4326 = reproject_geometry(bbox, download_response.crs, 4326)
    bbox_geom = WKBElement(bbox_geom_4326.wkb, srid=4326)

    image = (
        db_session.query(Image)
        .filter(Image.image_id == download_response.image_id)
        .filter(Image.timestamp == download_response.timestamp)
        .filter(Image.bbox.ST_Equals(bbox_geom))
        .filter(Image.job_id == job_id)
        .first()
    )

    return image is not None
